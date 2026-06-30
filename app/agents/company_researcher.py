from __future__ import annotations
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from app.models import CompanyResearch, ContactCandidate

HEADERS = {"User-Agent": "JobPilotAI/0.5 (+portfolio project; respectful public page fetch)"}


def fetch_public_page(url: str, timeout: int = 15) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)


def _candidate_urls(company_url: str) -> list[str]:
    base = company_url if company_url.startswith("http") else f"https://{company_url}"
    paths = [
        "", "/about", "/careers", "/jobs", "/news", "/blog", "/press", "/team",
        "/company", "/contact", "/leadership", "/people", "/our-team", "/management",
    ]
    return [urljoin(base.rstrip("/") + "/", p.lstrip("/")) for p in paths]


def _extract_public_emails(text: str) -> list[str]:
    email_pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    blocked_fragments = ["example.com", "domain.com", "email.com", "sentry", "wixpress", "wordpress", "schema.org"]
    emails = []
    for email in sorted(set(re.findall(email_pattern, text or ""))):
        lower = email.lower()
        if any(b in lower for b in blocked_fragments):
            continue
        emails.append(email)
    return emails[:15]


def summarize_company_from_text(company: str, text: str, source_url: str | None = None) -> CompanyResearch:
    text = re.sub(r"\s+", " ", text or " ").strip()
    lower_all = text.lower()
    bad_page_signals = [
        "jobs at", "recently posted jobs", "open positions", "job openings",
        "apply now", "equal opportunity", "candidate privacy", "cookie",
        "senior research engineer", "product manager", "view all jobs",
    ]
    # If Tavily/search returns a careers listing snippet, do not use it as a
    # "unique company detail" in outreach emails.
    if any(x in lower_all for x in bad_page_signals) and not any(x in lower_all for x in ["announced", "launched", "mission", "platform", "product"]):
        return CompanyResearch(company=company, unique_detail="", source_url=source_url, confidence="low")

    sentences = re.split(r"(?<=[.!?])\s+", text)
    priority_terms = ["announced", "launch", "launched", "new", "platform", "product", "mission", "research", "AI", "cloud", "security", "customers", "helps", "builds"]
    candidates = []
    for s in sentences:
        s = re.sub(r"\s+", " ", s).strip()
        sl = s.lower()
        if any(b in sl for b in bad_page_signals):
            continue
        if 70 <= len(s) <= 240 and any(term.lower() in sl for term in priority_terms):
            candidates.append(s)
    if candidates:
        candidates = sorted(candidates, key=lambda x: ("copyright" in x.lower(), "privacy" in x.lower(), len(x)))
        return CompanyResearch(company=company, unique_detail=candidates[0], source_url=source_url, confidence="medium")
    return CompanyResearch(company=company, unique_detail="", source_url=source_url, confidence="low")

def research_company(company: str, company_url: str | None = None, tavily_api_key: str | None = None) -> CompanyResearch:
    tavily_key = tavily_api_key or os.getenv("TAVILY_API_KEY", "")
    if tavily_key:
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily_key, "query": f"{company} recent product launch mission AI software product news -jobs -careers", "max_results": 5},
                timeout=20,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            for item in results:
                content = item.get("content", "")
                if len(content) > 80:
                    return summarize_company_from_text(company, content, item.get("url"))
        except Exception:
            pass
    if company_url:
        for url in _candidate_urls(company_url)[:10]:
            try:
                result = summarize_company_from_text(company, fetch_public_page(url), url)
                if result.confidence != "low":
                    return result
            except Exception:
                continue
    return CompanyResearch(company=company, unique_detail="your company mission and recent public updates", confidence="low")


