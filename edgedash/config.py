"""
Load project configuration from config.yaml at the repo root.

All user-specific values (role, city, skills, etc.) live here.
No other module should reference os.environ or read config files directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # PyYAML


_REPO_ROOT = Path(__file__).parent.parent
_CONFIG_FILE = _REPO_ROOT / "config.yaml"
_ENV_FILE = _REPO_ROOT / ".env"


def _load_dotenv(path: Path = _ENV_FILE) -> None:
    """Parse a simple KEY=VALUE .env file into os.environ (stdlib only).

    Rule 46 — no third-party dependency. Supports:
      KEY=value           # basic assignment
      KEY="value"         # double-quoted, surrounding quotes stripped
      KEY='value'         # single-quoted, surrounding quotes stripped
      # comment           # ignored
      <blank line>        # ignored

    Existing env vars are never overwritten — the shell wins over .env.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env_secret(key: str) -> str | None:
    """Load a secret from the environment. All secret reads go through here.

    Rules 4 + 48: secrets live in env vars, loaded in exactly one place.
    No other module reads os.environ for secrets.
    """
    value = os.environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


_DEFAULTS: dict[str, Any] = {
    "target_role": "Data Analyst",
    "target_city": "Remote",
    "keywords": [],
    "my_skills": [],
    "experience_years": 0,
    "db_path": "edgedash.db",
    "min_fit_score": 60,
    "sources": ["arbeitnow"],
    "use_mock_fetcher": False,
    "llm_provider": "gemini",
    "llm_model": "gemini-3.6-flash",
    "score_batch_size": 25,
    "fetch_interval_hours": 6,
    "max_score_seconds": 300,
    "max_analyse_seconds": 120,
    "max_fetch_pages": 5,
    "max_fetch_listings": 200,
    "target_seniority": "mid",
    "score_weights": {
        "skill_match": 0.45,
        "seniority_fit": 0.25,
        "location_fit": 0.15,
        "recency": 0.15,
    },
    "skill_aliases": {},
    # ---- Verification thresholds (rule 39) ----
    # min_score_spread: catches score inflation where all values cluster together
    "min_score_spread": 10,
    # min_score_stdev: catches a constant-output scorer (all scores identical)
    "min_score_stdev": 5,
    # max_empty_extraction_pct: catches a broken extractor returning no skills
    "max_empty_extraction_pct": 20,
    # max_skills_per_listing: catches a model that returned a sentence as one skill
    "max_skills_per_listing": 20,
    # min_gap_sample: catches ranking a gap computed from a single listing (rumour)
    "min_gap_sample": 3,
    # max_data_age_days: catches running on stale data when the fetch failed silently
    "max_data_age_days": 3,
}


@dataclass
class Config:
    target_role: str = "Data Analyst"
    target_city: str = "Remote"
    keywords: list[str] = field(default_factory=list)
    my_skills: list[str] = field(default_factory=list)
    experience_years: int = 0
    db_path: str = "edgedash.db"
    min_fit_score: int = 60
    sources: list[str] = field(default_factory=lambda: ["arbeitnow"])
    use_mock_fetcher: bool = False
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.6-flash"
    llm_api_key: str | None = None
    score_batch_size: int = 25
    fetch_interval_hours: int = 6
    max_score_seconds: int = 300
    max_analyse_seconds: int = 120
    max_fetch_pages: int = 5
    max_fetch_listings: int = 200
    target_seniority: str = "mid"
    score_weights: dict[str, float] = field(
        default_factory=lambda: {
            "skill_match": 0.45,
            "seniority_fit": 0.25,
            "location_fit": 0.15,
            "recency": 0.15,
        }
    )
    skill_aliases: dict[str, str] = field(default_factory=dict)
    # Verification thresholds — all in config per rule 39
    min_score_spread: int = 10
    min_score_stdev: float = 5.0
    max_empty_extraction_pct: float = 20.0
    max_skills_per_listing: int = 20
    min_gap_sample: int = 3
    max_data_age_days: int = 3

    @property
    def skills(self) -> list[str]:
        """Alias for my_skills for clean access."""
        return self.my_skills


def load_config(path: Path | None = None) -> Config:
    """Read config.yaml and return a validated Config instance.

    Rule 48: all env-secret reads happen in this function, via `_env_secret`.
    `DATABASE_URL` in the environment overrides `db_path` from config.yaml
    (rule 47 — hosted Postgres in production, SQLite file for local dev).
    """
    config_path = path or _CONFIG_FILE

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. "
            "Copy config.yaml.example to config.yaml and fill in your details."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    merged = {**_DEFAULTS, **raw}

    db_path_value = str(merged["db_path"])
    db_url_from_env = _env_secret("DATABASE_URL")
    if db_url_from_env:
        db_path_value = db_url_from_env

    llm_api_key_value = _env_secret("GEMINI_API_KEY")

    return Config(
        target_role=str(merged["target_role"]),
        target_city=str(merged["target_city"]),
        keywords=list(merged["keywords"]),
        my_skills=list(merged["my_skills"]),
        experience_years=int(merged["experience_years"]),
        db_path=db_path_value,
        min_fit_score=int(merged["min_fit_score"]),
        sources=list(merged["sources"]),
        use_mock_fetcher=bool(merged["use_mock_fetcher"]),
        llm_provider=str(merged["llm_provider"]),
        llm_model=str(merged["llm_model"]),
        llm_api_key=llm_api_key_value,
        score_batch_size=int(merged["score_batch_size"]),
        fetch_interval_hours=int(merged["fetch_interval_hours"]),
        max_score_seconds=int(merged["max_score_seconds"]),
        max_analyse_seconds=int(merged["max_analyse_seconds"]),
        max_fetch_pages=int(merged["max_fetch_pages"]),
        max_fetch_listings=int(merged["max_fetch_listings"]),
        target_seniority=str(merged.get("target_seniority", "mid")),
        score_weights=dict(merged.get("score_weights") or _DEFAULTS["score_weights"]),
        skill_aliases=dict(merged.get("skill_aliases") or {}),
        min_score_spread=int(merged["min_score_spread"]),
        min_score_stdev=float(merged["min_score_stdev"]),
        max_empty_extraction_pct=float(merged["max_empty_extraction_pct"]),
        max_skills_per_listing=int(merged["max_skills_per_listing"]),
        min_gap_sample=int(merged["min_gap_sample"]),
        max_data_age_days=int(merged["max_data_age_days"]),
    )
