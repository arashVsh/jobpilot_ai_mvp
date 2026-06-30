from __future__ import annotations
import re
import subprocess
from pathlib import Path
from app.models import JobPosting

ROLE_SUMMARIES = {
    "software": "Software Developer with experience building deployed applications, backend APIs, and AI-enabled tools using Python, Java, Kotlin, JavaScript, FastAPI, Streamlit, React, and cloud platforms. MSc Computer Science candidate with strong foundations in machine learning, cybersecurity, and practical product development. Available for full-time roles and open to relocation across Canada.",
    "ml": "Machine Learning Developer with research publications and industry certifications in AI, cybersecurity, and cloud. Experienced in adversarially resilient models and end-to-end deployed AI applications using PyTorch, Streamlit, FastAPI, and AWS/Azure. Strong at translating research into practical products for users. Available for full-time roles and open to relocation across Canada.",
    "agentic": "AI Developer focused on agentic AI, RAG/LLMOps, AI security, and deployed AI applications. Experienced with Python, Streamlit, OpenAI API, document question answering, retrieval pipelines, and robust ML research. Available for full-time roles and open to relocation across Canada.",
    "cyber": "AI Security and Cybersecurity candidate with MSc research in adversarial machine learning, robustness evaluation, and trustworthy AI. Experienced with Python, PyTorch, phishing/security applications, cloud fundamentals, and security certifications. Available for full-time roles and open to relocation across Canada.",
    "teaching": "MSc Computer Science candidate with graduate teaching assistant experience in Java, Python, data science, network modeling, and software engineering. Experienced in explaining technical concepts clearly through tutorials, course materials, and public AI security education. Available for tutoring, teaching, and full-time technical roles.",
}


def infer_role_family(job: JobPosting) -> str:
    t = f"{job.title} {job.description}".lower()
    if any(x in t for x in ["agentic", "llm", "rag", "genai", "generative ai", "openai"]):
        return "agentic"
    if any(x in t for x in ["cyber", "security", "phishing", "threat", "soc"]):
        return "cyber"
    if any(x in t for x in ["software", "backend", "frontend", "full stack", "java", "react", "api"]):
        return "software"
    if any(x in t for x in ["tutor", "teaching", "instructor", "trainer"]):
        return "teaching"
    return "ml"


def replace_summary(tex: str, summary: str) -> str:
    pattern = r"\\section\{Summary\}.*?\n\s*%--------%\n\s*% Skills %"
    repl = f"\\section{{Summary}}\n{summary}\n\n%--------%\n% Skills %"
    return re.sub(pattern, repl, tex, flags=re.S)


def maybe_uncomment_project(tex: str, project_name: str) -> str:
    # Simple MVP: remove leading % from contiguous commented project block containing project_name.
    lines = tex.splitlines()
    out = []
    in_block = False
    buffer = []
    for line in lines:
        if project_name in line and line.lstrip().startswith("%"):
            in_block = True
        if in_block:
            buffer.append(line)
            if "\\end{resume_list}" in line:
                for b in buffer:
                    out.append(re.sub(r"^\s*%\s?", "", b))
                buffer = []
                in_block = False
            continue
        out.append(line)
    out.extend(buffer)
    return "\n".join(out) + "\n"


def tailor_latex(base_tex_path: Path, job: JobPosting, out_path: Path) -> dict:
    tex = base_tex_path.read_text(encoding="utf-8")
    family = infer_role_family(job)
    tex = replace_summary(tex, ROLE_SUMMARIES[family])
    changes = [f"Role family inferred: {family}", "Summary replaced."]
    if family == "software":
        tex = maybe_uncomment_project(tex, "AttackBench")
        changes.append("Uncommented AttackBench if present.")
    if family == "cyber":
        tex = maybe_uncomment_project(tex, "PhishGuard")
        changes.append("Uncommented PhishGuard if present.")
    out_path.write_text(tex, encoding="utf-8")
    return {"role_family": family, "changes": changes, "tex_path": str(out_path)}


def compile_pdf(tex_path: Path, max_pages: float = 1.5) -> dict:
    """Compile using latexmk or pdflatex if available. Returns status; page counting can be added with pdfinfo."""
    workdir = tex_path.parent
    try:
        cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", tex_path.name]
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            cmd = ["pdflatex", "-interaction=nonstopmode", tex_path.name]
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=60)
        pdf_path = tex_path.with_suffix(".pdf")
        return {"ok": proc.returncode == 0 and pdf_path.exists(), "pdf_path": str(pdf_path), "log": proc.stdout[-3000:]}
    except FileNotFoundError:
        return {"ok": False, "pdf_path": None, "log": "No LaTeX compiler found. Install TeX Live / latexmk or use a Docker image."}
    except Exception as e:
        return {"ok": False, "pdf_path": None, "log": str(e)}
