"""
Verifier agent (steering rules 34–39).

Reads the current cycle's output from storage, runs all plausibility checks,
and returns a verdict in the AgentResult. It writes NO data other than the
verdict log entry. It never repairs, rewrites, or adjusts scores or gaps.

The Orchestrator decides what to do with a failing verdict (rule 34).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.verification import Verdict, run_all_checks

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


class Verifier:
    name: str = "verifier"

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
    ) -> AgentResult:
        now = datetime.now(timezone.utc)

        # --- Read only what the checks need (rule 34: Verifier reads, never writes) ---

        # Scores and Extracted facts: all scored listings with their extracted facts
        scored_rows = storage.get_scored_listings_with_extractions(db_path)
        scores: list[int] = [
            r["fit_score"] for r in scored_rows
            if r.get("fit_score") is not None
        ]

        facts_list: list[dict] = [
            r.get("facts") or {}
            for r in scored_rows
            if r.get("fit_score") is not None
        ]

        # Gap snapshot: most recent run
        latest_run_id = storage.latest_gap_run_id(db_path)
        gaps: list[dict] = (
            storage.get_gap_snapshot_by_run(db_path, latest_run_id)
            if latest_run_id
            else []
        )

        # Freshness: newest listing's fetched_at
        latest_fetch_at = storage.last_fetch_time(db_path)

        # --- Run all checks (pure function — no I/O) ---
        verdict: Verdict = run_all_checks(
            scores=scores,
            facts_list=facts_list,
            gaps=gaps,
            latest_fetch_at=latest_fetch_at,
            config=config,
            now=now,
        )

        # --- Build the AgentResult notes (rule 37: name the check and observed value) ---
        if verdict.passed:
            notes = f"VERDICT: pass — {verdict.summary}"
            status = "ok"
        else:
            fail_details = "; ".join(c.message for c in verdict.failed_checks)
            notes = f"VERDICT: fail — {fail_details}"
            status = "failed"

        return AgentResult(
            agent=self.name,
            status=status,
            records_touched=len(scores),
            notes=notes,
        )
