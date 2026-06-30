from __future__ import annotations
import os
import re
import time
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable
try:
    from dateutil import parser as date_parser
except Exception:
    date_parser = None
from app.config import JOB_CATEGORIES, ScanSettings
from app.models import CandidateProfile, JobPosting, ContactCandidate, CompanyResearch, FitResult
from app.connectors.serpapi_jobs import fetch_serpapi_jobs, iter_serpapi_jobs, optimize_queries_for_speed
from app.connectors.ats_discovery import fetch_default_ats_jobs
from app.connectors.remotive import fetch_remotive_jobs
from app.connectors.remoteok import fetch_remoteok_jobs
from app.agents.fit_scorer import score_job
from app.agents.company_researcher import research_company, find_contacts_from_company_site, find_ceo_contact
from app.storage.job_store import connect, upsert_job, job_was_seen, mark_seen, add_activity_log, record_rejected_job, get_job
from app.utils.salary import job_meets_salary_threshold
from app.utils.text_cleaning import clean_job_text, normalize_plain_text


_PROCESS_LOCK = threading.Lock()
_PROCESS_STOP_EVENT = threading.Event()
_PROCESS_THREAD: threading.Thread | None = None
_PROCESS_RUN_ID: str | None = None


def process_background_thread_alive() -> bool:
    global _PROCESS_THREAD
    return bool(_PROCESS_THREAD and _PROCESS_THREAD.is_alive())


def scanner_is_active(db_path: str, stale_after_seconds: int = 300) -> bool:
    """Return True when a current-process or recently reported scanner is active."""
    if process_background_thread_alive():
        return True
    status = read_scanner_status(db_path)
    updated_at = float(status.get("updated_at") or 0)
    recent = (time.time() - updated_at) < stale_after_seconds
    return bool(status.get("running") and recent)


def build_queries(categories: list[str]) -> list[str]:
    queries: list[str] = []
    for cat in categories:
        queries.extend(JOB_CATEGORIES.get(cat, []))
    return list(dict.fromkeys(queries))




def build_fast_queries(categories: list[str], depth: str = "Fast") -> list[str]:
    """Build a high-yield query list for search APIs.

    This keeps scans fast when many categories are selected by avoiding dozens of
    low-yield SerpAPI requests before the first useful row appears.
    """
    return optimize_queries_for_speed(build_queries(categories), depth=depth)


def infer_company_url(job: JobPosting) -> str | None:
    url = job.apply_url or ""
    if not url.startswith("http"):
        return None
    blocked = ["linkedin.", "indeed.", "glassdoor.", "google.", "serpapi.", "greenhouse.io", "lever.co", "ashbyhq.com"]
    if any(b in url.lower() for b in blocked):
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return None


def role_matches_categories(job: JobPosting, categories: list[str]) -> bool:
    text = f"{job.title} {job.department} {job.description}".lower()
    queries = build_queries(categories)
    if not queries:
        return True
    for q in queries:
        tokens = [t.strip('"').lower() for t in q.split() if len(t.strip('"')) > 2]
        if tokens and any(tok in text for tok in tokens):
            return True
    return False


def role_matches_work_format(job: JobPosting, work_formats: list[str] | None) -> bool:
    if not work_formats:
        return True
    text = f"{job.title} {job.location} {job.description} {job.employment_type}".lower()
    if "Remote" in work_formats and re.search(r"\bremote\b|work from home|anywhere", text):
        return True
    if "Hybrid" in work_formats and re.search(r"\bhybrid\b", text):
        return True
    if "On-site" in work_formats and re.search(r"on[- ]?site|in office|office-based|office based", text):
        return True
    if "On-site" in work_formats and not re.search(r"\bremote\b|\bhybrid\b|work from home|anywhere", text):
        return True
    return False


def _normalized_search_locations(locations: list[str] | None) -> list[str]:
    """Convert UI locations into search-engine locations.

    This function was missing in earlier versions, so SerpAPI-enabled scans could
    fail after the ATS pass. The strict location post-filter still decides what
    is actually shown/saved.
    """
    if not locations:
        return ["Canada"]
    out: list[str] = []
    for loc in locations:
        loc = (loc or "").strip()
        if not loc:
            continue
        low = loc.lower()
        if low == "remote - canada":
            candidate = "Canada"
        elif low == "worldwide remote":
            candidate = "Worldwide"
        elif low == "united states":
            candidate = "United States"
        else:
            candidate = loc
        if candidate not in out:
            out.append(candidate)
    return out or ["Canada"]


