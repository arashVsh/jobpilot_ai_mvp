from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

KEY_NAMES = {
    "serpapi": "SERPAPI_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _store_path(app_dir: Path) -> Path:
    output_dir = app_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / "saved_api_keys.json"


def load_saved_api_keys(app_dir: Path) -> Dict[str, str]:
    """Load locally remembered API keys.

    This is for a local portfolio/dev app. It is not encrypted secret storage.
    Prefer environment variables for production deployment.
    """
    path = _store_path(app_dir)
    if not path.exists():
        return {name: "" for name in KEY_NAMES}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {name: str(data.get(name, "") or "") for name in KEY_NAMES}
    except Exception:
        return {name: "" for name in KEY_NAMES}


def save_api_keys(app_dir: Path, *, serpapi: str = "", tavily: str = "", openai: str = "") -> Path:
    path = _store_path(app_dir)
    payload = {
        "serpapi": serpapi.strip(),
        "tavily": tavily.strip(),
        "openai": openai.strip(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return path


def clear_saved_api_keys(app_dir: Path) -> None:
    path = _store_path(app_dir)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "not saved"
    if len(key) <= 8:
        return "saved: ••••"
    return f"saved: {key[:4]}…{key[-4:]}"
