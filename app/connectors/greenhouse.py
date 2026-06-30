from __future__ import annotations
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


def fetch_greenhouse_board(board_token: str, timeout: int = 20) -> list[JobPosting]:
    """Fetch public jobs from a Greenhouse board token, e.g., 'openai'."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        offices = item.get("offices") or []
        location = ", ".join(o.get("name", "") for o in offices if o.get("name")) or item.get("location", {}).get("name", "")
        jobs.append(JobPosting(
            title=item.get("title", ""),
            company=board_token,
            location=location,
            description=html_to_plain_text(item.get("content", "")),
            apply_url=item.get("absolute_url", ""),
            source="greenhouse",
            department=(item.get("departments") or [{}])[0].get("name", "") if item.get("departments") else "",
            raw=item,
        ))
    return jobs