CANADA_LOCATION_TERMS = [
    "canada", "canadian", "ontario", "on", "toronto", "ottawa", "waterloo", "kitchener",
    "british columbia", "bc", "vancouver", "victoria",
    "quebec", "québec", "qc", "montreal", "montréal",
    "new brunswick", "nb", "fredericton", "moncton", "saint john",
    "nova scotia", "ns", "halifax",
    "alberta", "ab", "calgary", "edmonton",
    "manitoba", "mb", "winnipeg",
    "saskatchewan", "sk", "regina", "saskatoon",
    "newfoundland", "labrador", "nl", "st. john's",
    "prince edward island", "pei", "charlottetown",
    "yukon", "northwest territories", "nunavut",
]

# Terms that usually mean a posting is NOT Canada-eligible when they appear in
# the structured location field. The previous implementation checked the full
# description for "Canada", which caused false positives when a global legal
# footer mentioned Canada even though the actual job location was Tokyo/Japan or
# a US-only remote location.
NON_CANADA_LOCATION_TERMS = [
    # United States / US states and common city signals
    "united states", "usa", "u.s.", "us-only", "u.s. only", "california", "ca", "san francisco", "los angeles",
    "colorado", "co", "denver", "oregon", "or", "portland", "washington", "wa", "seattle",
    "new york", "ny", "texas", "tx", "austin", "dallas", "florida", "fl", "miami",
    "massachusetts", "ma", "boston", "illinois", "il", "chicago", "georgia", "ga",
    "north carolina", "nc", "virginia", "va", "arizona", "az", "utah", "ut",
    # Common non-Canada countries/cities observed from broad ATS/search results
    "japan", "tokyo", "india", "bengaluru", "bangalore", "hyderabad", "delhi", "mumbai",
    "united kingdom", "uk", "london", "germany", "berlin", "france", "paris", "netherlands", "amsterdam",
    "ireland", "dublin", "singapore", "australia", "sydney", "melbourne", "new zealand", "auckland",
    "brazil", "sao paulo", "mexico", "mexico city", "poland", "warsaw", "spain", "madrid", "italy", "milan",
]

REMOTE_GENERIC_TERMS = ["remote", "anywhere", "work from home", "distributed"]
WORLDWIDE_REMOTE_TERMS = ["worldwide", "global", "anywhere", "any country", "remote / worldwide", "remote worldwide"]


