from __future__ import annotations
import json
import re
from rapidfuzz import fuzz
from app.models import CandidateProfile, JobPosting, FitResult
from app.utils.llm import call_openai_json

VOLUNTEER_PATTERNS = [r"\bvolunteer\b", r"\bunpaid\b", r"\bno compensation\b", r"\bwithout compensation\b", r"\binternship\s*\(unpaid\)"]
SENIOR_ONLY_PATTERNS = [r"\bsenior\b", r"\bstaff\b", r"\bprincipal\b", r"\blead\b", r"\bmanager\b", r"\bdirector\b"]
TEACHING_TERMS = ["teaching assistant", "tutor", "instructor", "teaching", "education", "trainer", "coding teacher"]
CS_TERMS = [
    "software", "developer", "engineer", "programmer", "coding", "web", "backend", "frontend",
    "full stack", "api", "open source", "github", "devrel", "developer relations",
    "machine learning", "ml", "artificial intelligence", "ai", "generative ai", "llm", "rag",
    "nlp", "computer vision", "data", "analytics", "cloud", "devops", "aws", "azure", "gcp",
    "cybersecurity", "security", "trustworthy ai", "ai safety", "research", "technical",
    "help desk", "it support", "technical support", "product manager", "technical product"
]

DEFAULT_SKILLS = [
    "python", "java", "kotlin", "javascript", "sql", "c++", "pytorch", "tensorflow", "scikit-learn",
    "machine learning", "ai security", "adversarial machine learning", "cybersecurity", "aws", "azure",
    "google cloud", "fastapi", "flask", "react", "streamlit", "docker", "rag", "llmops",
    "agentic design", "openai api", "teaching", "tutoring", "git", "github",
    "typescript", "html", "css", "node", "next.js", "kubernetes", "linux",
    "technical support", "help desk", "data analyst", "business intelligence"
]


def _text(job: JobPosting) -> str:
    return f"{job.title} {job.department} {job.location} {job.employment_type} {job.description}".lower()


def extract_required_years(text: str) -> int | None:
    patterns = [
        r"(\d+)\+?\s*(?:years|yrs)",
        r"minimum\s+of\s+(\d+)\s*(?:years|yrs)",
        r"at least\s+(\d+)\s*(?:years|yrs)",
        r"(\d+)\s*-\s*(\d+)\s*(?:years|yrs)",
    ]
    years = []
    for p in patterns:
        for match in re.finditer(p, text, flags=re.I):
            nums = [int(x) for x in match.groups() if x and x.isdigit()]
            if nums:
                years.append(max(nums))
    return max(years) if years else None


def is_relevant_domain(text: str, profile: CandidateProfile, selected_categories: list[str] | None = None) -> bool:
    if any(term in text for term in CS_TERMS):
        return True
    if profile.include_teaching and any(term in text for term in TEACHING_TERMS):
        return True
    return False


def hard_filter(job: JobPosting, profile: CandidateProfile) -> tuple[bool, list[str]]:
    t = _text(job)
    reasons = []
    if profile.exclude_volunteer and any(re.search(p, t) for p in VOLUNTEER_PATTERNS):
        reasons.append("Excluded because it appears volunteer/unpaid.")
    years = extract_required_years(t)
    if years is not None and years >= 5:
        reasons.append(f"Excluded because it appears to require {years}+ years of experience.")
    if any(re.search(p, t) for p in SENIOR_ONLY_PATTERNS) and years is not None and years >= 4:
        reasons.append("Excluded because title/description suggests a senior role with high experience expectations.")
    if not is_relevant_domain(t, profile):
        reasons.append("Excluded because it is not clearly CS/AI/software/security/cloud/data/help-desk or teaching/tutoring related.")
    return (len(reasons) == 0), reasons


