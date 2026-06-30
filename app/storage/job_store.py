from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from app.models import JobPosting, FitResult, CompanyResearch, ContactCandidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT UNIQUE,
    title TEXT,
    company TEXT,
    location TEXT,
    source TEXT,
    apply_url TEXT,
    posted_date TEXT,
    discovered_at TEXT,
    score INTEGER,
    decision TEXT,
    matched_skills TEXT,
    missing_skills TEXT,
    concerns TEXT,
    rationale TEXT,
    salary_evidence TEXT,
    unique_company_detail TEXT,
    company_detail_url TEXT,
    contact_name TEXT,
    contact_title TEXT,
    contact_email TEXT,
    contact_url TEXT,
    contact_confidence TEXT,
    ceo_name TEXT,
    ceo_title TEXT,
    ceo_email TEXT,
    ceo_url TEXT,
    ceo_confidence TEXT,
    ceo_json TEXT,
    job_json TEXT,
    fit_json TEXT,
    research_json TEXT,
    contact_json TEXT,
    status TEXT DEFAULT 'New',
    applied INTEGER DEFAULT 0,
    favorite INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0,
    applied_at TEXT,
    follow_up_choice TEXT DEFAULT 'No follow-up',
    follow_up_due_at TEXT,
    follow_up_completed INTEGER DEFAULT 0,
    follow_up_completed_at TEXT,
    deleted_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    message TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    job_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    source TEXT,
    apply_url TEXT,
    reason_category TEXT,
    reason_detail TEXT,
    score INTEGER,
    job_json TEXT,
    created_at TEXT NOT NULL
);
"""

NEW_COLUMNS: dict[str, str] = {
    "salary_evidence": "TEXT",
    "ceo_name": "TEXT",
    "ceo_title": "TEXT",
    "ceo_email": "TEXT",
    "ceo_url": "TEXT",
    "ceo_confidence": "TEXT",
    "ceo_json": "TEXT",
    "applied": "INTEGER DEFAULT 0",
    "favorite": "INTEGER DEFAULT 0",
    "is_deleted": "INTEGER DEFAULT 0",
    "applied_at": "TEXT",
    "follow_up_choice": "TEXT DEFAULT 'No follow-up'",
    "follow_up_due_at": "TEXT",
    "follow_up_completed": "INTEGER DEFAULT 0",
    "follow_up_completed_at": "TEXT",
    "deleted_at": "TEXT",
    "last_seen_at": "TEXT",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, decl in NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


def job_was_seen(conn: sqlite3.Connection, stable_key: str) -> bool:
    row = conn.execute("SELECT id FROM jobs WHERE stable_key = ?", (stable_key,)).fetchone()
    return row is not None


def mark_seen(conn: sqlite3.Connection, stable_key: str) -> None:
    conn.execute("UPDATE jobs SET last_seen_at=?, updated_at=? WHERE stable_key=?", (_utc_now(), _utc_now(), stable_key))
    conn.commit()


def record_rejected_job(
    conn: sqlite3.Connection,
    job: JobPosting,
    reason_category: str,
    reason_detail: str,
    score: int | None = None,
) -> None:
    """Persist a compact rejection/debug record for scanner transparency.

    Uses INSERT rather than UNIQUE upsert so repeated scans can show when/why a
    role was rejected. The debug tab limits display volume.
    """
    try:
        conn.execute(
            """
            INSERT INTO rejected_jobs(
                stable_key,title,company,location,source,apply_url,reason_category,reason_detail,score,job_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.stable_key,
                job.title,
                job.company,
                job.location,
                job.source,
                job.apply_url,
                reason_category,
                reason_detail,
                score,
                json.dumps(job.to_dict(), ensure_ascii=False),
                _utc_now(),
            ),
        )
        conn.commit()
    except Exception:
        pass


def list_rejected_jobs(conn: sqlite3.Connection, limit: int = 500, reason_category: str | None = None):
    if reason_category and reason_category != "All":
        return conn.execute(
            """
            SELECT * FROM rejected_jobs
            WHERE reason_category = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (reason_category, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM rejected_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def rejected_jobs_summary(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT reason_category, COUNT(*) AS rejected
        FROM rejected_jobs
        GROUP BY reason_category
        ORDER BY rejected DESC
        """
    ).fetchall()


def clear_rejected_jobs(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) FROM rejected_jobs").fetchone()[0]
    conn.execute("DELETE FROM rejected_jobs")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='rejected_jobs'")
    conn.commit()
    return int(count)


