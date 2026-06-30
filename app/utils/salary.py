from __future__ import annotations
import re
from dataclasses import dataclass
from app.models import JobPosting


@dataclass
class SalaryParseResult:
    hourly_values: list[float]
    annual_values: list[float]
    evidence: list[str]


def _money_to_float(raw: str) -> float | None:
    if not raw:
        return None
    text = raw.lower().replace(",", "").strip()
    mult = 1000 if text.endswith("k") else 1
    text = text.rstrip("k")
    try:
        return float(text) * mult
    except ValueError:
        return None


def parse_salary_text(text: str) -> SalaryParseResult:
    """Best-effort salary parser. Conservative by design: if uncertain, keep the job.

    It extracts explicit hourly and annual values/ranges only. The scanner excludes a job
    only when the maximum explicitly stated salary is below the user's threshold.
    """
    normalized = re.sub(r"\s+", " ", text or "")
    hourly: list[float] = []
    annual: list[float] = []
    evidence: list[str] = []

    money = r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?k)"

    # Hourly examples: $22/hr, $20 - $28 per hour, CAD 30 hourly.
    hourly_patterns = [
        rf"{money}\s*(?:-|–|to)\s*{money}\s*(?:/\s*hr|/\s*hour|per\s+hour|hourly|an\s+hour)",
        rf"{money}\s*(?:/\s*hr|/\s*hour|per\s+hour|hourly|an\s+hour)",
    ]
    for pattern in hourly_patterns:
        for m in re.finditer(pattern, normalized, flags=re.I):
            vals = [_money_to_float(g) for g in m.groups() if g]
            vals = [v for v in vals if v is not None and 5 <= v <= 500]
            if vals:
                hourly.extend(vals)
                evidence.append(m.group(0).strip())

    # Annual examples: $65,000 - $90,000 per year, $75k annual, CAD 80,000 salary.
    annual_patterns = [
        rf"{money}\s*(?:-|–|to)\s*{money}\s*(?:per\s+year|a\s+year|annually|annual|salary|/\s*year|/\s*yr)",
        rf"{money}\s*(?:per\s+year|a\s+year|annually|annual|salary|/\s*year|/\s*yr)",
        rf"(?:salary|annual|annually)\s*(?:range)?\s*:?\s*{money}\s*(?:-|–|to)\s*{money}",
    ]
    for pattern in annual_patterns:
        for m in re.finditer(pattern, normalized, flags=re.I):
            vals = [_money_to_float(g) for g in m.groups() if g]
            vals = [v for v in vals if v is not None]
            # Treat 40-200 without k/comma as likely hourly, not annual.
            vals = [v for v in vals if v >= 1000]
            if vals:
                annual.extend(vals)
                evidence.append(m.group(0).strip())

    return SalaryParseResult(hourly_values=hourly, annual_values=annual, evidence=list(dict.fromkeys(evidence))[:6])


def job_meets_salary_threshold(job: JobPosting, min_hourly: float = 0.0, min_annual: int = 0) -> tuple[bool, str]:
    text = f"{job.title} {job.company} {job.location} {job.description} {job.employment_type} {job.raw}" 
    parsed = parse_salary_text(text)

    # If no salary was disclosed, keep the role. The user explicitly asked for this behavior.
    if not parsed.hourly_values and not parsed.annual_values:
        return True, "No explicit salary found; kept by default."

    if min_hourly and parsed.hourly_values:
        if max(parsed.hourly_values) < float(min_hourly):
            return False, f"Excluded because explicit hourly salary appears below ${min_hourly:g}/hr. Evidence: {', '.join(parsed.evidence)}"

    if min_annual and parsed.annual_values:
        if max(parsed.annual_values) < float(min_annual):
            return False, f"Excluded because explicit annual salary appears below ${min_annual:,}/year. Evidence: {', '.join(parsed.evidence)}"

    return True, f"Salary passed or was not comparable. Evidence: {', '.join(parsed.evidence) if parsed.evidence else 'none'}"
