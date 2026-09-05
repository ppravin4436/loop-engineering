"""
edgedash/query/tools.py

Deterministic query tool registry for natural language queries (steering rules 40–46).
No LLMs, no dynamic SQL generation.
All database access is read-only through edgedash.storage against verified data (rule 38, 46).
Every parameter is treated as untrusted input from a model and is validated / clamped (rule 41).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from edgedash import storage
from edgedash.config import Config, load_config
from edgedash.skills import canonical


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., dict[str, Any]]


# Fixed registry of parameterized query tools (Rule 40)
TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, Any]):
    """
    Decorator to register a query tool in the TOOLS dictionary with
    a JSON-schema parameter specification for the router model.
    """
    def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        spec = ToolSpec(
            name=name,
            description=description.strip(),
            parameters=parameters,
            func=fn,
        )
        TOOLS[name] = spec
        return fn

    return decorator


def _clamp_int(val: Any, default: int, min_val: int, max_val: int) -> int:
    """Validate and clamp untrusted integer input to [min_val, max_val] (Rule 41)."""
    try:
        if val is None:
            return default
        n = int(val)
        return max(min_val, min(n, max_val))
    except (ValueError, TypeError):
        return default


def _verified_guard(db_path: str) -> dict[str, Any] | None:
    """Check rule 46: only read when a verified passing cycle exists."""
    storage.init_db(db_path)
    if storage.last_verified_cycle(db_path) is None:
        return {
            "rows": [],
            "summary": "No verified cycle exists yet. Data is unavailable until at least one cycle passes verification.",
        }
    return None


# ---------------------------------------------------------------------------
# Tool 1: companies_hiring
# ---------------------------------------------------------------------------

@tool(
    name="companies_hiring",
    description=(
        "Returns companies that posted job listings in the last N days along with their listing counts. "
        "Use this tool when the user asks which companies are hiring, who is looking for roles, "
        "or wants hiring volume breakdown by company over a recent timeframe."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of past days to look back for job postings (clamped between 1 and 90, default 7).",
                "default": 7,
            }
        },
        "additionalProperties": False,
    },
)
def companies_hiring(
    days: int = 7,
    *,
    config: Config | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Companies with listings posted in the last N days, with counts."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    clamped_days = _clamp_int(days, default=7, min_val=1, max_val=90)
    current_time = now or datetime.now(timezone.utc)
    cutoff = (current_time - timedelta(days=clamped_days)).isoformat(timespec="seconds")

    rows = storage.get_companies_hiring(db_path, cutoff_date=cutoff)

    total_listings = sum(r["listing_count"] for r in rows) if rows else 0
    company_count = len(rows)

    if not rows:
        summary = f"0 companies found with listings posted in the last {clamped_days} day(s)."
    else:
        summary = (
            f"Found {company_count} companies across {total_listings} "
            f"listings posted in the last {clamped_days} day(s)."
        )

    return {
        "rows": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 2: best_matches
# ---------------------------------------------------------------------------

@tool(
    name="best_matches",
    description=(
        "Returns the highest-scoring job listings ranked by deterministic fit score. "
        "Includes fit score, role title, company, location, and scoring reason. "
        "Use this tool when the user asks for their top job recommendations, best match roles, "
        "highest fit jobs, or what they should apply for."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of top matching listings to return (clamped between 1 and 25, default 10).",
                "default": 10,
            }
        },
        "additionalProperties": False,
    },
)
def best_matches(
    n: int = 10,
    *,
    config: Config | None = None,
) -> dict[str, Any]:
    """Highest-scoring listings with score, title, company, reason."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    clamped_n = _clamp_int(n, default=10, min_val=1, max_val=25)
    rows = storage.get_best_matches(db_path, limit=clamped_n)

    if not rows:
        summary = "0 scored listings found in the database."
    else:
        top_score = rows[0]["fit_score"]
        lowest_score = rows[-1]["fit_score"]
        summary = (
            f"Top {len(rows)} matching listing(s) with fit scores ranging from "
            f"{top_score} down to {lowest_score}."
        )

    return {
        "rows": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 3: top_gaps
# ---------------------------------------------------------------------------

@tool(
    name="top_gaps",
    description=(
        "Returns the top missing skill gaps ranked by opportunity cost from the latest verified snapshot. "
        "Includes skill name, listings blocked, opportunity cost, mean fit score, and top score. "
        "Use this tool when the user asks what skills they are missing, what they should learn next, "
        "or what is holding back their job match scores."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of top skill gaps to return (clamped between 1 and 25, default 5).",
                "default": 5,
            }
        },
        "additionalProperties": False,
    },
)
def top_gaps(
    n: int = 5,
    *,
    config: Config | None = None,
) -> dict[str, Any]:
    """Top skill gaps by opportunity cost, with listings_blocked."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    clamped_n = _clamp_int(n, default=5, min_val=1, max_val=25)
    snapshot = storage.get_latest_gap_snapshot(db_path)
    rows = snapshot[:clamped_n]

    if not rows:
        summary = "0 skill gaps found in the latest verified snapshot."
    else:
        top_skill = rows[0]["skill"]
        top_cost = rows[0]["opportunity_cost"]
        summary = (
            f"Top {len(rows)} skill gap(s) from the latest snapshot. "
            f"Highest opportunity cost is '{top_skill}' (cost {top_cost:.2f}, {rows[0]['listings_blocked']} listings blocked)."
        )

    return {
        "rows": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 4: gap_detail
# ---------------------------------------------------------------------------

@tool(
    name="gap_detail",
    description=(
        "Returns the specific job listings that are currently blocked by a named missing skill. "
        "Use this tool when the user drills down into a specific skill gap (e.g. 'Which jobs need Docker?', "
        "'Why is Python a gap?', 'Show me the listings requiring AWS')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The name of the skill to inspect.",
            }
        },
        "required": ["skill"],
        "additionalProperties": False,
    },
)
def gap_detail(
    skill: str,
    *,
    config: Config | None = None,
) -> dict[str, Any]:
    """The listings blocked by one named skill — rule 26 drill-down exposed as a question."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    if not skill or not isinstance(skill, str):
        return {
            "rows": [],
            "summary": "No skill name provided.",
        }

    # Canonicalise the input skill safely (Rule 41)
    aliases = cfg.skill_aliases if hasattr(cfg, "skill_aliases") else {}
    canon_skill = canonical(skill, aliases)

    if not canon_skill:
        return {
            "rows": [],
            "summary": f"Could not parse skill '{skill}'.",
        }

    snapshot = storage.get_latest_gap_snapshot(db_path)
    matched_gap = next((g for g in snapshot if g["skill"] == canon_skill), None)

    if matched_gap is None:
        return {
            "rows": [],
            "summary": f"Skill '{canon_skill}' is not a tracked gap in the latest verified snapshot.",
        }

    example_ids = matched_gap.get("example_ids") or []
    if isinstance(example_ids, str):
        import json
        try:
            example_ids = json.loads(example_ids)
        except Exception:
            example_ids = []

    listings = storage.get_listings_by_ids(db_path, example_ids)

    summary = (
        f"Skill '{canon_skill}' blocks {matched_gap['listings_blocked']} listing(s) "
        f"with total opportunity cost {matched_gap['opportunity_cost']:.2f}. "
        f"Showing {len(listings)} sample listing(s)."
    )

    return {
        "rows": listings,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 5: trend
# ---------------------------------------------------------------------------

@tool(
    name="trend",
    description=(
        "Returns the historical change and trajectory of skill gap opportunity costs over N weeks. "
        "Shows cost changes, percentage deltas, and whether gaps are increasing, decreasing, or new. "
        "Use this tool when the user asks about skill trends, market changes over time, "
        "or how in-demand skills have evolved recently."
    ),
    parameters={
        "type": "object",
        "properties": {
            "weeks": {
                "type": "integer",
                "description": "Number of past weeks to analyze trends across snapshots (clamped between 1 and 12, default 3).",
                "default": 3,
            }
        },
        "additionalProperties": False,
    },
)
def trend(
    weeks: int = 3,
    *,
    config: Config | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Gap opportunity_cost change over N weeks from the snapshots."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    clamped_weeks = _clamp_int(weeks, default=3, min_val=1, max_val=12)
    current_time = now or datetime.now(timezone.utc)
    cutoff = (current_time - timedelta(weeks=clamped_weeks)).isoformat(timespec="seconds")

    runs = storage.get_gap_snapshot_runs(db_path)
    if not runs:
        return {
            "rows": [],
            "summary": "0 gap snapshot runs recorded in the database.",
        }

    # Filter runs on or after cutoff, or take all available if all are within window
    window_runs = [r for r in runs if r["computed_at"] >= cutoff]
    if len(window_runs) < 2:
        # If fewer than 2 runs in window, fallback to all runs if we have at least 2
        window_runs = runs

    if len(window_runs) < 2:
        latest_snapshot = storage.get_latest_gap_snapshot(db_path)
        return {
            "rows": latest_snapshot,
            "summary": (
                f"Only 1 snapshot run available ({runs[0]['computed_at']}). "
                "Need at least 2 snapshot cycles to compute historical trends."
            ),
        }

    baseline_run = window_runs[0]
    latest_run = window_runs[-1]

    baseline_rows = storage.get_gap_snapshot_by_run(db_path, baseline_run["run_id"])
    latest_rows = storage.get_gap_snapshot_by_run(db_path, latest_run["run_id"])

    base_map = {r["skill"]: r for r in baseline_rows}
    late_map = {r["skill"]: r for r in latest_rows}

    all_skills = sorted(set(base_map.keys()) | set(late_map.keys()))
    comparison_rows: list[dict[str, Any]] = []

    for skill in all_skills:
        prev = base_map.get(skill)
        curr = late_map.get(skill)

        prev_cost = prev["opportunity_cost"] if prev else 0.0
        curr_cost = curr["opportunity_cost"] if curr else 0.0
        prev_blocked = prev["listings_blocked"] if prev else 0
        curr_blocked = curr["listings_blocked"] if curr else 0

        delta_cost = round(curr_cost - prev_cost, 2)
        if prev_cost > 0:
            pct_change = round((delta_cost / prev_cost) * 100.0, 1)
        else:
            pct_change = 100.0 if curr_cost > 0 else 0.0

        if prev is None:
            status = "NEW"
        elif curr is None:
            status = "DROPPED"
        elif delta_cost > 0:
            status = "INCREASING"
        elif delta_cost < 0:
            status = "DECREASING"
        else:
            status = "UNCHANGED"

        comparison_rows.append({
            "skill": skill,
            "current_cost": curr_cost,
            "previous_cost": prev_cost,
            "cost_change": delta_cost,
            "pct_change": pct_change,
            "current_blocked": curr_blocked,
            "previous_blocked": prev_blocked,
            "status": status,
        })

    # Sort by absolute cost change and current cost
    comparison_rows.sort(key=lambda r: (abs(r["cost_change"]), r["current_cost"]), reverse=True)

    summary = (
        f"Compared {len(comparison_rows)} skill(s) across snapshot runs between "
        f"{baseline_run['computed_at']} and {latest_run['computed_at']} ({clamped_weeks} week window)."
    )

    return {
        "rows": comparison_rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 6: listing_count
# ---------------------------------------------------------------------------

@tool(
    name="listing_count",
    description=(
        "Returns aggregate listing totals: total count of listings, number scored, number unscored, "
        "and newest listing fetched date. "
        "Use this tool when the user asks how many jobs exist in the database, what the database size is, "
        "or how many listings have been processed."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)
def listing_count(
    *,
    config: Config | None = None,
) -> dict[str, Any]:
    """Totals: listings, scored, unscored, newest listing date."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    total = storage.count_total_listings(db_path)
    scored = storage.count_scored(db_path)
    unscored = storage.count_unscored(db_path)
    latest_fetch = storage.last_fetch_time(db_path)

    row = {
        "total_listings": total,
        "scored_listings": scored,
        "unscored_listings": unscored,
        "latest_fetch_at": latest_fetch,
    }

    summary = (
        f"Database contains {total} total listings: {scored} scored, "
        f"{unscored} unscored. Latest fetch: {latest_fetch or 'never'}."
    )

    return {
        "rows": [row],
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool 7: skill_demand
# ---------------------------------------------------------------------------

@tool(
    name="skill_demand",
    description=(
        "Returns the demand breakdown for a specific skill across all extracted listings in the database, "
        "differentiating between required (mandatory) and nice_to_have (optional) mentions. "
        "Use this tool when the user asks about the market demand for a single skill "
        "(e.g. 'How popular is Python?', 'Is Docker required or optional?', 'How many jobs mention SQL?')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill name to query demand for.",
            }
        },
        "required": ["skill"],
        "additionalProperties": False,
    },
)
def skill_demand(
    skill: str,
    *,
    config: Config | None = None,
) -> dict[str, Any]:
    """How often one skill appears in required vs nice_to_have."""
    cfg = config or load_config()
    db_path = cfg.db_path

    unverified = _verified_guard(db_path)
    if unverified is not None:
        return unverified

    if not skill or not isinstance(skill, str):
        return {
            "rows": [],
            "summary": "No skill name provided.",
        }

    aliases = cfg.skill_aliases if hasattr(cfg, "skill_aliases") else {}
    canon_skill = canonical(skill, aliases)

    if not canon_skill:
        return {
            "rows": [],
            "summary": f"Could not parse skill '{skill}'.",
        }

    extractions = storage.get_all_extractions_with_metadata(db_path)
    if not extractions:
        return {
            "rows": [],
            "summary": "0 extractions available in the database.",
        }

    req_count = 0
    nice_count = 0

    for item in extractions:
        payload = item.get("payload") or {}
        raw_req = payload.get("required_skills") or []
        raw_nice = payload.get("nice_to_have") or []

        canon_req = {canonical(s, aliases) for s in raw_req if s}
        canon_nice = {canonical(s, aliases) for s in raw_nice if s}

        if canon_skill in canon_req:
            req_count += 1
        elif canon_skill in canon_nice:
            nice_count += 1

    total_mentions = req_count + nice_count
    total_listings = len(extractions)

    if total_mentions == 0:
        return {
            "rows": [],
            "summary": f"Skill '{canon_skill}' was not found in any job listings.",
        }

    pct_of_market = round((total_mentions / total_listings) * 100.0, 1) if total_listings > 0 else 0.0

    row = {
        "skill": canon_skill,
        "required_count": req_count,
        "nice_to_have_count": nice_count,
        "total_mentions": total_mentions,
        "total_listings_checked": total_listings,
        "market_percentage": pct_of_market,
    }

    summary = (
        f"Skill '{canon_skill}' appears in {total_mentions}/{total_listings} listings ({pct_of_market}%): "
        f"{req_count} required, {nice_count} nice-to-have."
    )

    return {
        "rows": [row],
        "summary": summary,
    }
