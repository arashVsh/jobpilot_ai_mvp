from __future__ import annotations
from datetime import datetime, timezone
from app.models import JobPosting
from app.config import DEFAULT_ATS_BOARDS
from app.connectors.greenhouse import fetch_greenhouse_board
from app.connectors.lever import fetch_lever_board
from app.connectors.ashby import fetch_ashby_board


def fetch_default_ats_jobs(max_boards_per_provider: int | None = None) -> list[JobPosting]:
    """Fetch public jobs from known ATS boards. This is a discovery baseline without paid APIs."""
    jobs: list[JobPosting] = []
    providers = {
        "greenhouse": fetch_greenhouse_board,
        "lever": fetch_lever_board,
        "ashby": fetch_ashby_board,
    }
    for provider, slugs in DEFAULT_ATS_BOARDS.items():
        fetcher = providers[provider]
        selected = slugs[:max_boards_per_provider] if max_boards_per_provider else slugs
        for slug in selected:
            try:
                batch = fetcher(slug)
                now = datetime.now(timezone.utc).isoformat()
                for job in batch:
                    job.discovered_at = job.discovered_at or now
                    jobs.append(job)
            except Exception:
                # Some public boards may not exist for a slug or may block. Continue scanning.
                continue
    return jobs
