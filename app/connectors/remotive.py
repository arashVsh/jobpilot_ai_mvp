from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


def _item_to_job(item: dict) -> JobPosting:
    title = item.get("title", "")
    company = item.get("company_name", "")
    url = item.get("url", "")
    location = item.get("candidate_required_location") or "Remote / Worldwide"
    description = html_to_plain_text(item.get("description", ""))
    return JobPosting(
        title=title,
        company=company,
        location=location,
        description=description,
        apply_url=url,
        source="remotive",
        posted_date=item.get("publication_date"),
        discovered_at=datetime.now(timezone.utc).isoformat(),
        raw=item,
    )


def fetch_remotive_jobs(
    queries: Iterable[str],
    timeout: int = 8,
    stop_check=None,
    max_jobs_per_query: int = 20,
) -> list[JobPosting]:
    """Fetch public remote jobs from Remotive. No API key is required.

    This is a lightweight supplemental source. It is intentionally bounded so it
    cannot dominate a scan. Location/salary/fit filters are still applied by the
    main scanner.
    """
    jobs: list[JobPosting] = []
    seen: set[str] = set()
    for q in queries:
        if stop_check and stop_check():
            return jobs
        try:
            r = requests.get(REMOTIVE_URL, params={"search": q.replace('"', '')}, timeout=timeout, headers={"User-Agent": "JobPilotAI/0.6"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            jobs.append(JobPosting(
                title="__REMOTIVE_ERROR__",
                company="Remotive",
                location="Remote",
                description=str(e),
                source="remotive_error",
                raw={"query": q, "error": str(e)},
            ))
            continue
        count = 0
        for item in data.get("jobs", []) or []:
            if stop_check and stop_check():
                return jobs
            job = _item_to_job(item)
            if job.stable_key in seen:
                continue
            jobs.append(job)
            seen.add(job.stable_key)
            count += 1
            if count >= max_jobs_per_query:
                break
    return jobs
