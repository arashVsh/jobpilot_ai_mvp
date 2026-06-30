from __future__ import annotations
import re
from pathlib import Path
from typing import Iterable

SKILL_CANDIDATES = [
    "python", "java", "kotlin", "javascript", "typescript", "sql", "c++", "react", "html", "css",
    "fastapi", "flask", "streamlit", "rest api", "docker", "aws", "azure", "google cloud", "gcp",
    "pytorch", "tensorflow", "scikit-learn", "numpy", "pandas", "matplotlib", "machine learning",
    "deep learning", "reinforcement learning", "stable-baselines3", "gymnasium", "nlp", "rag", "llmops",
    "agentic design", "openai api", "cybersecurity", "ai security", "adversarial machine learning",
    "phishing", "wireshark", "git", "github", "mlflow", "jupyter", "teaching", "tutoring"
]

PROJECT_KEYWORDS = {
    "PaperWise AI": ["paperwise", "rag", "llmops", "openai", "document question answering"],
    "EvasionRL": ["evasionrl", "reinforcement learning", "adversarial attacks", "mountaincar"],
    "PromptShield": ["promptshield", "vision-language", "adversarial detection"],
    "PhishGuard": ["phishguard", "phishing", "android", "kotlin"],
    "AttackBench": ["attackbench", "javafx", "fastapi", "adversarial attacks"],
}


def extract_text_from_upload(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    name = uploaded_file.name.lower()
    data = uploaded_file.read()
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            return f"[Could not parse PDF: {e}]"
    return data.decode("utf-8", errors="ignore")


def clean_latex(text: str) -> str:
    text = re.sub(r"%.*", " ", text)
    text = re.sub(r"\\href\{([^}]*)\}\{([^}]*)\}", r"\2 \1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?(?:\{([^{}]*)\})?", r" \1 ", text)
    text = re.sub(r"[{}\\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def infer_name_email(text: str) -> tuple[str, str]:
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    email = email_match.group(0) if email_match else ""
    name = "Candidate"
    for line in text.splitlines()[:25]:
        plain = clean_latex(line).strip()
        if len(plain.split()) in (2, 3) and not any(ch.isdigit() for ch in plain) and "@" not in plain:
            if plain.upper() == plain or plain.istitle():
                name = plain.title()
                break
    return name, email


def extract_skills(text: str, candidates: Iterable[str] = SKILL_CANDIDATES) -> list[str]:
    t = clean_latex(text).lower()
    found = []
    for skill in candidates:
        if skill.lower() in t:
            found.append(skill.lower())
    return sorted(set(found))


def extract_projects(text: str) -> dict[str, list[str]]:
    t = clean_latex(text).lower()
    projects = {}
    for name, kws in PROJECT_KEYWORDS.items():
        hits = [kw for kw in kws if kw.lower() in t]
        if hits:
            projects[name] = hits
    return projects
