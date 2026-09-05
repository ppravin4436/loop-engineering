"""
Deterministic scoring logic for job listings.

Pure functions only. No model calls, no network, no LLM imports (steering rule 16).
All scoring arithmetic and reason string generation live here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config

SENIORITY_BANDS: dict[str, int] = {"junior": 0, "mid": 1, "senior": 2, "lead": 3}
DEFAULT_WEIGHTS: dict[str, float] = {
    "skill_match": 0.45,
    "seniority_fit": 0.25,
    "location_fit": 0.15,
    "recency": 0.15,
}


def _calc_skill_match(facts: dict[str, Any], user_skills: list[str]) -> tuple[float, list[str]]:
    """Fraction of required skills matched (nice_to_have at 1/3 weight)."""
    user_set = {s.strip().lower() for s in user_skills if s.strip()}
    req_skills = [s.strip() for s in facts.get("required_skills") or [] if s.strip()]
    nice_skills = [s.strip() for s in facts.get("nice_to_have") or [] if s.strip()]

    matched_req = [s for s in req_skills if s.lower() in user_set]
    missing_req = [s for s in req_skills if s.lower() not in user_set]
    matched_nice = [s for s in nice_skills if s.lower() in user_set]

    total_w = len(req_skills) + (len(nice_skills) / 3.0)
    if total_w == 0.0:
        return 1.0, []

    matched_w = len(matched_req) + (len(matched_nice) / 3.0)
    return min(1.0, max(0.0, matched_w / total_w)), missing_req


def _calc_seniority_fit(facts: dict[str, Any], target_seniority: str) -> float:
    """Exact 1.0, 1 band away 0.6, 2 bands 0.25, 3+ bands 0.0. Unknown -> 0.5."""
    listing_sen = (facts.get("seniority") or "").strip().lower()
    target_sen = (target_seniority or "").strip().lower()
    if listing_sen not in SENIORITY_BANDS or target_sen not in SENIORITY_BANDS:
        return 0.5
    dist = abs(SENIORITY_BANDS[listing_sen] - SENIORITY_BANDS[target_sen])
    return {0: 1.0, 1: 0.6, 2: 0.25}.get(dist, 0.0)


def _calc_location_fit(listing: dict[str, Any], facts: dict[str, Any], target_city: str) -> float:
    """remote_ok true -> 1.0; matches city -> 1.0; unknown -> 0.5; elsewhere -> 0.1."""
    if facts.get("remote_ok") is True:
        return 1.0
    loc = (listing.get("location") or "").strip().lower()
    target = target_city.strip().lower()
    if not loc or loc in ("unknown", "(not stated)", "n/a", "none"):
        return 0.5 if facts.get("remote_ok") is None else 0.1
    return 1.0 if "remote" in loc or (target and target in loc) else 0.1


def _calc_recency(listing: dict[str, Any]) -> float:
    """Posted today -> 1.0, linear decay to 0.0 at 30 days. Null -> 0.5. No crash."""
    posted_raw = listing.get("posted_at")
    if not posted_raw:
        return 0.5
    try:
        dt = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        return max(0.0, min(1.0, 1.0 - (max(0.0, age_days) / 30.0)))
    except Exception:
        return 0.5


def build_reason(
    components: dict[str, float],
    facts: dict[str, Any],
    config: Config,
    listing: dict[str, Any] | None = None,
    missing_skills: list[str] | None = None,
) -> str:
    """Assemble a compact human-readable reason string from numbers (rule 19)."""
    parts: list[str] = []
    user_set = {s.strip().lower() for s in getattr(config, "skills", config.my_skills)}
    req_skills = [s.strip() for s in facts.get("required_skills") or [] if s.strip()]
    matched_req = [s for s in req_skills if s.lower() in user_set]

    if req_skills:
        parts.append(f"{len(matched_req)}/{len(req_skills)} required skills")
    elif facts.get("nice_to_have"):
        nice = facts.get("nice_to_have") or []
        m_nice = [s for s in nice if s.lower() in user_set]
        parts.append(f"{len(m_nice)}/{len(nice)} nice-to-have skills")
    else:
        parts.append("no skills specified")

    sen_map = {1.0: "seniority fits", 0.6: "seniority 1 band off", 0.25: "seniority 2 bands off", 0.0: "seniority mismatch"}
    parts.append(sen_map.get(components.get("seniority_fit", 0.5), "seniority unknown"))

    if facts.get("remote_ok") is True:
        parts.append("remote")
    elif components.get("location_fit") == 1.0:
        parts.append(f"in {config.target_city}")
    elif components.get("location_fit") == 0.5:
        parts.append("location unknown")
    else:
        loc = listing.get("location") if listing else None
        parts.append(f"on-site in {loc}" if loc else "on-site elsewhere")

    posted_raw = listing.get("posted_at") if listing else None
    if posted_raw:
        try:
            dt = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = int((datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
            parts.append("posted today" if days <= 0 else f"posted {days}d ago")
        except Exception:
            parts.append("posted date unknown")
    else:
        parts.append("posted date unknown")

    gaps = missing_skills if missing_skills is not None else [s for s in req_skills if s.lower() not in user_set]
    parts.append(f"gap: {', '.join(gaps)}" if gaps else "no skill gaps")
    return " · ".join(parts)


def score_listing(
    listing: dict[str, Any],
    facts: dict[str, Any],
    config: Config,
    widen_distribution: bool = False,
) -> dict[str, Any]:
    """Score a listing deterministically from extracted facts and user config.

    widen_distribution=True applies a spread amplifier after computing the
    normal [0,100] score: deviation from the midpoint (50) is multiplied by
    1.4, then clamped back to [0,100]. This preserves rank order while pushing
    scores toward the extremes, directly addressing the inflation failure mode
    detected by check_score_spread. The amplifier lives here — not in the
    check — so the Verifier never touches scoring logic.
    """
    user_skills = getattr(config, "skills", config.my_skills)
    target_seniority = getattr(config, "target_seniority", "mid")
    weights = getattr(config, "score_weights", DEFAULT_WEIGHTS)

    skill_score, missing_req = _calc_skill_match(facts, user_skills)
    seniority_score = _calc_seniority_fit(facts, target_seniority)
    location_score = _calc_location_fit(listing, facts, config.target_city)
    recency_score = _calc_recency(listing)

    components = {
        "skill_match": round(skill_score, 4),
        "seniority_fit": round(seniority_score, 4),
        "location_fit": round(location_score, 4),
        "recency": round(recency_score, 4),
    }

    w_skill = weights.get("skill_match", DEFAULT_WEIGHTS["skill_match"])
    w_sen = weights.get("seniority_fit", DEFAULT_WEIGHTS["seniority_fit"])
    w_loc = weights.get("location_fit", DEFAULT_WEIGHTS["location_fit"])
    w_rec = weights.get("recency", DEFAULT_WEIGHTS["recency"])
    total_w = (w_skill + w_sen + w_loc + w_rec) or 1.0

    raw = (skill_score * w_skill + seniority_score * w_sen + location_score * w_loc + recency_score * w_rec) / total_w
    score = max(0, min(100, int(round(raw * 100))))

    if widen_distribution:
        # Amplify deviation from midpoint to increase spread.
        # Example: score=55 → deviation=5 → amplified=7 → new score=57
        #          score=70 → deviation=20 → amplified=28 → new score=78
        #          score=30 → deviation=-20 → amplified=-28 → new score=22
        deviation = score - 50
        score = max(0, min(100, int(round(50 + deviation * 1.4))))

    reason = build_reason(components, facts, config, listing, missing_req)

    return {"score": score, "reason": reason, "components": components}
