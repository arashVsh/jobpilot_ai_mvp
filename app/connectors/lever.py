from __future__ import annotations
import requests
from app.models import JobPosting
from app.utils.text_cleaning import html_to_plain_text


def fetch_lever_board(company_slug: str, timeout: int = 20) -> list[JobPosting]:
    """Fetch public jobs from Lever postings API, e.g., 'netflix' if available."""
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data:
        description = " ".join([
            item.get("descriptionPlain", ""),
            html_to_plain_text(item.get("description", "")),
            html_to_plain_text(item.get("lists", "")) if isinstance(item.get("lists"), str) else "",
        ])
        categories = item.get("categories") or {}
        jobs.append(JobPosting(
            title=item.get("text", ""),
            company=company_slug,
            location=categories.get("location", ""),
            description=description,
            apply_url=item.get("hostedUrl", ""),
            source="lever",
            department=categories.get("team", ""),
            employment_type=categories.get("commitment", ""),
            raw=item,
        ))
    return jobs
