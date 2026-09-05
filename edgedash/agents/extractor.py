"""
Scorer extraction step — the only Scorer path that calls a model.

extract() returns structured facts from a listing. It never asks for a
score. Cache hits skip the model entirely (steering rules 16 and 18).
"""

from __future__ import annotations

import hashlib
from typing import Any

from edgedash import storage
from edgedash.config import Config, load_config
from edgedash.llm import complete_json

SENIORITY_VALUES = ("junior", "mid", "senior", "lead", "unknown")

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "properties": {
        "required_skills": {"type": "array", "items": {"type": "string"}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}},
        "seniority": {"type": "string", "enum": list(SENIORITY_VALUES)},
        "years_required": {"type": ["integer", "null"]},
        "remote_ok": {"type": ["boolean", "null"]},
    },
}

_PROMPT = """\
Read the job listing below and extract only facts it states explicitly.

Rules:
- Do not infer. Do not guess. Do not evaluate anyone.
- If a value is not stated, use null (years_required, remote_ok) or an empty list (required_skills, nice_to_have).
- seniority must be exactly one of: junior, mid, senior, lead, unknown. Use unknown when seniority is not stated.
- years_required is an integer only when the listing states a number of years. Never invent a number.
- remote_ok is true or false only when the listing states a remote or on-site policy. Otherwise null.
- Return JSON with exactly these keys: required_skills, nice_to_have, seniority, years_required, remote_ok.

Job listing:
{document}
"""


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _listing_text(listing: dict) -> str:
    return listing.get("description") or ""


def _document(listing: dict) -> str:
    title = listing.get("title") or "(not stated)"
    company = listing.get("company") or "(not stated)"
    location = listing.get("location") or "(not stated)"
    description = _listing_text(listing) or "(not stated)"
    return (
        f"Title: {title}\n"
        f"Company: {company}\n"
        f"Location: {location}\n"
        f"Description:\n{description}"
    )


def _normalise_skills(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        folded = name.strip().lower()
        if folded:
            out.append(folded)
    return out


def _normalise(payload: dict) -> dict:
    return {
        "required_skills": _normalise_skills(payload.get("required_skills") or []),
        "nice_to_have": _normalise_skills(payload.get("nice_to_have") or []),
        "seniority": payload["seniority"],
        "years_required": payload["years_required"],
        "remote_ok": payload["remote_ok"],
    }


def extract(listing: dict, *, config: Config | None = None) -> dict:
    cfg = config if config is not None else load_config()
    storage.init_db(cfg.db_path)

    text_hash = description_hash(_listing_text(listing))
    cached = storage.get_extraction(cfg.db_path, text_hash)
    if cached is not None:
        return _normalise(cached)

    prompt = _PROMPT.format(document=_document(listing))
    raw = complete_json(prompt, EXTRACTION_SCHEMA, config=cfg)
    result = _normalise(raw)
    storage.put_extraction(cfg.db_path, text_hash, result)
    return result
