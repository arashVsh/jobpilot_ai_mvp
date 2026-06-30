from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from typing import Iterable
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


def _work_format_query(work_formats: list[str] | None) -> str:
    if not work_formats:
        return ""
    parts = []
    if "Remote" in work_formats:
        parts.append("remote")
    if "Hybrid" in work_formats:
        parts.append("hybrid")
    if "On-site" in work_formats:
        parts.extend(["onsite", '"on-site"'])
    return " (" + " OR ".join(parts) + ")" if parts else ""


def _smart_jobs_query(role_query: str, location: str, work_formats: list[str] | None) -> str:
    """Build a targeted Google Jobs query instead of a broad random web query.

    The goal is to ask for jobs related to the selected industries in the selected
    location, then still apply strict post-filters before saving anything.
    """
    q = str(role_query or "").replace('"', "").strip()
    loc = str(location or "Canada").strip()
    fmt_terms: list[str] = []
    if work_formats:
        if "Remote" in work_formats:
            fmt_terms.append("remote")
        if "Hybrid" in work_formats:
            fmt_terms.append("hybrid")
        if "On-site" in work_formats:
            fmt_terms.append("on-site")
    fmt = " ".join(dict.fromkeys(fmt_terms))
    if loc.lower() in {"worldwide", "worldwide remote"}:
        return f'{q} jobs {fmt} worldwide'.strip()
    if loc.lower().startswith("remote"):
        return f'{q} jobs remote Canada'.strip()
    return f'{q} jobs in {loc} {fmt}'.strip()


def _posted_within_lookback(posted_at: str | None, lookback_hours: int) -> bool:
    """Best-effort filter for Google Jobs relative strings such as '3 hours ago'.
    If no posted string is available, keep the item because exact timestamps are not
    always exposed by Google Jobs/SerpAPI.
    """
    if not posted_at:
        return True
    text = str(posted_at).lower()
    if "just" in text or "today" in text or "hour" in text or "minute" in text:
        match = re.search(r"(\d+)\s*(minute|minutes|hour|hours)", text)
        if not match:
            return True
        value = int(match.group(1))
        unit = match.group(2)
        hours = value / 60 if "minute" in unit else value
        return hours <= lookback_hours
    match = re.search(r"(\d+)\s*(day|days)", text)
    if match:
        return int(match.group(1)) * 24 <= lookback_hours
    match = re.search(r"(\d+)\s*(week|weeks)", text)
    if match:
        return int(match.group(1)) * 24 * 7 <= lookback_hours
    return True


def _item_to_job(item: dict) -> JobPosting:
    apply_options = item.get("apply_options") or []
    apply_url = apply_options[0].get("link", "") if apply_options else item.get("share_link", "")
    detected_extensions = item.get("detected_extensions") or {}
    posted = detected_extensions.get("posted_at") or item.get("via", "")
    return JobPosting(
        title=item.get("title", ""),
        company=item.get("company_name", ""),
        location=item.get("location", ""),
        description=html_to_plain_text(item.get("description", "")),
        apply_url=apply_url,
        source="serpapi_google_jobs",
        posted_date=str(posted) if posted else None,
        discovered_at=datetime.now(timezone.utc).isoformat(),
        raw=item,
    )



def optimize_queries_for_speed(queries: Iterable[str], depth: str = "Fast") -> list[str]:
    """Return a small, high-yield query list.

    Earlier versions sent every category phrase to SerpAPI. With many selected
    categories this could mean dozens of requests before the first row was
    inserted. This keeps the strongest phrases first and caps the list by depth.
    """
    cleaned: list[str] = []
    for q in queries:
        q = str(q or "").strip()
        if not q:
            continue
        # Remove excessive quotes for broader matching while keeping natural phrases.
        q = q.replace('"', '')
        if q.lower() not in [x.lower() for x in cleaned]:
            cleaned.append(q)
    priority_terms = [
        "software engineer", "software developer", "python developer",
        "machine learning engineer", "AI developer", "data scientist",
        "LLM engineer", "RAG developer", "cybersecurity analyst",
        "cloud engineer", "data analyst", "IT support", "programming tutor",
    ]
    ordered = []
    for pt in priority_terms:
        for q in cleaned:
            if pt.lower() in q.lower() and q not in ordered:
                ordered.append(q)
    for q in cleaned:
        if q not in ordered:
            ordered.append(q)
    caps = {"Fast": 8, "Balanced": 14, "Broad": 24, "Deep": 40}
    return ordered[:caps.get(depth, 8)]