def _tokenized_contains(text: str, terms: list[str]) -> bool:
    """Match location tokens without letting abbreviations match inside words."""
    if not text:
        return False
    normalized = re.sub(r"[|;/()\[\],]+", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    padded = f" {normalized} "
    for term in terms:
        t = term.lower().strip()
        if len(t) <= 3 and t.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", normalized):
                return True
        elif t in padded:
            return True
    return False


def _has_canada_signal(text: str) -> bool:
    return _tokenized_contains(text, CANADA_LOCATION_TERMS)


def _has_non_canada_location_signal(text: str) -> bool:
    return _tokenized_contains(text, NON_CANADA_LOCATION_TERMS)


def _location_blob(job: JobPosting) -> str:
    """Return only structured location-like fields, not the full description.

    This avoids a common ATS problem: a job in Tokyo/Japan can include a global
    footer mentioning Canada, which should not make it eligible for a Canada-only
    search.
    """
    parts: list[str] = [job.location or ""]
    raw = job.raw or {}
    for key in ["location", "locations", "job_location", "office", "offices", "address", "formattedLocation"]:
        value = raw.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.extend(str(v) for k, v in item.items() if "location" in k.lower() or k.lower() in {"name", "city", "region", "country"})
        elif isinstance(value, dict):
            parts.extend(str(v) for k, v in value.items() if "location" in k.lower() or k.lower() in {"name", "city", "region", "country"})
    # Google Jobs sometimes puts useful info in extensions, but keep only short
    # location-like strings so descriptions/legal text cannot leak in.
    ext = raw.get("detected_extensions") or raw.get("extensions") or {}
    if isinstance(ext, dict):
        for k, v in ext.items():
            if "location" in str(k).lower() or str(k).lower() in {"work_from_home", "commute_time"}:
                parts.append(str(v))
    return " | ".join(x for x in parts if x).lower()


def _full_text(job: JobPosting) -> str:
    return f"{job.title} {job.location} {job.description} {job.raw}".lower()


def _is_generic_remote_location(location_blob: str) -> bool:
    if not location_blob:
        return False
    return _tokenized_contains(location_blob, REMOTE_GENERIC_TERMS) and not _has_non_canada_location_signal(location_blob)


def _is_worldwide_remote_location(location_blob: str) -> bool:
    if not location_blob:
        return False
    return _tokenized_contains(location_blob, WORLDWIDE_REMOTE_TERMS) and not _has_non_canada_location_signal(location_blob)


def role_matches_location(job: JobPosting, locations: list[str] | None, work_formats: list[str] | None) -> bool:
    """Strict location post-filter for both newly scanned and visible saved jobs.

    If the user selects Canada, the actual structured job location must be
    Canada/Canadian-city/province eligible. A Canada mention in the job
    description or legal footer is not enough. This prevents false positives like
    Tokyo, Japan or Remote - Washington D.C. being shown in a Canada-only search.
    """
    if not locations:
        return True

    selected = {loc.lower().strip() for loc in locations if loc}
    loc_blob = _location_blob(job)
    full_text = _full_text(job)

    canada_selected = bool(selected.intersection({"canada", "remote - canada"}))
    if canada_selected:
        if _has_canada_signal(loc_blob):
            return True
        if _has_non_canada_location_signal(loc_blob):
            return False
        # Worldwide/global remote roles are eligible for a Canada-based user unless
        # a specific non-Canada restriction is present.
        if _is_worldwide_remote_location(loc_blob):
            return True
        # Accept generic remote only when the posting explicitly says Canada in a
        # meaningful eligibility context, not just in a long legal footer. This is
        # deliberately conservative.
        if _is_generic_remote_location(loc_blob) and re.search(r"\b(remote|anywhere|work from home)\b.{0,80}\b(canada|canadian)\b|\b(canada|canadian)\b.{0,80}\b(remote|anywhere|work from home)\b", full_text):
            return True
        return False

    for loc in locations:
        loc_l = loc.lower().strip()
        if loc_l in {"worldwide remote", "remote"}:
            if re.search(r"\bremote\b|anywhere|worldwide", full_text):
                return True
        elif loc_l == "united states":
            if _has_non_canada_location_signal(loc_blob) or "united states" in full_text or "usa" in full_text:
                return True
        else:
            # For city/province selections, prefer structured location fields;
            # only use full text when the structured location is generic remote.
            city = loc_l.split(",")[0].strip()
            province = loc_l.split(",")[-1].strip() if "," in loc_l else ""
            if city and city in loc_blob:
                return True
            if province and _tokenized_contains(loc_blob, [province]):
                return True
            if _is_generic_remote_location(loc_blob) and city and city in full_text:
                return True
    return False


def _state_paths(db_path: str) -> tuple[Path, Path]:
    base = Path(db_path).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / "scanner_status.json", base / "stop_scan.flag"


def _run_path(db_path: str) -> Path:
    base = Path(db_path).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / "active_scanner_run.json"


def set_active_run(db_path: str, run_id: str | None) -> None:
    path = _run_path(db_path)
    payload = {"active_run_id": run_id, "updated_at": time.time()}
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def active_run_matches(db_path: str, run_id: str | None) -> bool:
    if not run_id:
        return True
    path = _run_path(db_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("active_run_id") == run_id
    except Exception:
        return True


def request_stop(db_path: str) -> None:
    status_path, stop_path = _state_paths(db_path)
    stop_path.write_text("stop", encoding="utf-8")
    set_active_run(db_path, None)
    try:
        status = {"running": False, "last_message": "Stop requested — waiting for any active scanner to hit the next checkpoint", "updated_at": time.time()}
        status_path.write_text(json.dumps(status), encoding="utf-8")
    except Exception:
        pass


def clear_stop_request(db_path: str) -> None:
    _, stop_path = _state_paths(db_path)
    try:
        stop_path.unlink(missing_ok=True)
    except Exception:
        pass


def stop_requested(db_path: str) -> bool:
    _, stop_path = _state_paths(db_path)
    return stop_path.exists()


def read_scanner_status(db_path: str) -> dict:
    status_path, _ = _state_paths(db_path)
    if not status_path.exists():
        return {"running": False, "last_message": "Idle"}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "last_message": "Idle"}


def write_scanner_status(db_path: str, running: bool, message: str, result: dict | None = None) -> None:
    status_path, _ = _state_paths(db_path)
    payload = {"running": running, "last_message": message, "last_result": result or {}, "updated_at": time.time()}
    try:
        status_path.write_text(json.dumps(payload), encoding="utf-8"),
    except Exception:
        pass



def application_window_open(job: JobPosting) -> tuple[bool, str]:
    """Best-effort check that the application window is still open.

    If the job has no deadline information, keep it. Only exclude jobs where the
    text clearly says applications are closed or an explicit application deadline
    has passed.
    """
    text = f"{job.title}\n{job.description}\n{job.raw}".lower()
    closed_patterns = [
        r"applications?\s+(are\s+)?closed",
        r"application\s+window\s+(is\s+)?closed",
        r"no\s+longer\s+accepting\s+applications",
        r"position\s+has\s+been\s+filled",
        r"deadline\s+has\s+passed",
    ]
    if any(re.search(p, text) for p in closed_patterns):
        return False, "Explicitly says applications are closed."

    if not date_parser:
        return True, "No date parser available; kept unless explicitly closed."

    # Look only near deadline/closing phrases so posted dates do not cause false exclusions.
    snippets: list[str] = []
    patterns = [
        r"(?:apply by|application deadline|deadline|closing date|applications close|applications accepted until|apply before)[:\s\-]*([^\.\n]{4,80})",
        r"(?:closes on|closing on)[:\s\-]*([^\.\n]{4,80})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            snippets.append(m.group(1))

    now = datetime.now(timezone.utc)
    for snippet in snippets[:5]:
        if any(x in snippet for x in ["open until filled", "until filled", "rolling"]):
            return True, "Deadline says open until filled/rolling."
        try:
            parsed = date_parser.parse(snippet, fuzzy=True)
            if not parsed.tzinfo:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed < now:
                return False, f"Application deadline appears passed: {parsed.date().isoformat()}."
            return True, f"Application deadline appears open: {parsed.date().isoformat()}."
        except Exception:
            continue
    return True, "No explicit passed application deadline found."

def _empty_research(job: JobPosting) -> CompanyResearch:
    return CompanyResearch(
        company=job.company,
        unique_detail="Company research pending. Open the job details or let the scan continue to enrich this record.",
        source_url=None,
        confidence="low",
    )


def _general_contact(job: JobPosting, company_url: str | None) -> ContactCandidate:
    return ContactCandidate(
        name="General Hiring Contact",
        title="Careers/Recruiting Contact",
        email=None,
        contact_url=company_url or job.apply_url or None,
        reason="No specific public recruiter found yet. Use the posting/apply link or company careers/contact route.",
        confidence="low",
    )



def enrich_job_record(job_id: int, db_path: str, tavily_api_key: str | None = None) -> bool:
    """Enrich a saved job on demand with company/contact/CEO research.

    Scans defer this by default for speed. The UI can call this when a user opens
    a specific job, so expensive Tavily/company-page requests are spent only on
    roles the user actually reviews.
    """
    conn = connect(db_path)
    row = get_job(conn, int(job_id))
    if not row:
        return False
    try:
        job = JobPosting(**json.loads(row["job_json"]))
        fit = FitResult(**json.loads(row["fit_json"]))
    except Exception:
        return False
    company_url = infer_company_url(job)
    evidence = row["salary_evidence"] or ""
    try:
        research = research_company(job.company, company_url, tavily_api_key=tavily_api_key)
        contacts = find_contacts_from_company_site(job.company, company_url, job.title) if company_url else []
        contact = contacts[0] if contacts else _general_contact(job, company_url)
        ceo = find_ceo_contact(job.company, company_url, tavily_api_key=tavily_api_key)
        upsert_job(conn, job, fit, research, contact, ceo=ceo, salary_evidence=evidence)
        add_activity_log(conn, "Research Agent", f"Research Agent: enriched {job.company} for {job.title} on demand.", job_id=int(job_id))
        return True
    except Exception as e:
        add_activity_log(conn, "Research Agent", f"Research Agent: on-demand enrichment failed for {job.company}: {e}", level="warning", job_id=int(job_id))
        return False


def scan_once(
    profile: CandidateProfile,
    settings: ScanSettings,
    db_path: str,
    progress_cb: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    is_background: bool = False,
    max_scan_seconds: int = 90,
) -> dict:
    conn = connect(db_path)
    scan_started_at = time.time()
    queries = build_fast_queries(settings.categories, getattr(settings, "search_depth", "Fast"))
    seen = 0
    kept = 0
    inserted = 0
    duplicates_skipped = 0
    category_skipped = 0
    location_skipped = 0
    format_skipped = 0
    closed_skipped = 0
    salary_skipped = 0
    fit_skipped = 0
    researched = 0
    stopped = False
    api_errors = 0
    processed_keys: set[str] = set()
    rejected_debug_logged = 0
    max_rejected_debug_per_scan = 800

    def should_stop() -> bool:
        timed_out = bool(max_scan_seconds and (time.time() - scan_started_at) > max_scan_seconds)
        return bool(timed_out or (stop_check and stop_check()) or stop_requested(db_path))

    def log(msg: str, agent: str = "System", level: str = "info"):
        if progress_cb:
            progress_cb(msg)
        write_scanner_status(db_path, True, msg)
        try:
            add_activity_log(conn, agent, msg, level=level)
        except Exception:
            pass

    def reject(job: JobPosting, category: str, detail: str, score: int | None = None) -> None:
        nonlocal rejected_debug_logged
        if rejected_debug_logged >= max_rejected_debug_per_scan:
            return
        rejected_debug_logged += 1
        try:
            record_rejected_job(conn, job, category, detail, score)
        except Exception:
            pass

    def process_job(job: JobPosting) -> bool:
        """Process one job and insert it as soon as it passes fast filters.

        Returns True when a new job record was inserted/updated. Expensive
        enrichment happens after the quick insert so the user sees rows while
        the scan is still running.
        """
        nonlocal seen, kept, inserted, duplicates_skipped, category_skipped, location_skipped
        nonlocal format_skipped, closed_skipped, salary_skipped, fit_skipped, researched, stopped, api_errors

        if should_stop():
            stopped = True
            return False

        # Normalize fields before filtering/scoring/storage. Some APIs return
        # HTML fragments or HTML entities in descriptions/titles.
        job.title = normalize_plain_text(job.title)
        job.company = normalize_plain_text(job.company)
        job.location = normalize_plain_text(job.location)
        job.description = clean_job_text(job.description)

        if job.source in {"serpapi_error", "remotive_error"}:
            api_errors += 1
            log(f"Search Agent: {job.source} failed for {job.location}: {job.description[:180]}", "Search Agent", level="warning")
            reject(job, "API error", job.description[:250])
            return False
        if job.stable_key in processed_keys:
            duplicates_skipped += 1
            reject(job, "Duplicate", "Same stable job key already processed in this scan.")
            return False
        processed_keys.add(job.stable_key)
        seen += 1
        if job_was_seen(conn, job.stable_key):
            mark_seen(conn, job.stable_key)
            duplicates_skipped += 1
            reject(job, "Duplicate", "This role was already saved, archived, applied, or seen before, so it was not suggested again.")
            return False
        if not role_matches_categories(job, settings.categories):
            category_skipped += 1
            reject(job, "Category mismatch", f"Role did not match selected categories: {', '.join(settings.categories)}.")
            return False
        if not role_matches_location(job, settings.locations, settings.work_formats):
            location_skipped += 1
            reject(job, "Location mismatch", f"Location '{job.location or 'not listed'}' did not match selected locations: {', '.join(settings.locations or [])}.")
            return False
        if not role_matches_work_format(job, settings.work_formats):
            format_skipped += 1
            reject(job, "Work format mismatch", f"Posting did not match selected work formats: {', '.join(settings.work_formats or [])}.")
            return False
        window_ok, window_reason = application_window_open(job)
        if not window_ok:
            closed_skipped += 1
            reject(job, "Closed/deadline", window_reason)
            return False
        salary_ok, salary_reason = job_meets_salary_threshold(
            job,
            min_hourly=float(settings.min_hourly_rate or 0),
            min_annual=int(settings.min_annual_salary or 0),
        )
        if not salary_ok:
            salary_skipped += 1
            reject(job, "Salary below threshold", salary_reason)
            return False

        # Fast scanner rule: do not block the live dashboard on LLM calls.
        # OpenAI is still used for email drafting/detail enrichment, but scanner
        # inserts quickly so records appear as they are found.
        fit = score_job(job, profile, openai_api_key=None, use_llm=False)
        if fit.decision == "Skip" or fit.score < settings.min_score:
            fit_skipped += 1
            reject(job, "Fit score", f"Fit score {fit.score} was below the minimum threshold {settings.min_score}. {fit.rationale}", fit.score)
            return False
        kept += 1
        company_url = infer_company_url(job)
        evidence = "; ".join(x for x in [salary_reason, window_reason] if x)

        # Quick insert first, before Tavily/contact/CEO research.
        quick_inserted = upsert_job(conn, job, fit, _empty_research(job), _general_contact(job, company_url), salary_evidence=evidence)
        if quick_inserted:
            inserted += 1
            log(f"Tracker Agent: added {job.title} at {job.company} to saved jobs.", "Tracker Agent")

        # Stop early after a useful batch has been found. Users can run another
        # scan to continue. This makes the app feel responsive instead of trying
        # to exhaust the whole web in one pass.
        target = int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)
        if target and inserted >= target:
            stopped = True
            return quick_inserted

        if should_stop():
            stopped = True
            return quick_inserted

        # Optional enrichment. Default is deferred for speed; users can enrich a
        # selected job on demand from the details panel.
        if bool(getattr(settings, "enrich_during_scan", False)):
            try:
                research = research_company(job.company, company_url, tavily_api_key=settings.tavily_api_key)
                contacts = find_contacts_from_company_site(job.company, company_url, job.title) if company_url else []
                contact = contacts[0] if contacts else _general_contact(job, company_url)
                ceo = find_ceo_contact(job.company, company_url, tavily_api_key=settings.tavily_api_key)
                researched += 1
                upsert_job(conn, job, fit, research, contact, ceo=ceo, salary_evidence=evidence)
            except Exception as e:
                log(f"Research Agent: enrichment failed for {job.company}: {e}", "Research Agent", level="warning")
        return quick_inserted

    serpapi_key = (settings.serpapi_api_key or os.getenv("SERPAPI_API_KEY", "")).strip()

    # Root-cause fix for low/stuck record counts: when SerpAPI is active, run
    # broad web discovery BEFORE the finite built-in ATS board list. Earlier
    # versions could spend the scan time budget on thousands of ATS postings
    # from the same built-in companies, hit the safety limit, and never explore
    # deeper SerpAPI pages. That made the list appear stuck at a small number.
    if serpapi_key and not should_stop():
        norm_locations = _normalized_search_locations(settings.locations)
        max_requests = int(getattr(settings, "serpapi_max_requests", 60) or 60)
        pages_per_query = int(getattr(settings, "serpapi_pages_per_query", 2) or 2)
        if is_background:
            # Background scans should be lighter than manual scans.
            max_requests = min(max_requests, 40)
        log(
            f"Search Agent: querying SerpAPI first for {', '.join(norm_locations)} | "
            f"categories: {', '.join(settings.categories)} | lookback: {settings.lookback_hours}h | "
            f"request budget: {max_requests}, pages/query: {pages_per_query}.",
            "Search Agent",
        )
        try:
            serp_seen = 0
            for job in iter_serpapi_jobs(
                queries,
                settings.lookback_hours,
                api_key=serpapi_key,
                locations=norm_locations,
                work_formats=settings.work_formats or ["Remote", "Hybrid", "On-site"],
                timeout=5 if getattr(settings, "search_depth", "Fast") == "Fast" else 6,
                stop_check=should_stop,
                max_requests=max_requests,
                pages_per_query=pages_per_query,
                depth=getattr(settings, "search_depth", "Fast"),
            ):
                serp_seen += 1
                process_job(job)
                if should_stop() or (int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) and inserted >= int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
                    stopped = True
                    break
            log(f"Search Agent: SerpAPI streamed {serp_seen} postings in this bounded pass; rows were saved incrementally.", "Search Agent")
        except Exception as e:
            log(f"Search Agent: SerpAPI search failed: {e}", "Search Agent", level="warning")
    elif not serpapi_key:
        log("Search Agent: no SerpAPI key active; skipped broad web job discovery and used built-in ATS boards only.", "Search Agent")

    # Lightweight no-key remote source. This improves coverage for remote roles
    # without extra API keys and is kept bounded for speed.
    if bool(getattr(settings, "enable_remotive", True)) and not should_stop() and (not int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) or inserted < int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
        if settings.work_formats and "Remote" not in settings.work_formats:
            log("Search Agent: skipped Remotive because Remote work format is not selected.", "Search Agent")
        else:
            try:
                rem_queries = queries[:8 if getattr(settings, "search_depth", "Fast") == "Fast" else 14]
                log(f"Search Agent: checking Remotive remote jobs with {len(rem_queries)} optimized queries.", "Search Agent")
                rem_jobs = fetch_remotive_jobs(rem_queries, timeout=6, stop_check=should_stop, max_jobs_per_query=12 if getattr(settings, "search_depth", "Fast") == "Fast" else 20)
                for job in rem_jobs:
                    process_job(job)
                    if should_stop() or (int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) and inserted >= int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
                        stopped = True
                        break
                if not should_stop() and (not int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) or inserted < int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
                    log("Search Agent: checking RemoteOK public remote jobs as a second no-key source.", "Search Agent")
                    for job in fetch_remoteok_jobs(rem_queries, timeout=6, stop_check=should_stop, max_jobs=50 if getattr(settings, "search_depth", "Fast") == "Fast" else 100):
                        process_job(job)
                        if should_stop() or (int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) and inserted >= int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
                            stopped = True
                            break
            except Exception as e:
                log(f"Search Agent: no-key remote source scan failed: {e}", "Search Agent", level="warning")

    # Built-in ATS boards are a useful fallback, but they are finite and often
    # repeat the same jobs on every pass. Keep them bounded so they do not starve
    # SerpAPI or make the scanner look stuck.
    if not should_stop() and (not int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) or inserted < int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)):
        max_boards = int(getattr(settings, "ats_boards_per_provider", 8) or 8)
        log(f"Search Agent: scanning built-in public ATS boards as a bounded fallback ({max_boards} boards/provider).", "Search Agent")
        try:
            for job in fetch_default_ats_jobs(max_boards_per_provider=max_boards):
                process_job(job)
                if should_stop():
                    stopped = True
                    break
        except Exception as e:
            log(f"Search Agent: public ATS scan failed: {e}", "Search Agent", level="warning")

    if should_stop():
        stopped = True
        if max_scan_seconds and (time.time() - scan_started_at) > max_scan_seconds:
            log(f"System: scan reached the {max_scan_seconds}-second safety limit and stopped. Run another scan to continue discovery.", "System", level="warning")

    log(
        f"Filter Agent: removed {duplicates_skipped} duplicates, {category_skipped} category mismatches, "
        f"{location_skipped} location mismatches, {format_skipped} work-format mismatches, "
        f"{closed_skipped} closed/past-deadline roles, and {salary_skipped} salary-threshold failures.",
        "Filter Agent",
    )
    log(f"Fit Agent: fast-scored {seen - duplicates_skipped - category_skipped - location_skipped - format_skipped - closed_skipped - salary_skipped} candidates and kept {kept} roles above threshold.", "Fit Agent")
    log(f"Research Agent: enriched {researched} companies/contact routes. API errors: {api_errors}.", "Research Agent")
    ended_by_time_limit = bool(max_scan_seconds and (time.time() - scan_started_at) > max_scan_seconds)
    result = {
        "seen": seen,
        "kept": kept,
        "inserted_or_updated": inserted,
        "duplicates_skipped": duplicates_skipped,
        "category_skipped": category_skipped,
        "location_skipped": location_skipped,
        "format_skipped": format_skipped,
        "closed_skipped": closed_skipped,
        "salary_skipped": salary_skipped,
        "fit_skipped": fit_skipped,
        "researched": researched,
        "api_errors": api_errors,
        "stopped": stopped,
        "ended_by_time_limit": ended_by_time_limit,
        "new_unique_matches_found": inserted,
        "target_reached": bool(int(getattr(settings, "max_new_jobs_per_scan", 0) or 0) and inserted >= int(getattr(settings, "max_new_jobs_per_scan", 0) or 0)),
    }
    # A manual one-off scan should finish with running=False. A background worker
    # immediately writes running=True again between intervals. Older versions left
    # manual scans marked as running, which made the Stop button/state confusing.
    final_running = bool(is_background and not stopped and not should_stop())
    write_scanner_status(db_path, final_running, f"Last scan: {result}", result)
    return result


