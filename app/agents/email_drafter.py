from __future__ import annotations
import re
from app.models import CandidateProfile, JobPosting, FitResult, CompanyResearch, ContactCandidate
from app.utils.llm import call_openai_text
from app.utils.text_cleaning import clean_job_text, normalize_plain_text


def role_focus(job: JobPosting) -> str:
    text = f"{job.title} {clean_job_text(job.description)}".lower()
    if any(x in text for x in ["agentic", "llm", "rag", "openai", "genai", "generative ai"]):
        return "agentic AI and LLM systems"
    if any(x in text for x in ["software", "backend", "full stack", "java", "react", "api", "frontend", "typescript"]):
        return "software development and practical application building"
    if any(x in text for x in ["security", "cyber", "adversarial", "threat", "soc"]):
        return "cybersecurity and secure AI systems"
    if any(x in text for x in ["cloud", "devops", "aws", "azure", "gcp"]):
        return "cloud, APIs, and deployment workflows"
    if any(x in text for x in ["help desk", "technical support", "it support"]):
        return "technical support and troubleshooting"
    if any(x in text for x in ["tutor", "teaching", "instructor", "trainer"]):
        return "teaching, tutoring, and technical instruction"
    return "the technical skills described in my resume"


def _with_article(title: str) -> str:
    title = normalize_plain_text(title)
    if not title:
        return "a candidate with a technical background"
    lower = title.lower()
    if lower.startswith(("a ", "an ", "the ", "i ")):
        return title
    if lower.startswith(("msc", "mba", "ma ", "ms ", "ml ", "ai ")):
        return f"an {title}"
    if re.match(r"^[aeiou]", lower):
        return f"an {title}"
    return f"a {title}"


def _candidate_intro(profile: CandidateProfile) -> str:
    name = normalize_plain_text(profile.name) or "Candidate"
    title = _with_article(profile.current_title)
    focus = normalize_plain_text(profile.focus)
    if focus:
        return f"My name is {name}, and I am {title} with a focus on {focus}"
    return f"My name is {name}, and I am {title}"


def _signature(profile: CandidateProfile) -> str:
    lines = ["Best regards,", normalize_plain_text(profile.name) or "Candidate"]
    affiliation = normalize_plain_text(getattr(profile, "affiliation", ""))
    if affiliation:
        lines.append(affiliation)
    elif profile.current_title:
        lines.append(normalize_plain_text(profile.current_title))
    if profile.email:
        lines.append(f"Email: {normalize_plain_text(profile.email)}")
    if profile.portfolio:
        lines.append(f"Portfolio: {normalize_plain_text(profile.portfolio)}")
    if profile.scholar:
        lines.append(f"Additional profile: {normalize_plain_text(profile.scholar)}")
    return "\n".join(lines)


def _resume_evidence(profile: CandidateProfile, fit: FitResult, max_items: int = 4) -> list[str]:
    items: list[str] = []
    for skill in list(fit.matched_skills) + list(profile.core_skills):
        s = normalize_plain_text(skill)
        if s and s.lower() not in {x.lower() for x in items}:
            items.append(s)
        if len(items) >= max_items:
            break
    return items[:max_items]


def _extract_requirement_summary(job: JobPosting, fit: FitResult, max_items: int = 4) -> list[str]:
    text = clean_job_text(job.description)
    items: list[str] = []
    for s in list(fit.matched_skills[:3]) + list(fit.missing_skills[:2]):
        cleaned = normalize_plain_text(s)
        if cleaned and len(cleaned) <= 60 and cleaned.lower() not in {x.lower() for x in items}:
            items.append(cleaned)
        if len(items) >= max_items:
            return items[:max_items]
    terms = [
        "TypeScript", "JavaScript", "React", "Next.js", "Node.js", "Python", "Java", "SQL",
        "REST APIs", "frontend", "backend", "open source", "developer tools", "LLM", "RAG",
        "NLP", "machine learning", "PyTorch", "TensorFlow", "AWS", "Azure", "GCP",
        "cybersecurity", "CI/CD", "GitHub Actions", "Docker", "Kubernetes", "teaching", "tutoring",
    ]
    lower = text.lower()
    for term in terms:
        if term.lower() in lower and term.lower() not in {x.lower() for x in items}:
            items.append(term)
        if len(items) >= max_items:
            break
    return items[:max_items]


def _usable_company_detail(research: CompanyResearch, job: JobPosting) -> str:
    detail = normalize_plain_text(research.unique_detail)
    if not detail:
        return ""
    bad_signals = [
        "jobs at", "recently posted jobs", "job openings", "view all jobs", "cookie", "privacy notice",
        "equal opportunity", "candidate privacy", "apply now", "we use cookies", "company research pending",
        "your company mission", "terms of service", "all rights reserved",
    ]
    if any(b in detail.lower() for b in bad_signals):
        return ""
    if len(detail) < 45:
        return ""
    if len(detail) > 220:
        detail = detail[:217].rsplit(" ", 1)[0] + "..."
    return detail


def _recipient(contact: ContactCandidate | None) -> str:
    name = normalize_plain_text(contact.name) if contact else ""
    if name and name not in {"General Hiring Contact", "Public Contact", "Hiring Team"}:
        return name
    return "[Recruiter's name]"


