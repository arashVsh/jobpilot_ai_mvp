from __future__ import annotations
import json
import os
from typing import Any
import requests

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")


def has_openai_key(api_key: str | None = None) -> bool:
    return bool((api_key or os.getenv("OPENAI_API_KEY", "")).strip())


def _extract_output_text(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text") or content.get("value")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip() if chunks else None


def call_openai_text(system_prompt: str, user_prompt: str, api_key: str | None = None, model: str | None = None, timeout: int = 30) -> str | None:
    key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not key:
        return None
    payload = {
        "model": model or DEFAULT_MODEL,
        "instructions": system_prompt,
        "input": user_prompt,
    }
    try:
        r = requests.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return _extract_output_text(r.json())
    except Exception:
        return None


def call_openai_json(system_prompt: str, user_prompt: str, api_key: str | None = None, model: str | None = None, timeout: int = 30) -> dict[str, Any] | None:
    text = call_openai_text(system_prompt, user_prompt, api_key=api_key, model=model, timeout=timeout)
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1).replace("JSON\n", "", 1).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
