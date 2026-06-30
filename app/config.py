from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List


JOB_CATEGORIES: Dict[str, List[str]] = {
    "Software Development": [
        '"software developer"', '"software engineer"', '"junior developer"',
        '"backend developer"', '"full stack developer"', '"python developer"',
        '"java developer"', '"api developer"'
    ],
    "Machine Learning / AI": [
        '"machine learning engineer"', '"machine learning developer"', '"AI developer"',
        '"applied AI"', '"data scientist"', '"computer vision"', '"NLP engineer"'
    ],
    "Agentic AI / LLM": [
        '"agentic AI"', '"LLM engineer"', '"generative AI developer"',
        '"RAG" "developer"', '"AI agent" "developer"', '"OpenAI API" "developer"'
    ],
    "Cybersecurity / AI Security": [
        '"cybersecurity analyst"', '"security analyst"', '"AI security"',
        '"application security"', '"security engineer"', '"phishing" "analyst"'
    ],
    "Cloud / DevOps": [
        '"cloud developer"', '"cloud engineer"', '"AWS developer"',
        '"Azure developer"', '"DevOps junior"', '"site reliability" "junior"'
    ],
    "Data / Analytics": [
        '"data analyst"', '"data engineer"', '"business intelligence"',
        '"analytics developer"', '"SQL developer"'
    ],
    "Help Desk / IT Support": [
        '"help desk"', '"IT support"', '"technical support"',
        '"desktop support"', '"support specialist"'
    ],
    "Teaching / Tutoring": [
        '"computer science tutor"', '"programming tutor"', '"Python instructor"',
        '"coding instructor"', '"AI instructor"', '"teaching assistant"'
    ],
}

# Public ATS boards used for discovery even without a paid search API.
# Add/remove companies here. Slugs differ by ATS provider.
DEFAULT_ATS_BOARDS = {
    "greenhouse": [
        "cohere", "wealthsimple", "shopify", "databricks", "stripe", "figma", "notion", "ramp",
        "scaleai", "huggingface", "openai", "anthropic", "wandb", "recursion", "benchci"
    ],
    "lever": [
        "instacart", "roblox", "spotify", "netflix", "affirm", "brex", "figma", "chainalysis"
    ],
    "ashby": [
        "cohere", "cursor", "perplexity", "writer", "modal", "poolside", "replicate"
    ],
}

WORK_FORMATS = ["Remote", "Hybrid", "On-site"]

DEFAULT_LOCATIONS = [
    "Canada",
    "Remote - Canada",
    "Toronto, ON",
    "Vancouver, BC",
    "Montreal, QC",
    "Ottawa, ON",
    "Kitchener-Waterloo, ON",
    "Fredericton, NB",
    "Halifax, NS",
    "Calgary, AB",
    "Edmonton, AB",
    "United States",
    "Worldwide remote",
]

@dataclass(frozen=True)
class ScanSettings:
    categories: List[str]
    lookback_hours: int = 24
    min_score: int = 75
    max_years_excluded_at: int = 5
    locations: List[str] | None = None
    work_formats: List[str] | None = None
    include_teaching: bool = True
    exclude_volunteer: bool = True
    min_hourly_rate: float = 0.0
    min_annual_salary: int = 0
    serpapi_api_key: str = ""
    tavily_api_key: str = ""
    openai_api_key: str = ""
    api_provider: str = "SerpAPI"
    serpapi_max_requests: int = 60
    serpapi_pages_per_query: int = 2
    ats_boards_per_provider: int = 8
    search_depth: str = "Fast"
    max_new_jobs_per_scan: int = 30
    enrich_during_scan: bool = False
    enable_remotive: bool = True