def find_contacts_from_text(company: str, text: str, source_url: str | None = None, role_title: str = "") -> list[ContactCandidate]:
    contacts: list[ContactCandidate] = []
    for email in _extract_public_emails(text):
        lower = email.lower()
        if any(x in lower for x in ["career", "talent", "recruit", "hr", "people", "jobs"]):
            confidence = "high"
            title = "Careers/Recruiting Email"
        elif any(x in lower for x in ["info", "contact", "hello"]):
            confidence = "medium"
            title = "General Company Contact"
        else:
            confidence = "low"
            title = "Public Company Email"
        contacts.append(ContactCandidate(
            name="General Hiring Contact" if confidence != "low" else "Public Contact",
            title=title,
            email=email,
            contact_url=source_url,
            reason="Public email found on company page. Verify before sending outreach.",
            confidence=confidence,
        ))
    title_pattern = r"([A-Z][a-zA-Z'\-]+\s+[A-Z][a-zA-Z'\-]+)[^\.\n]{0,100}(Recruiter|Talent Acquisition|People Operations|HR|Human Resources|Engineering Manager|Head of People|Hiring Manager)"
    for name, title in re.findall(title_pattern, text):
        contacts.append(ContactCandidate(
            name=name,
            title=title,
            contact_url=source_url,
            reason=f"Public page mentions this person/title; likely relevant to {role_title or 'hiring'} but verify manually.",
            confidence="low" if "Manager" in title else "medium",
        ))
    return contacts[:8]


def find_contacts_from_company_site(company: str, company_url: str | None, role_title: str = "") -> list[ContactCandidate]:
    if not company_url:
        return []
    all_contacts: list[ContactCandidate] = []
    for url in _candidate_urls(company_url):
        try:
            all_contacts.extend(find_contacts_from_text(company, fetch_public_page(url), url, role_title))
        except Exception:
            continue
    seen = set()
    unique = []
    for c in all_contacts:
        key = (c.email or c.name or "", c.title)
        if key not in seen:
            unique.append(c)
            seen.add(key)
    return unique[:8]


def _extract_ceo_from_text(company: str, text: str, source_url: str | None = None) -> ContactCandidate | None:
    cleaned = re.sub(r"\s+", " ", text or "")
    emails = _extract_public_emails(cleaned)
    # Match common leadership-page patterns. This does not invent emails.
    patterns = [
        r"([A-Z][a-zA-Z'\-]+\s+[A-Z][a-zA-Z'\-]+)[^\.]{0,120}(Chief Executive Officer|CEO|Founder|Co-Founder|Co-founder)",
        r"(Chief Executive Officer|CEO|Founder|Co-Founder|Co-founder)[^\.]{0,80}([A-Z][a-zA-Z'\-]+\s+[A-Z][a-zA-Z'\-]+)",
    ]
    for p in patterns:
        m = re.search(p, cleaned)
        if not m:
            continue
        groups = list(m.groups())
        if len(groups) >= 2 and any("CEO" in g or "Chief" in g or "Founder" in g for g in groups):
            name = groups[0] if not any(x in groups[0] for x in ["CEO", "Chief", "Founder"]) else groups[1]
            title = groups[1] if name == groups[0] else groups[0]
            # Only attach an email if it is clearly public on the same page. Do not infer personal email patterns.
            email = None
            if emails:
                # Prefer leadership/general contact emails. Avoid assigning random support address as personal CEO email.
                for e in emails:
                    if any(x in e.lower() for x in ["founder", "ceo", name.split()[0].lower()]):
                        email = e
                        break
            return ContactCandidate(
                name=name.strip(),
                title=title.strip(),
                email=email,
                contact_url=source_url,
                reason="Public company/search page appears to identify this person as CEO/founder. Verify before outreach.",
                confidence="medium",
            )
    return None


def find_ceo_contact(company: str, company_url: str | None = None, tavily_api_key: str | None = None) -> ContactCandidate | None:
    """Find a public CEO/founder contact route. It never fabricates private email addresses."""
    tavily_key = tavily_api_key or os.getenv("TAVILY_API_KEY", "")
    if tavily_key:
        queries = [
            f"{company} CEO founder leadership",
            f"{company} chief executive officer contact",
        ]
        for q in queries:
            try:
                r = requests.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": q, "max_results": 5},
                    timeout=20,
                )
                r.raise_for_status()
                for item in r.json().get("results", []) or []:
                    content = item.get("content", "")
                    url = item.get("url")
                    c = _extract_ceo_from_text(company, content, url)
                    if c:
                        return c
            except Exception:
                continue
    if company_url:
        for url in _candidate_urls(company_url):
            try:
                c = _extract_ceo_from_text(company, fetch_public_page(url), url)
                if c:
                    return c
            except Exception:
                continue
    return None