def upsert_job(
    conn: sqlite3.Connection,
    job: JobPosting,
    fit: FitResult,
    research: CompanyResearch,
    contact: ContactCandidate | None,
    ceo: ContactCandidate | None = None,
    salary_evidence: str = "",
) -> bool:
    """Insert a new suggested job or refresh an existing job while preserving user labels.

    Existing jobs are not duplicated. User-controlled fields such as applied, favorite,
    is_deleted, and status are preserved on conflict.
    """
    now = _utc_now()
    contact = contact or ContactCandidate(name="", title="", confidence="low")
    ceo = ceo or ContactCandidate(name="", title="", confidence="low")
    data = {
        "stable_key": job.stable_key,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "source": job.source,
        "apply_url": job.apply_url,
        "posted_date": job.posted_date,
        "discovered_at": job.discovered_at or now,
        "score": fit.score,
        "decision": fit.decision,
        "matched_skills": ", ".join(fit.matched_skills),
        "missing_skills": ", ".join(fit.missing_skills),
        "concerns": "; ".join(fit.concerns),
        "rationale": fit.rationale,
        "salary_evidence": salary_evidence,
        "unique_company_detail": research.unique_detail,
        "company_detail_url": research.source_url,
        "contact_name": contact.name,
        "contact_title": contact.title,
        "contact_email": contact.email,
        "contact_url": contact.contact_url,
        "contact_confidence": contact.confidence,
        "ceo_name": ceo.name,
        "ceo_title": ceo.title,
        "ceo_email": ceo.email,
        "ceo_url": ceo.contact_url,
        "ceo_confidence": ceo.confidence,
        "ceo_json": json.dumps(ceo.__dict__, ensure_ascii=False),
        "job_json": json.dumps(job.to_dict(), ensure_ascii=False),
        "fit_json": json.dumps(fit.to_dict(), ensure_ascii=False),
        "research_json": json.dumps(research.__dict__, ensure_ascii=False),
        "contact_json": json.dumps(contact.__dict__, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
    }
    cur = conn.execute(
        """
        INSERT INTO jobs (
            stable_key,title,company,location,source,apply_url,posted_date,discovered_at,score,decision,
            matched_skills,missing_skills,concerns,rationale,salary_evidence,unique_company_detail,company_detail_url,
            contact_name,contact_title,contact_email,contact_url,contact_confidence,
            ceo_name,ceo_title,ceo_email,ceo_url,ceo_confidence,ceo_json,
            job_json,fit_json,research_json,contact_json,created_at,updated_at,last_seen_at
        ) VALUES (
            :stable_key,:title,:company,:location,:source,:apply_url,:posted_date,:discovered_at,:score,:decision,
            :matched_skills,:missing_skills,:concerns,:rationale,:salary_evidence,:unique_company_detail,:company_detail_url,
            :contact_name,:contact_title,:contact_email,:contact_url,:contact_confidence,
            :ceo_name,:ceo_title,:ceo_email,:ceo_url,:ceo_confidence,:ceo_json,
            :job_json,:fit_json,:research_json,:contact_json,:created_at,:updated_at,:last_seen_at
        )
        ON CONFLICT(stable_key) DO UPDATE SET
            score=excluded.score,
            decision=excluded.decision,
            matched_skills=excluded.matched_skills,
            missing_skills=excluded.missing_skills,
            concerns=excluded.concerns,
            rationale=excluded.rationale,
            salary_evidence=excluded.salary_evidence,
            unique_company_detail=excluded.unique_company_detail,
            company_detail_url=excluded.company_detail_url,
            contact_name=excluded.contact_name,
            contact_title=excluded.contact_title,
            contact_email=excluded.contact_email,
            contact_url=excluded.contact_url,
            contact_confidence=excluded.contact_confidence,
            ceo_name=excluded.ceo_name,
            ceo_title=excluded.ceo_title,
            ceo_email=excluded.ceo_email,
            ceo_url=excluded.ceo_url,
            ceo_confidence=excluded.ceo_confidence,
            ceo_json=excluded.ceo_json,
            job_json=excluded.job_json,
            fit_json=excluded.fit_json,
            research_json=excluded.research_json,
            contact_json=excluded.contact_json,
            updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at
        """,
        data,
    )
    conn.commit()
    return cur.rowcount > 0


def list_jobs(conn: sqlite3.Connection, limit: int = 500, include_deleted: bool = False):
    where = "" if include_deleted else "WHERE COALESCE(is_deleted, 0) = 0"
    return conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY favorite DESC, applied ASC, score DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def list_favorites(conn: sqlite3.Connection, limit: int = 500):
    return conn.execute(
        "SELECT * FROM jobs WHERE COALESCE(is_deleted, 0)=0 AND COALESCE(favorite, 0)=1 ORDER BY score DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def list_applied(conn: sqlite3.Connection, limit: int = 500):
    return conn.execute(
        "SELECT * FROM jobs WHERE COALESCE(is_deleted, 0)=0 AND COALESCE(applied, 0)=1 ORDER BY applied_at DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def list_unapplied(conn: sqlite3.Connection, limit: int = 500):
    return conn.execute(
        "SELECT * FROM jobs WHERE COALESCE(is_deleted, 0)=0 AND COALESCE(applied, 0)=0 ORDER BY favorite DESC, score DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def update_status(conn: sqlite3.Connection, job_id: int, status: str):
    conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, _utc_now(), job_id))
    conn.commit()


def update_applied_favorite(conn: sqlite3.Connection, job_id: int, applied: bool | None = None, favorite: bool | None = None):
    row = get_job(conn, job_id)
    if not row:
        return
    now = _utc_now()
    fields: list[str] = []
    values: list[object] = []
    if applied is not None:
        fields.append("applied=?")
        values.append(1 if applied else 0)
        fields.append("applied_at=?")
        values.append(now if applied else None)
        # Keep status readable but do not overwrite later pipeline states like Interview/Rejected when unchecked.
        fields.append("status=?")
        values.append("Applied" if applied else ("New" if row["status"] == "Applied" else row["status"]))
    if favorite is not None:
        fields.append("favorite=?")
        values.append(1 if favorite else 0)
    if not fields:
        return
    fields.append("updated_at=?")
    values.append(now)
    values.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()


def soft_delete_job(conn: sqlite3.Connection, job_id: int):
    now = _utc_now()
    conn.execute("UPDATE jobs SET is_deleted=1, deleted_at=?, updated_at=?, status='Archived' WHERE id=?", (now, now, job_id))
    conn.commit()


def restore_job(conn: sqlite3.Connection, job_id: int):
    now = _utc_now()
    conn.execute("UPDATE jobs SET is_deleted=0, deleted_at=NULL, updated_at=?, status='New' WHERE id=?", (now, job_id))
    conn.commit()


def archive_visible_jobs(conn: sqlite3.Connection) -> int:
    """Clear the visible list while preserving history so old jobs are not re-suggested."""
    count = conn.execute("SELECT COUNT(*) FROM jobs WHERE COALESCE(is_deleted,0)=0").fetchone()[0]
    now = _utc_now()
    conn.execute("UPDATE jobs SET is_deleted=1, deleted_at=?, updated_at=?, status='Archived' WHERE COALESCE(is_deleted,0)=0", (now, now))
    conn.commit()
    return int(count)


def archive_job_ids(conn: sqlite3.Connection, job_ids: list[int]) -> int:
    """Archive only the supplied job IDs while preserving history."""
    ids = [int(x) for x in job_ids if x is not None]
    if not ids:
        return 0
    now = _utc_now()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE jobs SET is_deleted=1, deleted_at=?, updated_at=?, status='Archived' WHERE id IN ({placeholders})",
        [now, now, *ids],
    )
    conn.commit()
    return len(ids)


def clear_jobs(conn: sqlite3.Connection) -> int:
    """Remove all stored job records and reset the table counter. This also clears history."""
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM rejected_jobs")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='rejected_jobs'")
    conn.commit()
    return int(count)


