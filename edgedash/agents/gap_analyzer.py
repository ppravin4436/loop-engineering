"""
Gap Analyzer agent — identifies and ranks skill gaps from scored listings.

Deterministic arithmetic only. No model calls (steering rule 22).
Ranks gaps by opportunity cost (sum of fit_score / 100) per rule 24,
writes timestamped snapshots (rule 25), and includes example IDs (rule 26).
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.skills import canonical

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


def compute_gaps(
    listings_with_facts: list[dict[str, Any]],
    user_skills: list[str],
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Compute missing skills across scored listings, ranked by opportunity cost.
    Opportunity cost = sum(listing.fit_score / 100.0) for blocked listings (rule 24).
    """
    user_canon = {canonical(s, aliases) for s in user_skills if s and s.strip()}

    gap_listings: dict[str, list[tuple[str, int]]] = defaultdict(list)
    nice_counts: dict[str, int] = defaultdict(int)

    for item in listings_with_facts:
        score = item.get("fit_score")
        if score is None:
            continue

        facts = item.get("facts") or {}
        req_raw = facts.get("required_skills") or []
        nice_raw = facts.get("nice_to_have") or []

        req_canon = {canonical(s, aliases) for s in req_raw if s and canonical(s, aliases)}
        nice_canon = {canonical(s, aliases) for s in nice_raw if s and canonical(s, aliases)}

        missing_req = {s for s in req_canon if s not in user_canon}
        missing_nice = {s for s in nice_canon if s not in user_canon}

        for skill in missing_req:
            gap_listings[skill].append((str(item["id"]), int(score)))

        for skill in missing_nice:
            nice_counts[skill] += 1

    results: list[dict[str, Any]] = []
    for skill, data in gap_listings.items():
        blocked = len(data)
        opp_cost = round(sum(score / 100.0 for _, score in data), 2)
        mean_score = round(sum(score for _, score in data) / blocked, 1)
        sorted_by_score = sorted(data, key=lambda x: x[1], reverse=True)
        top_score = sorted_by_score[0][1]
        example_ids = [lid for lid, _ in sorted_by_score[:5]]
        confidence = "high" if blocked >= 3 else "low"

        results.append({
            "skill": skill,
            "listings_blocked": blocked,
            "opportunity_cost": opp_cost,
            "mean_score": mean_score,
            "top_score": top_score,
            "example_ids": example_ids,
            "also_nice_to_have": nice_counts.get(skill, 0),
            "confidence": confidence,
        })

    results.sort(key=lambda r: (r["opportunity_cost"], r["listings_blocked"]), reverse=True)
    return results


class GapAnalyzer:
    """GapAnalyzer agent implementing the Agent protocol."""

    name: str = "gap_analyzer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
    ) -> AgentResult:
        storage.init_db(db_path)

        # Respect stop_conditions from the Orchestrator (rule 29).
        max_seconds: float | None = (
            stop.max_seconds
            if (stop and stop.max_seconds is not None)
            else config.max_analyse_seconds
        )
        deadline: float | None = (
            time.monotonic() + max_seconds if max_seconds is not None else None
        )

        listings = storage.get_scored_listings_with_extractions(db_path)

        if not listings:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="0 scored listings to analyse",
            )

        user_skills = getattr(config, "skills", config.my_skills)

        # Check deadline before the O(n) computation
        if deadline is not None and time.monotonic() >= deadline:
            return AgentResult(
                agent=self.name,
                status="failed",
                records_touched=0,
                notes=f"aborted: exceeded max_seconds={max_seconds} before compute_gaps",
            )

        ranked = compute_gaps(listings, user_skills, config.skill_aliases)
        top_10 = ranked[:10]

        now = datetime.now(timezone.utc)
        import uuid
        run_id = f"gap_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        computed_at = now.isoformat(timespec="seconds")

        storage.save_gap_snapshot(db_path, run_id, computed_at, top_10)

        if top_10:
            top_skill = top_10[0]
            notes = (
                f"{len(top_10)} gaps · top: {top_skill['skill']} "
                f"({top_skill['listings_blocked']} listings, cost {top_skill['opportunity_cost']}) · "
                f"{len(listings)} listings analysed"
            )
        else:
            notes = f"0 gaps · {len(listings)} listings analysed"

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(top_10),
            notes=notes,
        )