@dataclass
class ScannerState:
    running: bool = False
    last_message: str = "Idle"
    last_result: dict = field(default_factory=dict)
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)


def start_background_scan(
    state: ScannerState,
    profile: CandidateProfile,
    settings: ScanSettings,
    db_path: str,
    interval_seconds: int = 60,
    max_runtime_minutes: int = 15,
    stop_after_no_new_passes: int = 2,
):
    """Start exactly one background scanner in this Python process.

    Streamlit reruns can create new session_state objects while an old thread is
    still alive. Earlier versions only checked the session object, which allowed
    duplicate scanners. This function uses module-level process state plus the
    persisted scanner status/run id to avoid more than one active scanner.
    """
    global _PROCESS_THREAD, _PROCESS_RUN_ID
    with _PROCESS_LOCK:
        if process_background_thread_alive() or scanner_is_active(db_path):
            state.running = True
            state.last_message = "A scanner is already running. Stop it before starting another."
            write_scanner_status(db_path, True, state.last_message)
            return state

        run_id = uuid.uuid4().hex
        _PROCESS_RUN_ID = run_id
        set_active_run(db_path, run_id)
        clear_stop_request(db_path)
        state.stop_event.clear()
        _PROCESS_STOP_EVENT.clear()
        state.running = True
        write_scanner_status(db_path, True, "Background scanner starting...")

    def this_worker_should_stop() -> bool:
        return (
            state.stop_event.is_set()
            or _PROCESS_STOP_EVENT.is_set()
            or stop_requested(db_path)
            or not active_run_matches(db_path, run_id)
        )

    def worker():
        global _PROCESS_RUN_ID
        worker_started_at = time.time()
        no_new_passes = 0
        pass_no = 0
        final_message = "Stopped"
        try:
            while not this_worker_should_stop():
                # Background mode is useful for monitoring, but it should never
                # look like an endless stuck process. Auto-stop after a clear
                # runtime cap or after repeated passes with no new saved jobs.
                if max_runtime_minutes and (time.time() - worker_started_at) >= max_runtime_minutes * 60:
                    final_message = f"Background scanner auto-stopped after {max_runtime_minutes} minutes."
                    try:
                        add_activity_log(conn=connect(db_path), agent="System", message=final_message, level="info")
                    except Exception:
                        pass
                    break

                pass_no += 1
                try:
                    state.last_message = f"Background scan pass {pass_no} running..."
                    write_scanner_status(db_path, True, state.last_message)
                    result = scan_once(
                        profile,
                        settings,
                        db_path,
                        lambda m: setattr(state, "last_message", m),
                        stop_check=this_worker_should_stop,
                        is_background=True,
                        max_scan_seconds=120,
                    )
                    state.last_result = result
                    inserted_now = int(result.get("inserted_or_updated") or 0)
                    if inserted_now <= 0:
                        no_new_passes += 1
                    else:
                        no_new_passes = 0
                    state.last_message = f"Background pass {pass_no} complete: {result}"
                    if this_worker_should_stop():
                        final_message = "Stop requested"
                        break
                    if stop_after_no_new_passes and no_new_passes >= stop_after_no_new_passes:
                        final_message = f"Background scanner auto-stopped after {no_new_passes} consecutive pass(es) with no new saved jobs."
                        try:
                            add_activity_log(connect(db_path), "System", final_message, level="info")
                        except Exception:
                            pass
                        break
                    write_scanner_status(db_path, True, state.last_message, result)
                except Exception as e:
                    state.last_message = f"Scan error: {e}"
                    write_scanner_status(db_path, True, state.last_message)

                # Sleep in small chunks so Stop reacts quickly even with long intervals.
                for _ in range(max(1, int(interval_seconds))):
                    if this_worker_should_stop():
                        final_message = "Stop requested"
                        break
                    # Do not let the idle wait push a run far beyond the cap.
                    if max_runtime_minutes and (time.time() - worker_started_at) >= max_runtime_minutes * 60:
                        final_message = f"Background scanner auto-stopped after {max_runtime_minutes} minutes."
                        break
                    time.sleep(1)
        finally:
            state.running = False
            state.last_message = final_message
            if active_run_matches(db_path, run_id):
                set_active_run(db_path, None)
            with _PROCESS_LOCK:
                if _PROCESS_RUN_ID == run_id:
                    _PROCESS_RUN_ID = None
            write_scanner_status(db_path, False, final_message, state.last_result)

    thread = threading.Thread(target=worker, daemon=True, name="jobpilot-background-scanner")
    state.thread = thread
    with _PROCESS_LOCK:
        _PROCESS_THREAD = thread
    thread.start()
    return state


