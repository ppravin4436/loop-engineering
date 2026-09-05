"""
Scorer agent — extracts facts and deterministically scores job listings.

Processes unscored listings in bounded batches (rules 18 and 21).
Never calls LLMs directly; delegates extraction to extractor.py and
arithmetic to scoring.py.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.scoring import score_listing

if TYPE_CHECKING:
    from edgedash.planning import StopConditions

logger = logging.getLogger(__name__)


class Scorer:
    """Scorer agent implementing the Agent protocol."""

    name: str = "scorer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
        widen_distribution: bool = False,
    ) -> AgentResult:
        storage.init_db(db_path)

        # Respect stop_conditions from the Orchestrator (rule 29).
        batch_size: int = (
            stop.max_items
            if (stop and stop.max_items is not None)
            else config.score_batch_size
        )
        max_seconds: float | None = (
            stop.max_seconds
            if (stop and stop.max_seconds is not None)
            else config.max_score_seconds
        )

        # widen_distribution=True: clear existing scores so this run re-evaluates
        # them. The extractor cache is intentionally NOT cleared (it only caches
        # facts, not scores) so extraction cost is not paid again. The Scorer
        # then runs score_listing with a modified config that applies a spread
        # amplifier — scores further from the mean are pushed outward by
        # multiplying the deviation from 50 by 1.25, then clamping to [0, 100].
        # This directly addresses score inflation by making the distribution
        # wider without changing which listings rank above which.
        if widen_distribution:
            storage.clear_scores(db_path, limit=batch_size)

        listings = storage.get_unscored_listings(db_path, limit=batch_size)

        if not listings:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="0 unscored listings — nothing to score",
            )

        scores: list[int] = []
        failed_count = 0
        deadline: float | None = (
            time.monotonic() + max_seconds if max_seconds is not None else None
        )

        for listing in listings:
            # Hard time cap — stop processing mid-batch if budget is exhausted
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Scorer hit max_seconds=%s, stopping after %d listings",
                    max_seconds,
                    len(scores) + failed_count,
                )
                break

            try:
                facts = extract(listing, config=config)
                result = score_listing(
                    listing, facts, config,
                    widen_distribution=widen_distribution,
                )
                storage.update_score(
                    db_path=db_path,
                    listing_id=listing["id"],
                    score=result["score"],
                    reason=result["reason"],
                    components=result["components"],
                )
                scores.append(result["score"])
            except Exception as exc:
                failed_count += 1
                logger.warning(
                    "Failed to score listing %s (%s): %s",
                    listing.get("id"),
                    listing.get("title"),
                    exc,
                )

        scored_count = len(scores)
        if scored_count == 0:
            status = "failed" if failed_count > 0 else "ok"
            notes = f"0 scored · {failed_count} failed"
            return AgentResult(
                agent=self.name,
                status=status,
                records_touched=0,
                notes=notes,
            )

        min_score = min(scores)
        max_score = max(scores)
        mean_score = round(sum(scores) / scored_count)
        spread = max_score - min_score
        spread_status = "suspect" if spread < 10 and scored_count > 1 else "OK"

        notes = (
            f"scored {scored_count} · range {min_score}-{max_score} · "
            f"mean {mean_score} · {failed_count} failed · spread {spread_status}"
        )

        status = "suspect" if spread_status == "suspect" else "ok"

        return AgentResult(
            agent=self.name,
            status=status,
            records_touched=scored_count,
            notes=notes,
        )