def iter_serpapi_jobs(
    queries: Iterable[str],
    lookback_hours: int = 24,
    api_key: str | None = None,
    locations: list[str] | None = None,
    work_formats: list[str] | None = None,
    timeout: int = 6,
    stop_check=None,
    max_requests: int | None = None,
    pages_per_query: int = 1,
    depth: str = "Fast",
):
    """Yield Google Jobs results page-by-page through SerpAPI.

    This is the faster path used by the scanner. It lets the UI insert rows as
    soon as each page returns instead of waiting for all requests to finish.
    """
    key = (api_key or os.getenv("SERPAPI_API_KEY", "")).strip()
    if not key:
        return
    selected_locations = locations or ["Canada"]
    format_clause = _work_format_query(work_formats)
    requests_made = 0
    seen_keys: set[str] = set()
    fast_queries = optimize_queries_for_speed(queries, depth=depth)

    for location in selected_locations:
        for q in fast_queries:
            if stop_check and stop_check():
                return
            next_page_token = None
            for page_idx in range(max(1, int(pages_per_query or 1))):
                if stop_check and stop_check():
                    return
                if max_requests is not None and requests_made >= max_requests:
                    return
                if next_page_token:
                    params = {
                        "engine": "google_jobs",
                        "next_page_token": next_page_token,
                        "api_key": key,
                        "hl": "en",
                    }
                else:
                    smart_query = _smart_jobs_query(q, location, work_formats)
                    params = {
                        "engine": "google_jobs",
                        "q": smart_query,
                        "location": location,
                        "api_key": key,
                        "hl": "en",
                    }
                requests_made += 1
                try:
                    r = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    yield JobPosting(
                        title="__SERPAPI_ERROR__",
                        company="SerpAPI",
                        location=location,
                        description=str(e),
                        source="serpapi_error",
                        raw={"query": q, "location": location, "page": page_idx + 1, "error": str(e)},
                    )
                    break

                for item in data.get("jobs_results", []) or []:
                    posted = (item.get("detected_extensions") or {}).get("posted_at") or item.get("via", "")
                    if not _posted_within_lookback(str(posted) if posted else None, lookback_hours):
                        continue
                    job = _item_to_job(item)
                    key2 = job.stable_key
                    if key2 not in seen_keys:
                        seen_keys.add(key2)
                        yield job

                pag = data.get("serpapi_pagination") or {}
                next_page_token = pag.get("next_page_token")
                if not next_page_token:
                    break


def fetch_serpapi_jobs(
    queries: Iterable[str],
    lookback_hours: int = 24,
    api_key: str | None = None,
    locations: list[str] | None = None,
    work_formats: list[str] | None = None,
    timeout: int = 15,
    stop_check=None,
    max_requests: int | None = None,
    pages_per_query: int = 2,
) -> list[JobPosting]:
    """Fetch Google Jobs results through SerpAPI if a key is configured.

    This function is deliberately defensive: one failed/slow query should not
    block the entire scanner forever. It also follows Google Jobs pagination
    tokens for a small number of pages so a scan is not capped at the first few
    results.
    """
    key = (api_key or os.getenv("SERPAPI_API_KEY", "")).strip()
    if not key:
        return []
    jobs: list[JobPosting] = []
    selected_locations = locations or ["Canada"]
    format_clause = _work_format_query(work_formats)
    requests_made = 0
    seen_keys: set[str] = set()

    for location in selected_locations:
        for q in queries:
            if stop_check and stop_check():
                return jobs
            next_page_token = None
            for page_idx in range(max(1, int(pages_per_query or 1))):
                if stop_check and stop_check():
                    return jobs
                if max_requests is not None and requests_made >= max_requests:
                    return jobs
                if next_page_token:
                    params = {
                        "engine": "google_jobs",
                        "next_page_token": next_page_token,
                        "api_key": key,
                        "hl": "en",
                    }
                else:
                    smart_query = _smart_jobs_query(q, location, work_formats)
                    params = {
                        "engine": "google_jobs",
                        "q": smart_query,
                        "location": location,
                        "api_key": key,
                        "hl": "en",
                    }
                requests_made += 1
                try:
                    r = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    jobs.append(JobPosting(
                        title="__SERPAPI_ERROR__",
                        company="SerpAPI",
                        location=location,
                        description=str(e),
                        source="serpapi_error",
                        raw={"query": q, "location": location, "page": page_idx + 1, "error": str(e)},
                    ))
                    break

                for item in data.get("jobs_results", []) or []:
                    posted = (item.get("detected_extensions") or {}).get("posted_at") or item.get("via", "")
                    if not _posted_within_lookback(str(posted) if posted else None, lookback_hours):
                        continue
                    job = _item_to_job(item)
                    key2 = job.stable_key
                    if key2 not in seen_keys:
                        jobs.append(job)
                        seen_keys.add(key2)

                pag = data.get("serpapi_pagination") or {}
                next_page_token = pag.get("next_page_token")
                if not next_page_token:
                    break
    return jobs