FOLLOW_UP_OPTIONS = {
    "No follow-up": None,
    "Follow up in 7 days": 7,
    "Follow up in 10 days": 10,
}


def update_follow_up(conn: sqlite3.Connection, job_id: int, choice: str):
    """Set follow-up reminder for an applied job."""
    now = datetime.now(timezone.utc)
    days = FOLLOW_UP_OPTIONS.get(choice)
    due_at = None
    if days:
        due_at = (now + timedelta(days=days)).isoformat()
    conn.execute(
        """
        UPDATE jobs
        SET follow_up_choice=?, follow_up_due_at=?, follow_up_completed=0,
            follow_up_completed_at=NULL, updated_at=?
        WHERE id=?
        """,
        (choice, due_at, now.isoformat(), job_id),
    )
    conn.commit()


def mark_follow_up_completed(conn: sqlite3.Connection, job_id: int, completed: bool = True):
    now = _utc_now()
    conn.execute(
        "UPDATE jobs SET follow_up_completed=?, follow_up_completed_at=?, updated_at=? WHERE id=?",
        (1 if completed else 0, now if completed else None, now, job_id),
    )
    conn.commit()


def list_followups(conn: sqlite3.Connection, limit: int = 200, due_only: bool = False):
    now = _utc_now()
    where = """
        WHERE COALESCE(is_deleted,0)=0
          AND COALESCE(applied,0)=1
          AND follow_up_due_at IS NOT NULL
          AND COALESCE(follow_up_completed,0)=0
    """
    params: list[object] = []
    if due_only:
        where += " AND follow_up_due_at <= ?"
        params.append(now)
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY follow_up_due_at ASC LIMIT ?",
        params,
    ).fetchall()