def _rule_score_job(job: JobPosting, profile: CandidateProfile) -> FitResult:
    t = _text(job)
    ok, filter_reasons = hard_filter(job, profile)
    if not ok:
        return FitResult(0, "Skip", [], [], filter_reasons, "Hard filter exclusion: " + "; ".join(filter_reasons))

    resume_text = (profile.resume_text or "").lower()
    skills = sorted(set(profile.core_skills or DEFAULT_SKILLS))
    matched = []
    evidence_bonus = 0
    for skill in skills:
        if skill in t or fuzz.partial_ratio(skill, t) >= 88:
            matched.append(skill)
            if skill in resume_text:
                evidence_bonus += 1

    project_hits = []
    for project, kws in (profile.projects or {}).items():
        if any(kw.lower() in t for kw in kws):
            project_hits.append(project)

    missing = []
    likely_required = ["python", "java", "javascript", "sql", "aws", "azure", "docker", "react", "fastapi", "pytorch", "machine learning", "customer support"]
    for req in likely_required:
        if req in t and req not in matched:
            missing.append(req)

    years = extract_required_years(t)
    concerns = []
    if years and years > 3:
        concerns.append(f"Requires around {years} years; still below hard exclusion threshold but may be competitive.")
    if any(re.search(p, t) for p in SENIOR_ONLY_PATTERNS):
        concerns.append("Title/description may signal seniority; review manually.")

    role_score = 25 if is_relevant_domain(t, profile) else 0
    skill_score = min(42, len(matched) * 4)
    evidence_score = min(13, evidence_bonus + len(project_hits) * 4)
    years_score = 15 if years is None or years <= 2 else 10 if years <= 4 else 0
    relocation_score = 5 if profile.open_to_relocation else 2
    score = min(100, role_score + skill_score + evidence_score + years_score + relocation_score)
    decision = "Apply" if score >= 72 else "Maybe" if score >= 55 else "Skip"
    if decision == "Skip":
        concerns.append("Low resume/job evidence match. Kept only if user lowers minimum score.")
    extra = f" Project evidence: {', '.join(project_hits)}." if project_hits else ""
    rationale = f"{decision}: matched {len(matched)} resume skills; years requirement={years or 'not specified'}.{extra}"
    return FitResult(score, decision, matched[:14] + project_hits[:3], missing[:8], concerns, rationale)


def _llm_refine_score(job: JobPosting, profile: CandidateProfile, base: FitResult, openai_api_key: str | None) -> FitResult:
    """Optional OpenAI refinement. Hard filters have already run; the LLM can refine but not override exclusions."""
    if not openai_api_key or base.decision == "Skip":
        return base
    system = """You are a careful recruiting fit evaluator. Return only JSON. Do not include markdown.
Never recommend applying to unpaid/volunteer roles or roles requiring 5+ years of experience.
Score realistically for an MSc candidate with projects/certifications but limited full-time industry experience.
JSON schema: {"score": integer 0-100, "decision": "Apply"|"Maybe"|"Skip", "matched_skills": [string], "missing_skills": [string], "concerns": [string], "rationale": string}."""
    resume_excerpt = (profile.resume_text or "")[:6000]
    job_excerpt = f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\nDescription:\n{job.description[:6000]}"
    user = f"Candidate resume excerpt:\n{resume_excerpt}\n\nJob posting:\n{job_excerpt}\n\nRule-based result:\n{json.dumps(base.to_dict(), ensure_ascii=False)}"
    data = call_openai_json(system, user, api_key=openai_api_key, timeout=35)
    if not data:
        return base
    try:
        score = int(data.get("score", base.score))
        decision = str(data.get("decision", base.decision))
        if decision not in {"Apply", "Maybe", "Skip"}:
            decision = base.decision
        # Conservative guardrails.
        if score < 0 or score > 100:
            score = base.score
        if decision == "Apply" and score < 65:
            decision = "Maybe"
        return FitResult(
            score=score,
            decision=decision,
            matched_skills=list(data.get("matched_skills") or base.matched_skills)[:16],
            missing_skills=list(data.get("missing_skills") or base.missing_skills)[:10],
            concerns=list(data.get("concerns") or base.concerns)[:8],
            rationale=str(data.get("rationale") or base.rationale),
        )
    except Exception:
        return base


def score_job(job: JobPosting, profile: CandidateProfile, openai_api_key: str | None = None, use_llm: bool = True) -> FitResult:
    base = _rule_score_job(job, profile)
    if not use_llm:
        return base
    return _llm_refine_score(job, profile, base, openai_api_key)