def start_single_scan(state: ScannerState, profile: CandidateProfile, settings: ScanSettings, db_path: str, max_scan_seconds: int = 90):
    """Start exactly one bounded scan without blocking the Streamlit UI.

    Earlier versions used separate scan modes or a modal dialog. With SerpAPI/Tavily
    enabled, that could keep the dialog open for a long time while API calls
    were in progress. This one-off scan uses the same process-level lock/run-id
    mechanism as the background scanner, so rows appear as they are saved and
    the Stop button can interrupt the scan.
    """
    global _PROCESS_THREAD, _PROCESS_RUN_ID
    with _PROCESS_LOCK:
        if process_background_thread_alive() or scanner_is_active(db_path):
            state.running = True
            state.last_message = "A scanner is already running. Stop it before starting another."
            write_scanner_status(db_path, True, state.last_message)
            return state

        run_id = uuid.uuid4().hex
        _PROCESS_RUN_ID = run_id
        set_active_run(db_path, run_id)
        clear_stop_request(db_path)
        state.stop_event.clear()
        _PROCESS_STOP_EVENT.clear()
        state.running = True
        state.last_message = "Scanner starting..."
        write_scanner_status(db_path, True, state.last_message)

    def this_worker_should_stop() -> bool:
        return (
            state.stop_event.is_set()
            or _PROCESS_STOP_EVENT.is_set()
            or stop_requested(db_path)
            or not active_run_matches(db_path, run_id)
        )

    def worker():
        global _PROCESS_RUN_ID
        try:
            state.last_message = "Scanning..."
            write_scanner_status(db_path, True, state.last_message)
            result = scan_once(
                profile,
                settings,
                db_path,
                lambda m: setattr(state, "last_message", m),
                stop_check=this_worker_should_stop,
                is_background=False,
                max_scan_seconds=max_scan_seconds,
            )
            state.last_result = result
            state.last_message = f"Scan complete: {result}"
        except Exception as e:
            state.last_message = f"Scan error: {e}"
            write_scanner_status(db_path, True, state.last_message)
        finally:
            state.running = False
            if active_run_matches(db_path, run_id):
                set_active_run(db_path, None)
            with _PROCESS_LOCK:
                if _PROCESS_RUN_ID == run_id:
                    _PROCESS_RUN_ID = None
            write_scanner_status(db_path, False, state.last_message, state.last_result)

    thread = threading.Thread(target=worker, daemon=True, name="jobpilot-single-scan")
    state.thread = thread
    with _PROCESS_LOCK:
        _PROCESS_THREAD = thread
    thread.start()
    return state


def stop_background_scan(state: ScannerState, db_path: str | None = None):
    global _PROCESS_STOP_EVENT
    state.stop_event.set()
    _PROCESS_STOP_EVENT.set()
    state.running = False
    state.last_message = "Stop requested"
    if db_path:
        request_stop(db_path)
    # If the current session owns the thread, wait briefly so the UI status
    # becomes accurate. API requests may still need a few seconds to return.
    try:
        if state.thread and state.thread.is_alive():
            state.thread.join(timeout=2)
    except RuntimeError:
        pass
