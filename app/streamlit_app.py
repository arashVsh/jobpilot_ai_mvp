from __future__ import annotations
import sys
import json
import os
import shutil
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.append(str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # optional fallback
    st_autorefresh = None

from app.config import JOB_CATEGORIES, DEFAULT_LOCATIONS, WORK_FORMATS, ScanSettings
from app.models import CandidateProfile, JobPosting, FitResult, CompanyResearch, ContactCandidate
from app.utils.resume_parser import extract_text_from_upload, clean_latex, extract_skills, extract_projects, infer_name_email
from app.scanner.job_scanner import (
    ScannerState,
    scan_once,
    start_single_scan,
    start_background_scan,
    stop_background_scan,
    request_stop,
    clear_stop_request,
    read_scanner_status,
    role_matches_location,
    role_matches_work_format,
    role_matches_categories,
    application_window_open,
    scanner_is_active,
    enrich_job_record,
)
from app.storage.job_store import (
    connect,
    list_jobs,
    list_favorites,
    list_applied,
    list_unapplied,
    get_job,
    update_status,
    update_applied_favorite,
    update_follow_up,
    mark_follow_up_completed,
    list_followups,
    add_activity_log,
    list_activity_logs,
    clear_activity_logs,
    monthly_application_counts,
    applications_by_company,
    applications_by_role,
    applied_jobs_detail,
    latest_job_update_epoch,
    activity_summary,
    list_rejected_jobs,
    rejected_jobs_summary,
    clear_rejected_jobs,
    soft_delete_job,
    archive_visible_jobs,
    archive_job_ids,
    clear_jobs,
)
from app.agents.email_drafter import draft_email
from app.agents.resume_tailor import tailor_latex, compile_pdf
from app.utils.api_key_store import load_saved_api_keys, save_api_keys, clear_saved_api_keys, mask_key
from app.utils.salary import job_meets_salary_threshold
from app.utils.text_cleaning import clean_job_text, normalize_plain_text

APP_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = APP_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
DB_PATH = str(OUTPUT_DIR / "jobpilot.sqlite")

st.set_page_config(page_title="JobPilot AI", layout="wide")
st.title("JobPilot AI — Autonomous Job Search Agent")
st.caption("Upload a resume, select industries/location/work format/salary filters, scan, then track applied and favorite jobs persistently.")

if st_autorefresh:
    st_autorefresh(interval=10000, key="job_table_refresh")

conn = connect(DB_PATH)

# Local API-key memory for convenience in a local/dev app. This is intentionally
# simple JSON storage, not production-grade encrypted secret management.
saved_api_keys = load_saved_api_keys(APP_DIR)

# Process-level scanner state is initialized before the sidebar so search filters
# can be disabled while a scan is running. This prevents changing filters in the
# middle of a scan and makes scanner behavior easier to reason about.
if "scanner_state" not in st.session_state:
    st.session_state.scanner_state = ScannerState()
state: ScannerState = st.session_state.scanner_state
early_scanner_active = scanner_is_active(DB_PATH, stale_after_seconds=300)


def _source_status(label: str, active_key: str, env_name: str, purpose: str) -> str:
    if active_key:
        return f"✅ {label}: active from UI — {purpose}"
    if os.getenv(env_name):
        return f"✅ {label}: active from environment — {purpose}"
    return f"⚪ {label}: not configured — {purpose} unavailable or limited"


def _effective_key(state_name: str, env_name: str) -> str:
    return (st.session_state.get(state_name, "") or os.getenv(env_name, "")).strip()


def _email_parts(email_text: str) -> tuple[str, str]:
    subject = "Job application"
    lines = email_text.splitlines()
    body_lines: list[str] = []
    for line in lines:
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip() or subject
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return subject, body


def _mailto_url(contact_email: str | None, email_text: str) -> str:
    subject, body = _email_parts(email_text)
    recipient = contact_email or ""
    return f"mailto:{recipient}?subject={quote(subject)}&body={quote(body)}"



def _urls_equivalent(a: str | None, b: str | None) -> bool:
    """Return True when two public links effectively point to the same page.

    This prevents duplicate buttons such as "Open contact/company page" and
    "Open job posting" when the contact route is just the job posting URL.
    Fragments and trailing slashes are ignored; query strings are kept because
    ATS links sometimes use them meaningfully.
    """
    if not a or not b:
        return False
    try:
        pa, pb = urlparse(a.strip()), urlparse(b.strip())
        def norm(x):
            host = (x.netloc or '').lower().removeprefix('www.')
            path = (x.path or '/').rstrip('/') or '/'
            return (x.scheme.lower(), host, path, x.query)
        return norm(pa) == norm(pb)
    except Exception:
        return a.strip().rstrip('/') == b.strip().rstrip('/')

def _bool(v) -> bool:
    return bool(int(v or 0))


def _render_status_badge(applied: bool, favorite: bool) -> str:
    parts = []
    if applied:
        parts.append("✅ Applied")
    if favorite:
        parts.append("⭐ Favorite")
    return " | ".join(parts) if parts else "New"



def _row_matches_current_filters(row, current_settings: ScanSettings) -> bool:
    """Apply the current sidebar filters to saved rows before display.

    Saved jobs are persistent history. This helper prevents a job saved under an
    older/broader search, such as Tokyo or US-only remote, from appearing when the
    user currently selected Canada only.
    """
    try:
        job = JobPosting(**json.loads(row["job_json"]))
    except Exception:
        job = JobPosting(
            title=row["title"] or "",
            company=row["company"] or "",
            location=row["location"] or "",
            apply_url=row["apply_url"] or "",
            source=row["source"] or "saved",
        )
    job.title = normalize_plain_text(job.title)
    job.company = normalize_plain_text(job.company)
    job.location = normalize_plain_text(job.location)
    job.description = clean_job_text(job.description)
    if not role_matches_categories(job, current_settings.categories):
        return False
    if not role_matches_location(job, current_settings.locations, current_settings.work_formats):
        return False
    if not role_matches_work_format(job, current_settings.work_formats):
        return False
    window_ok, _ = application_window_open(job)
    if not window_ok:
        return False
    salary_ok, _ = job_meets_salary_threshold(
        job,
        min_hourly=float(current_settings.min_hourly_rate or 0),
        min_annual=int(current_settings.min_annual_salary or 0),
    )
    if not salary_ok:
        return False
    return True


def _filter_saved_rows_for_current_view(rows, current_settings: ScanSettings):
    return [r for r in rows if _row_matches_current_filters(r, current_settings)]

def _requirement_preview(job: JobPosting, fit: FitResult, max_items: int = 5) -> list[str]:
    """Return a compact, row-card friendly preview of likely important requirements.

    Prefer structured fit-scorer output because it is already based on the job
    text and uploaded resume. Fall back to common technical/role keywords from
    the job description when the fit output is sparse.
    """
    items: list[str] = []

    for skill in fit.matched_skills[:3]:
        if skill and skill not in items:
            items.append(skill)
    for skill in fit.missing_skills[:2]:
        label = f"Gap: {skill}"
        if skill and label not in items:
            items.append(label)

    if len(items) < 3 and job.description:
        common_terms = [
            "Python", "Java", "JavaScript", "TypeScript", "React", "Node", "FastAPI",
            "SQL", "PostgreSQL", "AWS", "Azure", "GCP", "Docker", "Kubernetes",
            "Machine Learning", "PyTorch", "TensorFlow", "LLM", "RAG", "NLP",
            "Cybersecurity", "Security+", "Help Desk", "Teaching", "Tutoring",
            "Communication", "Customer support", "REST API", "Git",
        ]
        desc_lower = job.description.lower()
        for term in common_terms:
            if term.lower() in desc_lower and term not in items:
                items.append(term)
            if len(items) >= max_items:
                break

    return items[:max_items]



def _infer_work_format(job: JobPosting) -> str:
    text = f"{job.title} {job.location} {job.description} {job.employment_type}".lower()
    if re.search(r"\bhybrid\b", text):
        return "Hybrid"
    if re.search(r"\bremote\b|work from home|anywhere", text):
        return "Remote"
    return "On-site / not stated"


def _salary_label(row, job: JobPosting) -> str:
    evidence = normalize_plain_text(row["salary_evidence"] if "salary_evidence" in row.keys() else "")
    text = f"{job.title} {job.location} {job.description} {evidence}"
    money = re.findall(r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:k|K)?(?:\s?[-–—to]+\s?\$?\s?\d[\d,]*(?:\.\d+)?\s?(?:k|K)?)?(?:\s?(?:/|per)?\s?(?:hour|hr|year|yr|annually|annual|CAD|USD))?", text)
    if money:
        return normalize_plain_text(money[0])[:80]
    if evidence and "no disclosed" not in evidence.lower():
        return evidence[:80]
    return "Not disclosed"


def _why_accepted(fit: FitResult, row=None) -> str:
    bits: list[str] = []
    if fit.score:
        bits.append(f"fit score {fit.score}")
    if fit.matched_skills:
        bits.append("matched " + ", ".join(fit.matched_skills[:4]))
    if fit.rationale:
        rationale = normalize_plain_text(fit.rationale)
        if rationale and rationale.lower() not in " ".join(bits).lower():
            bits.append(rationale[:160])
    return "; ".join(bits) if bits else "Accepted because it passed the selected filters and fit threshold."


def _company_special(job: JobPosting, research: CompanyResearch) -> str:
    detail = normalize_plain_text(research.unique_detail)
    bad = ["research pending", "your company mission", "job openings", "recently posted", "privacy", "cookie"]
    if detail and not any(x in detail.lower() for x in bad):
        return detail[:260]
    desc = clean_job_text(job.description)
    # Fall back to a role-specific company/product signal from the posting.
    for sent in re.split(r"(?<=[.!?])\s+", desc):
        sent = normalize_plain_text(sent)
        if 70 <= len(sent) <= 240 and any(x in sent.lower() for x in ["mission", "platform", "product", "customers", "build", "helps", "ai", "security", "open source"]):
            return sent
    return "No reliable unique company detail found yet; review the company website before sending outreach."


def _resume_suggestions(job: JobPosting, fit: FitResult, profile: CandidateProfile) -> list[str]:
    suggestions: list[str] = []
    title_desc = f"{job.title} {job.description}".lower()
    matched = [normalize_plain_text(x) for x in fit.matched_skills[:4] if x]
    gaps = [normalize_plain_text(x) for x in fit.missing_skills[:3] if x]
    if matched:
        suggestions.append("Emphasize evidence for: " + ", ".join(matched) + ".")
    if gaps:
        suggestions.append("Briefly address or avoid overstating gaps: " + ", ".join(gaps) + ".")
    if any(x in title_desc for x in ["llm", "rag", "agentic", "generative ai"]):
        suggestions.append("Mention your most relevant LLM/RAG or agentic-AI project near the top of the project section.")
    elif any(x in title_desc for x in ["react", "typescript", "frontend", "backend", "api", "software"]):
        suggestions.append("Move software/API/deployed-app projects above research-heavy projects for this role.")
    elif any(x in title_desc for x in ["security", "cyber", "threat", "soc"]):
        suggestions.append("Highlight security certifications and AI-security/adversarial-ML work before general ML details.")
    elif any(x in title_desc for x in ["teaching", "tutor", "instructor", "trainer"]):
        suggestions.append("Emphasize TA, tutoring, course development, and communication experience.")
    if not suggestions:
        suggestions.append("Keep the resume concise and mirror the top role keywords without adding unsupported claims.")
    return suggestions[:3]


def _format_log_message(message: str) -> str:
    message = normalize_plain_text(message)
    # Remove repeated agent prefix when displaying inside an agent-labeled row.
    return re.sub(r"^(Search Agent|Filter Agent|Fit Agent|Research Agent|Email Agent|Tracker Agent|System):\s*", "", message)

def _render_selected_job_details(row, selected_id: int, settings: ScanSettings, profile: CandidateProfile):
    """Render the detailed job panel inline under the selected row card."""
    job = JobPosting(**json.loads(row["job_json"]))
    job.title = normalize_plain_text(job.title)
    job.company = normalize_plain_text(job.company)
    job.location = normalize_plain_text(job.location)
    job.description = clean_job_text(job.description)
    fit = FitResult(**json.loads(row["fit_json"]))
    research = CompanyResearch(**json.loads(row["research_json"]))
    contact_data = json.loads(row["contact_json"] or "{}")
    contact = ContactCandidate(**contact_data) if contact_data else ContactCandidate(name="", title="")
    ceo_data = json.loads(row["ceo_json"] or "{}") if row["ceo_json"] else {}
    ceo = ContactCandidate(**ceo_data) if ceo_data else ContactCandidate(
        name=row["ceo_name"] or "",
        title=row["ceo_title"] or "CEO/Founder",
        email=row["ceo_email"],
        contact_url=row["ceo_url"],
        confidence=row["ceo_confidence"] or "low",
    )

    st.markdown("---")
    d_head, d_delete = st.columns([8, 1])
    with d_head:
        st.markdown(f"### {job.title} — {job.company}")
    with d_delete:
        if st.button("🗑️", key=f"detail_delete_top_{selected_id}", help="Delete/archive this job immediately"):
            soft_delete_job(conn, int(selected_id))
            if st.session_state.get("selected_job_id") == int(selected_id):
                st.session_state.pop("selected_job_id", None)
            add_activity_log(conn, "Tracker Agent", f"Tracker Agent: archived {job.title} at {job.company}.", job_id=int(selected_id))
            st.rerun()

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Fit score", fit.score)
    m2.metric("Decision", fit.decision)
    m3.metric("Work format", _infer_work_format(job))
    m4.metric("Salary", _salary_label(row, job))
    m5.metric("Applied", "Yes" if _bool(row["applied"]) else "No")
    m6.metric("Favorite", "Yes" if _bool(row["favorite"]) else "No")
    st.caption(f"Location: {job.location or 'Not listed'} | Source: {job.source}")

    c_applied, c_fav = st.columns([1, 1])
    with c_applied:
        new_applied = st.checkbox("Applied", value=_bool(row["applied"]), key=f"detail_applied_{selected_id}")
    with c_fav:
        new_favorite = st.checkbox("⭐ Favorite", value=_bool(row["favorite"]), key=f"detail_fav_{selected_id}")
    if new_applied != _bool(row["applied"]) or new_favorite != _bool(row["favorite"]):
        update_applied_favorite(conn, int(selected_id), applied=new_applied, favorite=new_favorite)
        if new_applied != _bool(row["applied"]):
            add_activity_log(conn, "Tracker Agent", f"Tracker Agent: marked {job.title} at {job.company} as {'applied' if new_applied else 'not applied'}.", job_id=int(selected_id))
        if new_favorite != _bool(row["favorite"]):
            add_activity_log(conn, "Tracker Agent", f"Tracker Agent: {'favorited' if new_favorite else 'unfavorited'} {job.title} at {job.company}.", job_id=int(selected_id))
        st.success("Applied/favorite flags saved.")
        st.rerun()

    left, right = st.columns([2, 1])
    with left:
        st.write("**Why this job was accepted:**", _why_accepted(fit, row))
        if fit.matched_skills:
            st.write("**Matched skills/evidence:**", ", ".join(fit.matched_skills))
        if fit.missing_skills:
            st.write("**Possible missing skills:**", ", ".join(fit.missing_skills))
        if fit.concerns:
            st.warning("; ".join(fit.concerns))
        if row["salary_evidence"]:
            st.write("**Salary filter evidence:**", row["salary_evidence"])
        st.write("**Company-specific detail for outreach:**", _company_special(job, research))
        if research.source_url:
            st.caption(f"Company detail source: {research.source_url}")
        needs_research = (not research.unique_detail) or ("pending" in (research.unique_detail or "").lower()) or (research.confidence == "low" and not research.source_url)
        if needs_research:
            if st.button("Research company/contact now", key=f"research_now_{selected_id}"):
                with st.spinner("Researching company, contact route, and CEO/founder source..."):
                    ok = enrich_job_record(int(selected_id), DB_PATH, tavily_api_key=settings.tavily_api_key or None)
                if ok:
                    st.success("Company/contact research updated.")
                else:
                    st.warning("Could not enrich this company from public sources.")
                st.rerun()
        st.write("**Brief resume suggestions for this role:**")
        for suggestion in _resume_suggestions(job, fit, profile):
            st.caption("• " + suggestion)
        if job.description:
            with st.expander("Job description"):
                st.text_area(
                    "Plain-text description",
                    value=clean_job_text(job.description),
                    height=360,
                    disabled=True,
                    label_visibility="collapsed",
                    key=f"plain_desc_{selected_id}",
                )
    with right:
        st.write("**Potential recruiter/contact route**")
        has_recruiter_contact = bool(contact and (contact.email or contact.contact_url))
        shown_primary_link = False
        if has_recruiter_contact:
            st.write(contact.name or "General Hiring Contact")
            st.caption(contact.title or "Contact route")
            st.caption(contact.reason or "Public contact route found. Verify before outreach.")
            if contact.email:
                st.success(contact.email)
            # If the contact URL is only the job posting and no email was found,
            # do not show both buttons. The posting link below is enough.
            if contact.contact_url and not _urls_equivalent(contact.contact_url, job.apply_url):
                st.link_button("Open contact/company page", contact.contact_url)
                shown_primary_link = True
        else:
            st.info("No public recruiter/contact information found. Use the application link only.")
        if job.apply_url:
            st.link_button("Open job posting", job.apply_url)
            shown_primary_link = True

        st.write("**CEO / founder route**")
        if ceo and (ceo.name or ceo.email or ceo.contact_url):
            st.write(ceo.name or "Unknown")
            st.caption(ceo.title or "CEO/Founder")
            if ceo.email:
                st.success(ceo.email)
            # Only show the CEO/source page when it is not simply the same URL
            # as the job posting or recruiter/company route.
            duplicate_ceo_link = _urls_equivalent(ceo.contact_url, job.apply_url) or _urls_equivalent(ceo.contact_url, contact.contact_url if contact else None)
            if ceo.contact_url and not duplicate_ceo_link:
                st.link_button("Open CEO/source page", ceo.contact_url)
            elif ceo.contact_url and duplicate_ceo_link and not ceo.email:
                st.caption("CEO/source link is the same as the posting/contact page, so it is not repeated.")
            st.caption(ceo.reason or "Public source found; verify manually.")
        else:
            st.info("No public CEO/founder contact route found yet. Tavily improves this.")

    st.subheader("Editable email draft")
    st.caption("Drafting method: OpenAI Responses API when an OpenAI key is active; otherwise a local rule-based fallback. The email is never sent automatically.")
    email_key = f"email_draft_{selected_id}"
    if email_key not in st.session_state:
        st.session_state[email_key] = draft_email(profile, job, fit, research, contact, openai_api_key=settings.openai_api_key or None)
    gen_label = "Regenerate OpenAI-enhanced draft" if settings.openai_api_key else "Regenerate rule-based draft"
    if st.button(gen_label, key=f"gen_email_{selected_id}"):
        with st.spinner("Drafting email from uploaded resume and job description..."):
            st.session_state[email_key] = draft_email(profile, job, fit, research, contact, openai_api_key=settings.openai_api_key or None)
            add_activity_log(conn, "Email Agent", f"Email Agent: drafted outreach for {job.title} at {job.company} using {'OpenAI Responses API' if settings.openai_api_key else 'rule-based fallback'}.", job_id=int(selected_id))
    draft = st.text_area("Draft", value=st.session_state[email_key], height=300, key=f"email_text_area_{selected_id}")
    st.session_state[email_key] = draft

    if contact and contact.email:
        st.link_button("Email recruiter / open email client", _mailto_url(contact.email, draft))
    elif contact and contact.contact_url:
        st.caption("No recruiter email found. The app shows the public contact route above instead of an email button.")
    else:
        st.caption("No recruiter contact information found, so the email recruiter button is hidden.")
    if ceo and ceo.email:
        st.link_button("Email CEO / open email client", _mailto_url(ceo.email, draft))

    st.subheader("Application status")
    status_options = ["New", "Reviewed", "Email drafted", "Applied", "Followed up", "Interview", "Rejected", "Archived"]
    current_status = row["status"] if row["status"] in status_options else "New"
    new_status = st.selectbox("Status", status_options, index=status_options.index(current_status), key=f"status_{selected_id}")
    if st.button("Save status", key=f"save_status_{selected_id}"):
        update_status(conn, int(selected_id), new_status)
        if new_status == "Applied":
            update_applied_favorite(conn, int(selected_id), applied=True, favorite=None)
        add_activity_log(conn, "Tracker Agent", f"Tracker Agent: changed status for {job.title} at {job.company} to {new_status}.", job_id=int(selected_id))
        st.success("Status updated.")
        st.rerun()

    st.subheader("Follow-up reminder")
    if _bool(get_job(conn, int(selected_id))["applied"]):
        follow_options = ["No follow-up", "Follow up in 7 days", "Follow up in 10 days"]
        refreshed_row = get_job(conn, int(selected_id))
        current_follow = refreshed_row["follow_up_choice"] if refreshed_row["follow_up_choice"] in follow_options else "No follow-up"
        follow_choice = st.radio(
            "Reminder",
            follow_options,
            index=follow_options.index(current_follow),
            horizontal=True,
            key=f"follow_choice_{selected_id}",
        )
        if st.button("Save follow-up reminder", key=f"save_followup_{selected_id}"):
            update_follow_up(conn, int(selected_id), follow_choice)
            add_activity_log(conn, "Tracker Agent", f"Tracker Agent: set follow-up for {job.title} at {job.company} to {follow_choice}.", job_id=int(selected_id))
            st.success("Follow-up reminder saved.")
            st.rerun()
        refreshed_row = get_job(conn, int(selected_id))
        if refreshed_row["follow_up_due_at"]:
            st.info(f"Follow-up due: {refreshed_row['follow_up_due_at'][:10]}")
    else:
        st.caption("Mark this job as applied to set a follow-up reminder.")


# Pre-fill API-key inputs from locally saved keys when available.
st.session_state.setdefault("active_serpapi_key", "")
st.session_state.setdefault("active_tavily_key", "")
st.session_state.setdefault("active_openai_key", "")
st.session_state.setdefault("pending_serpapi_key", saved_api_keys.get("serpapi", ""))
st.session_state.setdefault("pending_tavily_key", saved_api_keys.get("tavily", ""))
st.session_state.setdefault("pending_openai_key", saved_api_keys.get("openai", ""))
st.session_state.setdefault("remember_api_keys", bool(any(saved_api_keys.values())))

with st.sidebar:
    controls_disabled = bool(early_scanner_active)
    if controls_disabled:
        st.warning("A scanner is running. Search filters are locked until it stops.")
    st.header("Storage")
    st.caption("Results are automatically saved in the local SQLite database. Use export/download as a backup, to move history to another machine, or to restore after deleting/reinstalling the app.")
    db_file = Path(DB_PATH)
    if db_file.exists():
        st.download_button(
            "Export / backup saved database",
            data=db_file.read_bytes(),
            file_name="jobpilot_saved_results.sqlite",
            mime="application/octet-stream",
        )
    uploaded_db = st.file_uploader("Load saved database (.sqlite)", type=["sqlite", "db"])
    if uploaded_db and st.button("Replace current database with uploaded file"):
        try:
            conn.close()
        except Exception:
            pass
        db_file.parent.mkdir(exist_ok=True)
        db_file.write_bytes(uploaded_db.getvalue())
        st.success("Saved database loaded.")
        st.rerun()

    st.header("1) Candidate resume")
    uploaded_resume = st.file_uploader("Upload resume (.pdf, .tex, .txt)", type=["pdf", "tex", "txt"], key="resume_upload", disabled=controls_disabled)
    resume_text = ""
    if uploaded_resume:
        resume_text = extract_text_from_upload(uploaded_resume)
        plain_resume = clean_latex(resume_text)
        name, email = infer_name_email(resume_text)
        skills = extract_skills(resume_text)
        projects = extract_projects(resume_text)
        st.success(f"Resume parsed: {len(plain_resume):,} characters, {len(skills)} skills detected.")
    else:
        plain_resume = ""
        name, email = "Candidate", ""
        skills, projects = [], {}
        st.info("Upload a resume first. Fit scoring is based on the uploaded resume.")

    st.caption("These fields are used only for email drafts. They are pre-filled when the resume parser can detect them.")
    candidate_name = st.text_input("Name for email drafts", value="" if name == "Candidate" else name, disabled=controls_disabled)
    candidate_email = st.text_input("Email for email drafts", value=email, disabled=controls_disabled)
    candidate_title = st.text_input("Current title / one-line background", value="", disabled=controls_disabled)
    candidate_affiliation = st.text_input("Affiliation for email signature", value="", help="Example: MSc Computer Science, University of New Brunswick", disabled=controls_disabled)
    candidate_focus = st.text_input("Focus / target area", value="", disabled=controls_disabled)
    candidate_portfolio = st.text_input("Portfolio / website / LinkedIn URL", value="", disabled=controls_disabled)
    candidate_extra_link = st.text_input("Additional profile link, optional", value="", disabled=controls_disabled)

    st.header("2) Search scope")
    categories = st.multiselect(
        "General industries / role families",
        list(JOB_CATEGORIES.keys()),
        default=["Software Development", "Machine Learning / AI", "Agentic AI / LLM", "Cybersecurity / AI Security", "Teaching / Tutoring"],
        disabled=controls_disabled,
    )
    locations = st.multiselect(
        "Locations",
        DEFAULT_LOCATIONS,
        default=["Canada"],
        help="If Canada is selected, the scanner includes roles anywhere in Canada regardless of city.",
        disabled=controls_disabled,
    )
    if "Canada" in locations:
        st.caption("Canada selected: Canada-wide roles are included regardless of city.")
    work_formats = st.multiselect("Work format", WORK_FORMATS, default=["Remote", "Hybrid", "On-site"], disabled=controls_disabled)
    lookback_hours = st.number_input("Only jobs opened within last N hours", min_value=1, max_value=720, value=24, step=1, disabled=controls_disabled)
    min_score = st.slider("Minimum fit score", 0, 100, 75, disabled=controls_disabled)
    st.caption("If the list seems stuck at a small number, lower the fit score or increase the search depth below. Strict Canada + 75+ fit can legitimately keep only a few roles.")
    search_depth = st.selectbox(
        "Search speed / depth",
        ["Fast", "Balanced", "Broad", "Deep"],
        index=0,
        help="Fast streams jobs page-by-page and stops after a useful batch. Broad/Deep use more API quota and take longer.",
        disabled=controls_disabled,
    )
    depth_settings = {
        "Fast": (18, 1, 3),
        "Balanced": (40, 2, 5),
        "Broad": (80, 3, 8),
        "Deep": (140, 4, 14),
    }
    serpapi_request_budget, serpapi_pages_per_query, ats_boards_per_provider = depth_settings[search_depth]
    max_new_jobs_per_scan = st.number_input(
        "Stop each scan after this many new jobs",
        min_value=5, max_value=200, value=30, step=5,
        help="Keeps scans quick. Run another scan to continue discovery after the first batch.",
        disabled=controls_disabled,
    )
    enrich_during_scan = st.checkbox(
        "Research companies during scan",
        value=False,
        help="Off is faster. When off, rows appear quickly and company/contact research can be run from a selected job.",
        disabled=controls_disabled,
    )
    enable_remotive = st.checkbox(
        "Include no-key remote-job sources",
        value=True,
        help="Adds bounded Remotive and RemoteOK passes. Useful for remote roles and does not need an API key.",
        disabled=controls_disabled,
    )
    records_per_page = st.number_input("Saved-job records per page", min_value=5, max_value=100, value=30, step=5, help="Limits how many job cards are shown on each page.")
    interval_seconds = 90
    background_max_minutes = 15
    background_no_new_limit = 2
    exclude_volunteer = st.checkbox("Exclude volunteer/unpaid work", value=True, disabled=controls_disabled)
    include_teaching = st.checkbox("Include tutoring/teaching", value=True, disabled=controls_disabled)
    st.caption("Hard rule: roles requiring 5+ years are excluded.")

    st.header("3) Salary filters")
    min_hourly_rate = st.number_input("Minimum hourly rate ($/hour)", min_value=0.0, max_value=500.0, value=0.0, step=1.0, disabled=controls_disabled)
    min_annual_salary = st.number_input("Minimum annual salary ($/year)", min_value=0, max_value=500000, value=0, step=5000, disabled=controls_disabled)
    st.caption("Jobs with no disclosed salary are kept. Only jobs explicitly below your thresholds are filtered out.")

    st.header("4) API keys")
    st.caption("Paste keys here, then click Apply API keys. Saved keys are pre-filled on later local runs, but are stored locally in plain JSON for convenience only.")
    if any(saved_api_keys.values()):
        st.caption(
            "Saved keys found — "
            f"SerpAPI {mask_key(saved_api_keys.get('serpapi', ''))}; "
            f"Tavily {mask_key(saved_api_keys.get('tavily', ''))}; "
            f"OpenAI {mask_key(saved_api_keys.get('openai', ''))}."
        )
    pending_serpapi_key = st.text_input("SerpAPI API key", type="password", help="Best improvement for automatic job discovery.", key="pending_serpapi_key")
    pending_tavily_key = st.text_input("Tavily API key", type="password", help="Improves company, recruiter, and CEO research.", key="pending_tavily_key")
    pending_openai_key = st.text_input("OpenAI API key", type="password", help="Improves fit reasoning and email drafting.", key="pending_openai_key")
    remember_api_keys = st.checkbox(
        "Remember these API keys on this computer",
        key="remember_api_keys",
        help="Stores keys in outputs/saved_api_keys.json. For deployment, use environment variables instead."
    )

    if st.button("Apply API keys", type="primary"):
        st.session_state.active_serpapi_key = pending_serpapi_key.strip()
        st.session_state.active_tavily_key = pending_tavily_key.strip()
        st.session_state.active_openai_key = pending_openai_key.strip()
        st.session_state.api_keys_applied_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if remember_api_keys:
            save_api_keys(
                APP_DIR,
                serpapi=st.session_state.active_serpapi_key,
                tavily=st.session_state.active_tavily_key,
                openai=st.session_state.active_openai_key,
            )
            st.success("API keys applied and saved locally for future runs.")
        else:
            st.success("API keys applied to this app session.")

    k1, k2 = st.columns(2)
    with k1:
        if st.button("Clear API keys from session"):
            st.session_state.active_serpapi_key = ""
            st.session_state.active_tavily_key = ""
            st.session_state.active_openai_key = ""
            st.warning("Active session API keys cleared. Saved keys and environment variables may still exist.")
    with k2:
        if st.button("Forget saved API keys"):
            clear_saved_api_keys(APP_DIR)
            for k in ["pending_serpapi_key", "pending_tavily_key", "pending_openai_key", "active_serpapi_key", "active_tavily_key", "active_openai_key"]:
                st.session_state[k] = ""
            st.session_state.remember_api_keys = False
            st.warning("Saved local API keys deleted and current key fields cleared.")
            st.rerun()

    st.markdown("**Active API status**")
    st.caption(_source_status("SerpAPI", st.session_state.active_serpapi_key, "SERPAPI_API_KEY", "broad job discovery"))
    st.caption(_source_status("Tavily", st.session_state.active_tavily_key, "TAVILY_API_KEY", "company/recruiter/CEO research"))
    st.caption(_source_status("OpenAI", st.session_state.active_openai_key, "OPENAI_API_KEY", "LLM fit scoring and email drafting"))
    if st.session_state.get("api_keys_applied_at"):
        st.caption(f"Last Apply API keys click: {st.session_state.api_keys_applied_at}")

profile = CandidateProfile(
    name=(candidate_name.strip() or (name if name != "Candidate" else "Candidate")),
    email=candidate_email.strip(),
    portfolio=candidate_portfolio.strip(),
    scholar=candidate_extra_link.strip(),
    current_title=candidate_title.strip() or "candidate with a computer science or technical background",
    affiliation=candidate_affiliation.strip(),
    focus=candidate_focus.strip(),
    resume_text=plain_resume,
    core_skills=skills,
    projects=projects,
    include_teaching=include_teaching,
    exclude_volunteer=exclude_volunteer,
)
settings = ScanSettings(
    categories=categories,
    lookback_hours=int(lookback_hours),
    min_score=int(min_score),
    locations=locations,
    work_formats=work_formats,
    include_teaching=include_teaching,
    exclude_volunteer=exclude_volunteer,
    min_hourly_rate=float(min_hourly_rate),
    min_annual_salary=int(min_annual_salary),
    serpapi_api_key=_effective_key("active_serpapi_key", "SERPAPI_API_KEY"),
    tavily_api_key=_effective_key("active_tavily_key", "TAVILY_API_KEY"),
    openai_api_key=_effective_key("active_openai_key", "OPENAI_API_KEY"),
    serpapi_max_requests=int(serpapi_request_budget),
    serpapi_pages_per_query=int(serpapi_pages_per_query),
    ats_boards_per_provider=int(ats_boards_per_provider),
    search_depth=search_depth,
    max_new_jobs_per_scan=int(max_new_jobs_per_scan),
    enrich_during_scan=bool(enrich_during_scan),
    enable_remotive=bool(enable_remotive),
)

st.subheader("Scanner")
api_cols = st.columns(3)
api_cols[0].info("SerpAPI: active" if settings.serpapi_api_key else "SerpAPI: not active — ATS only")
api_cols[1].info("Tavily: active" if settings.tavily_api_key else "Tavily: not active — basic company pages")
api_cols[2].info("OpenAI: active" if settings.openai_api_key else "OpenAI: not active — rule-based drafts/scoring")

if settings.openai_api_key and not settings.serpapi_api_key:
    st.warning("OpenAI is active and improves fit reasoning/email drafts, but it does not discover more jobs by itself. Add SerpAPI for broader automatic job discovery.")

st.caption(f"Search mode: {search_depth} | SerpAPI request budget: {serpapi_request_budget} | pages/query: {serpapi_pages_per_query} | stops after {int(max_new_jobs_per_scan)} new jobs. Company research during scan: {'on' if enrich_during_scan else 'off for speed'}.")

status_file = read_scanner_status(DB_PATH)
status_updated_at = float(status_file.get("updated_at") or 0)
status_recent = (datetime.now().timestamp() - status_updated_at) < max(int(interval_seconds) * 3, 300)
status_claims_running = bool(status_file.get("running"))
scanner_running = scanner_is_active(DB_PATH, stale_after_seconds=max(int(interval_seconds) * 3, 300))

if status_claims_running and not status_recent and not state.running:
    status_file["last_message"] = "Idle — previous scanner status was stale."

c1, c2, c3, c4 = st.columns([1.35, 1.05, 1.2, 3.4])
scan_disabled = not uploaded_resume or not categories or not locations or not work_formats

with c1:
    if st.button("Start scanning", type="primary", disabled=scan_disabled or scanner_running, width="stretch"):
        # One bounded scanner only. It streams rows into SQLite and stops after
        # the selected job limit, a safety time limit, or a user stop request.
        start_single_scan(state, profile, settings, DB_PATH, max_scan_seconds=300 if search_depth == "Deep" else 210 if search_depth == "Broad" else 150 if search_depth == "Balanced" else 90)
        st.info(f"Scan started with {search_depth.lower()} depth. Filters are locked until it stops.")
        st.rerun()
with c2:
    if scanner_running:
        if st.button("Stop scanner", width="stretch"):
            stop_background_scan(state, DB_PATH)
            request_stop(DB_PATH)
            st.warning("Stop requested. The scanner will stop at the next safe checkpoint/API-return point.")
            st.rerun()
    else:
        st.caption("No scanner running")
with c3:
    if st.button("Reset visible list"):
        # Archive only rows that match the current sidebar filters/view, not every
        # saved job in the database. This keeps other saved searches intact.
        current_visible_rows = _filter_saved_rows_for_current_view(list_jobs(conn), settings)
        removed = archive_job_ids(conn, [int(r["id"]) for r in current_visible_rows])
        st.session_state.pop("selected_job_id", None)
        st.success(f"Archived {removed} currently visible job record(s). History is preserved, so they will not be suggested again.")
        st.rerun()
with c4:
    st.write(f"Status: **{status_file.get('last_message') or state.last_message}**")
    if scanner_running:
        if state.running:
            st.caption("Scanner running in this session")
        else:
            st.caption("A scanner from a previous refresh/session appears to still be running")
    elif status_claims_running and not status_recent:
        st.caption("Scanner idle; previous status was stale. Use Force stop if old rows keep appearing.")
    else:
        st.caption("Scanner idle")

with st.expander("Danger zone"):
    st.caption("Use these controls carefully. Force stop writes a stop flag that old background scanners must respect. Wipe deletes all job history.")
    dz1, dz2 = st.columns(2)
    with dz1:
        if st.button("Force stop any scanner"):
            stop_background_scan(state, DB_PATH)
            request_stop(DB_PATH)
            add_activity_log(conn, "System", "System: force-stop requested for any active/stale background scanner.", level="warning")
            st.warning("Force stop requested. If an old scanner was still adding rows, it should stop at the next safe checkpoint/API-return point.")
            st.rerun()
    with dz2:
        if st.button("Wipe database and history"):
            st.session_state.pending_wipe_database = True
            st.rerun()

st.session_state.setdefault("pending_wipe_database", False)
if st.session_state.pending_wipe_database:
    @st.dialog("Wipe database and history?")
    def _confirm_wipe_database_dialog():
        st.error("This will permanently delete all saved jobs, applied/favorite flags, follow-up reminders, archived history, and agent logs from the local SQLite database.")
        st.caption("Use Export / backup saved database first if you might need this history later.")
        w1, w2 = st.columns(2)
        with w1:
            if st.button("Yes, wipe everything", type="primary"):
                jobs_removed = clear_jobs(conn)
                logs_removed = clear_activity_logs(conn)
                st.session_state.pop("selected_job_id", None)
                st.session_state.pending_wipe_database = False
                st.error(f"Deleted {jobs_removed} job record(s) and {logs_removed} activity log entries.")
                st.rerun()
        with w2:
            if st.button("Cancel"):
                st.session_state.pending_wipe_database = False
                st.rerun()
    _confirm_wipe_database_dialog()

if not uploaded_resume:
    st.warning("The autonomous scanner is disabled until a resume is uploaded. This prevents the app from showing jobs without checking qualification against the candidate resume.")


st.divider()
tab_jobs, tab_followups, tab_logs, tab_debug, tab_reports, tab_architecture = st.tabs([
    "Saved jobs",
    "Follow-ups",
    "Agent activity log",
    "Rejected jobs debug",
    "Activity report",
    "Architecture",
])

with tab_jobs:
    st.subheader("Saved job records")

    view = st.radio("View", ["Active", "Not applied", "Favorites", "Applied"], horizontal=True,
                    help="Active means all non-archived jobs. Not applied means active jobs you have not applied to yet.")
    if view == "Favorites":
        rows = list_favorites(conn)
    elif view == "Applied":
        rows = list_applied(conn)
    elif view == "Not applied":
        rows = list_unapplied(conn)
    else:
        rows = list_jobs(conn)

    total_rows_before_current_filters = len(rows)
    rows = _filter_saved_rows_for_current_view(rows, settings)
    hidden_by_current_filters = total_rows_before_current_filters - len(rows)
    if hidden_by_current_filters > 0:
        st.caption(f"{hidden_by_current_filters} saved record(s) are hidden because they do not match the current sidebar filters.")

    # Row delete/archive actions are immediate and persistent.

    if not rows:
        st.info("No saved jobs in this view. Upload a resume, choose filters, apply any API keys, then scan.")
    else:
        st.caption("Click a job row to open details directly under that record. Use Applied/Favorite checkboxes to save status. Use the trash button to archive a row immediately.")

        visible_job_ids = {int(r["id"]) for r in rows}
        if st.session_state.get("selected_job_id") and int(st.session_state.selected_job_id) not in visible_job_ids:
            st.session_state.pop("selected_job_id", None)
            st.info("The previously selected job is hidden because it no longer matches the current view/sidebar filters.")

        total_records = len(rows)
        records_per_page = int(records_per_page or 30)
        total_pages = max(1, (total_records + records_per_page - 1) // records_per_page)
        st.session_state.setdefault("job_page", 1)
        if st.session_state.job_page > total_pages:
            st.session_state.job_page = total_pages
        if st.session_state.job_page < 1:
            st.session_state.job_page = 1
        start_idx = (st.session_state.job_page - 1) * records_per_page
        end_idx = min(start_idx + records_per_page, total_records)
        page_rows = rows[start_idx:end_idx]
        st.caption(f"Showing records {start_idx + 1 if total_records else 0}–{end_idx} of {total_records}. Page {st.session_state.job_page} of {total_pages}.")

        for display_no, r in enumerate(page_rows, start=start_idx + 1):
            job_id = int(r["id"])
            applied = _bool(r["applied"])
            favorite = _bool(r["favorite"])
            selected = st.session_state.get("selected_job_id") == job_id
            prefix = "✅ " if applied else "⭐ " if favorite else ""
            status_label = _render_status_badge(applied, favorite)

            # Load fit/job data for the compact requirement preview shown on every card.
            try:
                preview_job = JobPosting(**json.loads(r["job_json"]))
                preview_job.title = normalize_plain_text(preview_job.title)
                preview_job.company = normalize_plain_text(preview_job.company)
                preview_job.location = normalize_plain_text(preview_job.location)
                preview_job.description = clean_job_text(preview_job.description)
                preview_fit = FitResult(**json.loads(r["fit_json"]))
                req_preview = _requirement_preview(preview_job, preview_fit)
            except Exception:
                req_preview = []

            with st.container(border=True):
                row_cols = st.columns([0.45, 0.9, 0.9, 6.7, 1.2, 0.55])
                with row_cols[0]:
                    st.markdown(f"**{display_no}**")
                    st.caption("No.")
                with row_cols[1]:
                    new_applied = st.checkbox("Applied", value=applied, key=f"row_applied_{job_id}", label_visibility="collapsed")
                    st.caption("Applied")
                with row_cols[2]:
                    new_favorite = st.checkbox("⭐", value=favorite, key=f"row_fav_{job_id}", label_visibility="collapsed")
                    st.caption("Favorite")
                with row_cols[3]:
                    try:
                        card_job = preview_job
                        card_fit = preview_fit
                    except Exception:
                        card_job = JobPosting(title=r["title"] or "", company=r["company"] or "", location=r["location"] or "", apply_url=r["apply_url"] or "", source=r["source"] or "saved")
                        card_fit = FitResult(score=int(r["score"] or 0), decision=r["decision"] or "Maybe", matched_skills=[], missing_skills=[], concerns=[], rationale=r["rationale"] or "")
                    work_format_text = _infer_work_format(card_job)
                    salary_text = _salary_label(r, card_job)
                    row_title = f"{prefix}{r['title']}"
                    if st.button(row_title, key=f"open_job_{job_id}", width="stretch"):
                        st.session_state.selected_job_id = None if selected else job_id
                        st.rerun()
                    st.markdown(f"**Company:** {r['company']}  ")
                    st.caption(f"Location: {r['location'] or 'Not listed'} | Work format: {work_format_text} | Salary: {salary_text} | Fit score: {r['score']} | {status_label}")
                    if req_preview:
                        st.caption("Important requirements: " + " • ".join(req_preview))
                    elif r["concerns"]:
                        st.caption("Review notes: " + str(r["concerns"])[:180])
                    else:
                        st.caption("Important requirements will appear here after fit analysis/enrichment.")
                    st.caption("Why accepted: " + _why_accepted(card_fit, r)[:260])
                with row_cols[4]:
                    if r["apply_url"]:
                        st.link_button("Posting", r["apply_url"], width="stretch")
                with row_cols[5]:
                    if st.button("🗑️", key=f"delete_row_{job_id}", help="Delete/archive this job immediately"):
                        soft_delete_job(conn, job_id)
                        if st.session_state.get("selected_job_id") == job_id:
                            st.session_state.pop("selected_job_id", None)
                        add_activity_log(conn, "Tracker Agent", f"Tracker Agent: archived {r['title']} at {r['company']}.", job_id=job_id)
                        st.rerun()

                if new_applied != applied or new_favorite != favorite:
                    update_applied_favorite(conn, job_id, applied=new_applied, favorite=new_favorite)
                    if new_applied != applied:
                        add_activity_log(conn, "Tracker Agent", f"Tracker Agent: marked {r['title']} at {r['company']} as {'applied' if new_applied else 'not applied'}.", job_id=job_id)
                    if new_favorite != favorite:
                        add_activity_log(conn, "Tracker Agent", f"Tracker Agent: {'favorited' if new_favorite else 'unfavorited'} {r['title']} at {r['company']}.", job_id=job_id)
                    st.rerun()

                # Inline details: the selected row expands directly below its own card.
                if selected:
                    fresh_row = get_job(conn, job_id)
                    if not fresh_row or _bool(fresh_row["is_deleted"]):
                        st.session_state.pop("selected_job_id", None)
                        st.warning("The selected job is no longer active. It may have been archived/deleted.")
                    elif not _row_matches_current_filters(fresh_row, settings):
                        st.session_state.pop("selected_job_id", None)
                        st.warning("The selected job is hidden because it no longer matches the current sidebar filters.")
                    else:
                        _render_selected_job_details(fresh_row, job_id, settings, profile)


        st.divider()
        p_prev, p_mid, p_next = st.columns([1, 2, 1])
        with p_prev:
            if st.button("← Previous page", disabled=st.session_state.job_page <= 1, width="stretch"):
                st.session_state.job_page -= 1
                st.rerun()
        with p_mid:
            selected_page = st.number_input(
                "Page number",
                min_value=1,
                max_value=total_pages,
                value=int(st.session_state.job_page),
                step=1,
            )
            if int(selected_page) != int(st.session_state.job_page):
                st.session_state.job_page = int(selected_page)
                st.rerun()
            st.caption(f"Page {st.session_state.job_page} of {total_pages} • {total_records} matching saved job(s)")
        with p_next:
            if st.button("Next page →", disabled=st.session_state.job_page >= total_pages, width="stretch"):
                st.session_state.job_page += 1
                st.rerun()

        if not st.session_state.get("selected_job_id"):
            st.info("Select a job row to review details, contact routes, and a draft email.")



with tab_followups:
    st.subheader("Follow-up reminders")
    st.caption("These reminders are created after you mark a job as applied and choose a follow-up window.")
    due_only = st.toggle("Show only due follow-ups", value=False)
    follow_rows = list_followups(conn, due_only=due_only)
    if not follow_rows:
        st.info("No follow-up reminders in this view.")
    else:
        for fr in follow_rows:
            with st.container(border=True):
                cols = st.columns([5, 2, 1])
                with cols[0]:
                    st.markdown(f"**{fr['title']} — {fr['company']}**")
                    st.caption(f"Due: {fr['follow_up_due_at'][:10] if fr['follow_up_due_at'] else 'Not set'} | Status: {fr['status']}")
                with cols[1]:
                    if fr['apply_url']:
                        st.link_button("Open posting", fr['apply_url'], width="stretch")
                with cols[2]:
                    if st.button("Done", key=f"follow_done_{fr['id']}", width="stretch"):
                        mark_follow_up_completed(conn, int(fr['id']), True)
                        add_activity_log(conn, "Tracker Agent", f"Tracker Agent: completed follow-up reminder for {fr['title']} at {fr['company']}.", job_id=int(fr['id']))
                        st.rerun()

with tab_logs:
    st.subheader("Agent activity log")
    st.caption("A polished timeline of what each agent did during scans, enrichment, tracking, and email drafting.")
    l1, l2 = st.columns([1, 4])
    with l1:
        if st.button("Clear logs"):
            removed = clear_activity_logs(conn)
            st.warning(f"Cleared {removed} log entries.")
            st.rerun()
    logs = list_activity_logs(conn, limit=300)
    if not logs:
        st.info("No agent activity has been logged yet. Run a scan or generate an email draft.")
    else:
        log_df = pd.DataFrame([dict(x) for x in logs])
        counts = log_df.groupby("agent").size().reset_index(name="events").sort_values("events", ascending=False)
        st.markdown("#### Agent summary")
        st.dataframe(counts, width="stretch", hide_index=True)
        st.markdown("#### Timeline")
        for item in logs[:80]:
            level = item["level"] or "info"
            icon = "⚠️" if level == "warning" else "❌" if level == "error" else "✅"
            with st.container(border=True):
                st.markdown(f"**{icon} {item['agent']}**")
                st.caption(f"{item['created_at']}" + (f" | job #{item['job_id']}" if item['job_id'] else ""))
                st.write(_format_log_message(item["message"]))
        with st.expander("Raw log table"):
            st.dataframe(log_df[["created_at", "agent", "level", "message", "job_id"]], width="stretch", hide_index=True)

with tab_debug:
    st.subheader("Rejected jobs debug")
    st.caption("Shows jobs the scanner rejected and why. This helps tune location, salary, category, and fit-score filters.")
    dbg1, dbg2, dbg3 = st.columns([2, 1, 1])
    with dbg1:
        reason_filter = st.selectbox(
            "Reason filter",
            ["All", "Duplicate", "Category mismatch", "Location mismatch", "Work format mismatch", "Closed/deadline", "Salary below threshold", "Fit score", "API error"],
        )
    with dbg2:
        debug_limit = st.number_input("Rows", min_value=50, max_value=2000, value=300, step=50)
    with dbg3:
        if st.button("Clear rejected-job debug"):
            removed = clear_rejected_jobs(conn)
            st.warning(f"Cleared {removed} rejected-job debug records.")
            st.rerun()
    summary_rows = rejected_jobs_summary(conn)
    if summary_rows:
        st.markdown("#### Rejection summary")
        df_summary = pd.DataFrame([dict(x) for x in summary_rows])
        st.dataframe(df_summary, width="stretch", hide_index=True)
    rejected = list_rejected_jobs(conn, limit=int(debug_limit), reason_category=reason_filter)
    if not rejected:
        st.info("No rejected-job records in this view yet. Run a scan to populate this tab.")
    else:
        st.markdown("#### Recent rejected jobs")
        df_rej = pd.DataFrame([dict(x) for x in rejected])
        show_cols = ["created_at", "reason_category", "reason_detail", "score", "title", "company", "location", "source", "apply_url"]
        for col in show_cols:
            if col not in df_rej.columns:
                df_rej[col] = ""
        df_rej_view = df_rej[show_cols].copy()
        # A blank score means the role was rejected before fit scoring, such as
        # by location or duplicate history. Convert to strings so Streamlit/PyArrow
        # does not see mixed float/string values in the same column.
        def _format_debug_score(x):
            if x is None or pd.isna(x):
                return ""
            try:
                xf = float(x)
                if xf.is_integer():
                    return str(int(xf))
                return f"{xf:.1f}"
            except Exception:
                return str(x)

        df_rej_view["score"] = df_rej_view["score"].apply(_format_debug_score).astype(str)
        df_rej_view = df_rej_view.fillna("").astype(str)
        st.dataframe(df_rej_view, width="stretch", hide_index=True)
        csv_bytes = df_rej_view.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Download rejected-job debug CSV",
            data=csv_bytes,
            file_name="jobpilot_rejected_jobs_debug.csv",
            mime="text/csv",
            key="download_rejected_jobs_debug_csv",
            width="stretch",
        )

with tab_reports:
    st.subheader("Application activity report")
    st.caption("Track your job-search activity over time, including monthly application volume, companies, role titles, and detailed applied-job history.")
    summary = activity_summary(conn)
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Total saved", summary.get("total_saved", 0))
    r2.metric("Applied", summary.get("applied", 0))
    r3.metric("Active not applied", summary.get("active_unapplied", 0))
    r4.metric("Favorites", summary.get("favorites", 0))

    if st.button("Generate application activity report", type="primary"):
        counts = monthly_application_counts(conn)
        companies = applications_by_company(conn)
        roles = applications_by_role(conn)
        details = applied_jobs_detail(conn)

        if not counts and not companies and not roles:
            st.info("No applied jobs with application dates yet.")
        else:
            if counts:
                st.markdown("#### Applications by month")
                df_month = pd.DataFrame([dict(x) for x in counts])
                df_month["applications"] = df_month["applications"].astype(int)
                st.bar_chart(df_month.set_index("month"))
                st.dataframe(df_month, width="stretch", hide_index=True)
                st.download_button(
                    "Download monthly report CSV",
                    data=df_month.to_csv(index=False).encode("utf-8"),
                    file_name="jobpilot_monthly_application_report.csv",
                    mime="text/csv",
                )

            col_company, col_role = st.columns(2)
            with col_company:
                st.markdown("#### Companies applied to")
                if companies:
                    df_company = pd.DataFrame([dict(x) for x in companies])
                    df_company["applications"] = df_company["applications"].astype(int)
                    st.bar_chart(df_company.set_index("company")["applications"])
                    st.dataframe(df_company, width="stretch", hide_index=True)
                    st.download_button(
                        "Download company report CSV",
                        data=df_company.to_csv(index=False).encode("utf-8"),
                        file_name="jobpilot_company_application_report.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("No applied-company data yet.")
            with col_role:
                st.markdown("#### Role titles applied to")
                if roles:
                    df_role = pd.DataFrame([dict(x) for x in roles])
                    df_role["applications"] = df_role["applications"].astype(int)
                    st.bar_chart(df_role.set_index("role_title")["applications"])
                    st.dataframe(df_role, width="stretch", hide_index=True)
                    st.download_button(
                        "Download role report CSV",
                        data=df_role.to_csv(index=False).encode("utf-8"),
                        file_name="jobpilot_role_application_report.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("No applied-role data yet.")

            st.markdown("#### Applied-job details")
            if details:
                df_detail = pd.DataFrame([dict(x) for x in details])
                st.dataframe(df_detail, width="stretch", hide_index=True)
                st.download_button(
                    "Download applied-job details CSV",
                    data=df_detail.to_csv(index=False).encode("utf-8"),
                    file_name="jobpilot_applied_jobs_detail.csv",
                    mime="text/csv",
                )
            else:
                st.info("No applied-job details yet.")

with tab_architecture:
    st.subheader("Architecture page")
    st.caption("A recruiter-friendly explanation of how JobPilot AI works.")
    st.markdown(
        """
        **Agent pipeline**

        `Search Agent → Fast Source Router → Filter Agent → Fit Agent → Tracker Agent → On-demand Research/Contact Agent → Email Agent`

        - **Search Agent:** queries public job sources, SerpAPI/Google Jobs-style results, and public ATS boards.
        - **Filter Agent:** removes duplicates, volunteer/unpaid roles, roles requiring 5+ years, location mismatches, closed/past-deadline postings, and salary-threshold failures.
        - **Fit Agent:** compares the role description against the uploaded resume and assigns a fit score.
        - **Research Agent:** looks for company-specific details so outreach does not sound generic.
        - **Contact Agent:** finds public recruiter/contact routes and CEO/founder routes when available.
        - **Email Agent:** drafts editable outreach emails but never sends them automatically.
        - **Tracker Agent:** saves applied/favorite/status/follow-up history in SQLite.
        """
    )
    st.info("The system is intentionally human-in-the-loop: it recommends, drafts, and tracks, but the user reviews applications and sends emails manually.")

st.divider()
st.caption("This app does not auto-apply or auto-send emails. It drafts outreach for user review and uses public/ethical job sources.")