def add_activity_log(conn: sqlite3.Connection, agent: str, message: str, level: str = "info", job_id: int | None = None):
    conn.execute(
        "INSERT INTO activity_logs(agent,message,level,job_id,created_at) VALUES (?,?,?,?,?)",
        (agent, message, level, job_id, _utc_now()),
    )
    conn.commit()


def list_activity_logs(conn: sqlite3.Connection, limit: int = 300):
    return conn.execute(
        "SELECT * FROM activity_logs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def clear_activity_logs(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
    conn.execute("DELETE FROM activity_logs")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='activity_logs'")
    conn.commit()
    return int(count)


def monthly_application_counts(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT substr(applied_at, 1, 7) AS month, COUNT(*) AS applications
        FROM jobs
        WHERE COALESCE(applied,0)=1 AND applied_at IS NOT NULL
        GROUP BY substr(applied_at, 1, 7)
        ORDER BY month ASC
        """
    ).fetchall()


def applications_by_company(conn: sqlite3.Connection, limit: int = 100):
    """Return applied-job counts grouped by company."""
    return conn.execute(
        """
        SELECT
          COALESCE(NULLIF(TRIM(company), ''), 'Unknown company') AS company,
          COUNT(*) AS applications,
          MIN(applied_at) AS first_applied_at,
          MAX(applied_at) AS last_applied_at
        FROM jobs
        WHERE COALESCE(applied,0)=1 AND applied_at IS NOT NULL
        GROUP BY COALESCE(NULLIF(TRIM(company), ''), 'Unknown company')
        ORDER BY applications DESC, last_applied_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def applications_by_role(conn: sqlite3.Connection, limit: int = 100):
    """Return applied-job counts grouped by role title."""
    return conn.execute(
        """
        SELECT
          COALESCE(NULLIF(TRIM(title), ''), 'Unknown role') AS role_title,
          COUNT(*) AS applications,
          MIN(applied_at) AS first_applied_at,
          MAX(applied_at) AS last_applied_at
        FROM jobs
        WHERE COALESCE(applied,0)=1 AND applied_at IS NOT NULL
        GROUP BY COALESCE(NULLIF(TRIM(title), ''), 'Unknown role')
        ORDER BY applications DESC, last_applied_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def applied_jobs_detail(conn: sqlite3.Connection, limit: int = 500):
    """Return a detailed list of applied jobs for export/reporting."""
    return conn.execute(
        """
        SELECT
          applied_at, title, company, location, source, score, status,
          follow_up_choice, follow_up_due_at, apply_url, contact_name, contact_email, contact_url
        FROM jobs
        WHERE COALESCE(applied,0)=1 AND applied_at IS NOT NULL
        ORDER BY applied_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def latest_job_update_epoch(conn: sqlite3.Connection) -> float | None:
    """Best-effort helper for diagnosing possible orphan scanners."""
    row = conn.execute(
        "SELECT MAX(updated_at) AS updated_at FROM jobs"
    ).fetchone()
    if not row or not row["updated_at"]:
        return None
    try:
        return datetime.fromisoformat(row["updated_at"]).timestamp()
    except Exception:
        return None


def activity_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_saved,
          SUM(CASE WHEN COALESCE(applied,0)=1 THEN 1 ELSE 0 END) AS applied,
          SUM(CASE WHEN COALESCE(favorite,0)=1 THEN 1 ELSE 0 END) AS favorites,
          SUM(CASE WHEN COALESCE(is_deleted,0)=1 THEN 1 ELSE 0 END) AS archived,
          SUM(CASE WHEN COALESCE(applied,0)=0 AND COALESCE(is_deleted,0)=0 THEN 1 ELSE 0 END) AS active_unapplied
        FROM jobs
        """
    ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}