def _clean_final_email(text: str, job: JobPosting, profile: CandidateProfile, contact: ContactCandidate | None) -> str:
    text = clean_job_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Make placeholders explicit with brackets.
    text = re.sub(r"\bDear Recruiter Name\b", "Dear [Recruiter's name]", text, flags=re.I)
    text = re.sub(r"\bHi Recruiter Name\b", "Hi [Recruiter's name]", text, flags=re.I)
    text = re.sub(r"\bDear Hiring Team\b", "Hi [Recruiter's name]", text, flags=re.I)
    text = re.sub(r"\bHi Hiring Team\b", "Hi [Recruiter's name]", text, flags=re.I)
    if not text.lower().startswith("subject:"):
        text = f"Subject: Interest in {normalize_plain_text(job.title)} at {normalize_plain_text(job.company)}\n\n{text}"
    # Simple spelling/coherency cleanups observed in generated drafts.
    fixes = {
        "avaialbe": "available",
        "drfat": "draft",
        "comapny": "company",
        "especific": "specific",
        "especial": "special",
    }
    for a, b in fixes.items():
        text = re.sub(a, b, text, flags=re.I)
    # Keep the draft brief. Preserve signature when possible.
    words = text.split()
    if len(words) > 210:
        # Conservative truncation before signature is risky; instead keep fallback if the LLM over-writes.
        return _fallback_email(profile, job, FitResult(0, "Maybe", [], [], [], ""), CompanyResearch(job.company, ""), contact)
    return text.strip() + "\n"


def _fallback_email(profile: CandidateProfile, job: JobPosting, fit: FitResult, research: CompanyResearch, contact: ContactCandidate | None = None) -> str:
    recipient = _recipient(contact)
    focus = role_focus(job)
    company_detail = _usable_company_detail(research, job)
    evidence = _resume_evidence(profile, fit)
    requirements = _extract_requirement_summary(job, fit)
    intro = _candidate_intro(profile)

    first_para = f"I hope you are doing well. {intro}. I am interested in the {normalize_plain_text(job.title)} position at {normalize_plain_text(job.company)}."
    if company_detail:
        detail_sentence = company_detail.rstrip(" .") + "."
        first_para += f" I noticed that {detail_sentence}"
    else:
        first_para += " I reviewed the role and the company materials available through the posting."

    if requirements:
        role_sentence = "The role seems to emphasize " + ", ".join(requirements[:3]) + "."
    else:
        role_sentence = "The role seems to emphasize practical technical work and clear collaboration."

    if evidence:
        evidence_sentence = "My background includes " + ", ".join(evidence[:4]) + ", which I believe is relevant without overstating my experience."
    else:
        evidence_sentence = f"My background in {focus} appears relevant, and I would be happy to discuss the fit."

    email = f"""Subject: Interest in {normalize_plain_text(job.title)} at {normalize_plain_text(job.company)}

Hi {recipient},

{first_para}

{role_sentence} {evidence_sentence}

I have attached my resume for your consideration. Thank you for your time.

{_signature(profile)}
"""
    return _clean_final_email(email, job, profile, contact)


def draft_email(profile: CandidateProfile, job: JobPosting, fit: FitResult, research: CompanyResearch, contact: ContactCandidate | None = None, openai_api_key: str | None = None) -> str:
    job = JobPosting(**job.to_dict())
    job.title = normalize_plain_text(job.title)
    job.company = normalize_plain_text(job.company)
    job.location = normalize_plain_text(job.location)
    job.description = clean_job_text(job.description)
    fallback = _fallback_email(profile, job, fit, research, contact)
    if not openai_api_key:
        return fallback

    company_detail = _usable_company_detail(research, job)
    recipient = _recipient(contact)
    req_items = _extract_requirement_summary(job, fit, max_items=6)
    evidence_items = _resume_evidence(profile, fit, max_items=8)
    affiliation = normalize_plain_text(getattr(profile, "affiliation", ""))

    system = """You write concise, human-sounding recruiter outreach emails for job applications.
Rules:
- Use the uploaded resume, the job description, and the company detail.
- First paragraph must introduce the candidate and mention one specific company detail if provided.
- Do not paste raw job-description text or search-result snippets.
- Do not exaggerate qualifications or claim direct experience that is not shown.
- Use simple sentences and a natural tone.
- The email must take less than 45 seconds to read, around 120-170 words excluding signature.
- If no recruiter name is known, use exactly: Hi [Recruiter's name],
- Return a complete email with a Subject line.
- Review grammar, structure, coherence, and spelling before returning."""
    user = f"""
Candidate profile/resume:
Name: {profile.name}
Title/background: {profile.current_title}
Affiliation for signature: {affiliation}
Focus/interests: {profile.focus}
Email: {profile.email}
Portfolio/profile link: {profile.portfolio}
Additional link: {profile.scholar}
Detected resume skills: {', '.join(profile.core_skills[:60])}
Detected projects: {profile.projects}
Resume excerpt: {normalize_plain_text(profile.resume_text)[:4000]}

Job:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description[:4500]}

Fit result:
Score: {fit.score}
Decision: {fit.decision}
Candidate evidence to consider: {', '.join(evidence_items)}
Potential gaps/concerns to avoid overstating: {'; '.join((fit.missing_skills + fit.concerns)[:8])}
Important requirements to summarize: {', '.join(req_items)}

Company detail to mention only if meaningful and specific:
{company_detail or 'No reliable specific company detail available; say you reviewed the role and company materials but do not invent details.'}
Source: {research.source_url or 'not available'}

Recipient line:
Hi {recipient},
"""
    generated = call_openai_text(system, user, api_key=openai_api_key, timeout=35)
    if not generated or len(generated.strip()) < 80:
        return fallback
    return _clean_final_email(generated, job, profile, contact)
