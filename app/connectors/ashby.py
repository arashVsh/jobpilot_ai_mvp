from __future__ import annotations
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


def fetch_ashby_board(board_name: str, include_compensation: bool = True, timeout: int = 20) -> list[JobPosting]:
    """Fetch public jobs from Ashby public posting API board name."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    params = {"includeCompensation": str(include_compensation).lower()}
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        location = item.get("locationName") or item.get("location", "")
        jobs.append(JobPosting(
            title=item.get("title", ""),
            company=board_name,
            location=location,
            description=html_to_plain_text(item.get("descriptionHtml", "")) or item.get("descriptionPlain", ""),
            apply_url=item.get("jobUrl", "") or item.get("applyUrl", ""),
            source="ashby",
            department=item.get("department", ""),
            employment_type=item.get("employmentType", ""),
            raw=item,
        ))
    return jobs
