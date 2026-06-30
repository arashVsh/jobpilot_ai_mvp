from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


def fetch_remoteok_jobs(
    queries: Iterable[str],
    timeout: int = 6,
    stop_check=None,
    max_jobs: int = 80,
) -> list[JobPosting]:
    """Fetch a bounded set of public RemoteOK jobs.

    RemoteOK is used as an optional no-key remote source. The app still applies
    strict category/location/work-format/fit filters before saving anything.
    """
    query_terms: list[str] = []
    for q in queries:
        for token in str(q).replace('"', '').lower().replace('/', ' ').split():
            token = token.strip()
            if len(token) >= 3 and token not in query_terms:
                query_terms.append(token)
    jobs: list[JobPosting] = []
    try:
        r = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobPilotAI/0.22 local portfolio app"},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [JobPosting(
            title="__REMOTEOK_ERROR__",
            company="RemoteOK",
            location="Remote",
            description=str(e),
            source="remotive_error",  # reuse existing API-error handling path
            raw={"error": str(e)},
        )]

    for item in data:
        if stop_check and stop_check():
            break
        if not isinstance(item, dict) or not item.get("position"):
            continue
        title = str(item.get("position") or "")
        company = str(item.get("company") or "")
        description = html_to_plain_text(str(item.get("description") or ""))
        tags = " ".join(str(x) for x in (item.get("tags") or []))
        blob = f"{title} {company} {description} {tags}".lower()
        if query_terms and not any(term in blob for term in query_terms[:40]):
            continue
        url = str(item.get("url") or item.get("apply_url") or "")
        location = str(item.get("location") or "Remote / Worldwide")
        jobs.append(JobPosting(
            title=title,
            company=company,
            location=location,
            description=description,
            apply_url=url,
            source="remoteok",
            employment_type="Remote",
            posted_date=str(item.get("date") or ""),
            discovered_at=datetime.now(timezone.utc).isoformat(),
            raw=item,
        ))
        if len(jobs) >= max_jobs:
            break
    return jobs
