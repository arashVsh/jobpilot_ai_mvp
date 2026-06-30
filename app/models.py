from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


@dataclass
class CandidateProfile:
    name: str = "Candidate"
    email: str = ""
    portfolio: str = ""
    scholar: str = ""
    current_title: str = ""
    affiliation: str = ""
    focus: str = ""
    resume_text: str = ""
    open_to_relocation: bool = True
    max_years_required: int = 4
    include_teaching: bool = True
    exclude_volunteer: bool = True
    core_skills: List[str] = field(default_factory=list)
    projects: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JobPosting:
    title: str
    company: str
    location: str = ""
    description: str = ""
    apply_url: str = ""
    source: str = "manual"
    department: str = ""
    employment_type: str = ""
    posted_date: Optional[str] = None
    discovered_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def stable_key(self) -> str:
        key = self.apply_url or f"{self.source}|{self.company}|{self.title}|{self.location}"
        return key.strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FitResult:
    score: int
    decision: str
    matched_skills: List[str]
    missing_skills: List[str]
    concerns: List[str]
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CompanyResearch:
    company: str
    unique_detail: str
    source_url: Optional[str] = None
    confidence: str = "low"


@dataclass
class ContactCandidate:
    name: str
    title: str
    contact_url: Optional[str] = None
    email: Optional[str] = None
    reason: str = ""
    confidence: str = "low"
