"""
Real Fetcher agent.

Iterates the enabled sources from config.sources, calls fetch() on each,
normalises rows into storage-ready dicts, and writes via upsert_listings.

Per steering rule 12: a failing source never kills the cycle. Each source
runs inside its own try/except; failures are logged and execution continues.

Per steering rule 29: stop_conditions are set by the Orchestrator.
max_listings caps the total rows written across all sources in this run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config

# Side-effect import: runs @register decorators before SOURCES is queried.
import edgedash.sources.arbeitnow  # noqa: F401
from edgedash.sources.base import SOURCES
from edgedash.sources.http import SourceError

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_storage_row(normalised: dict, fetched_at: str) -> dict:
    """
    Convert a Source-normalised dict into the shape storage.upsert_listings
    expects. Uses storage.make_listing_id — one id implementation (rule 2).
    """
    source = normalised.get("source") or ""
    url = normalised.get("url") or ""
    return {
        "id": storage.make_listing_id(source, url),
        "title": normalised.get("title") or "",
        "company": normalised.get("company") or "",
        "location": normalised.get("location") or "",
        "url": url,
        "description": normalised.get("description"),
        "source": source,
        "posted_at": normalised.get("posted_at"),
        "fetched_at": fetched_at,
        "fit_score": None,
        "fit_reason": None,
    }


class Fetcher:
    name: str = "fetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
    ) -> AgentResult:
        fetched_at = _now_utc()

        # Respect stop_conditions from the Orchestrator (rule 29).
        # Fall back to config values when stop is not provided.
        max_listings: int = (
            stop.max_listings
            if (stop and stop.max_listings is not None)
            else config.max_fetch_listings
        )

        source_summaries: list[str] = []
        total_new = 0
        total_fetched = 0

        for source_name in config.sources:
            # Hard cap: stop fetching once max_listings is reached
            if total_fetched >= max_listings:
                source_summaries.append(
                    f"{source_name}: SKIPPED (max_listings={max_listings} reached)"
                )
                continue

            source_cls = SOURCES.get(source_name)
            if source_cls is None:
                msg = f"Source '{source_name}' is not registered. Check config.sources."
                print(f"  ⚠  fetcher: {msg}")
                storage.log_cycle(
                    db_path=db_path,
                    agent=f"source:{source_name}",
                    started_at=fetched_at,
                    finished_at=_now_utc(),
                    records_touched=0,
                    status="failed",
                    notes=msg,
                )
                source_summaries.append(f"{source_name}: FAILED (not registered)")
                continue

            # Per-source try/except — one dead board never kills the rest (rule 12)
            source_started = _now_utc()
            try:
                source_instance = source_cls()
                raw_rows: list[dict] = source_instance.fetch(config)

                # Cap to the remaining budget under max_listings
                remaining = max_listings - total_fetched
                if len(raw_rows) > remaining:
                    raw_rows = raw_rows[:remaining]

                storage_rows = [_to_storage_row(r, fetched_at) for r in raw_rows]
                new_count = storage.upsert_listings(db_path, storage_rows)
                total_new += new_count
                total_fetched += len(raw_rows)

                storage.log_cycle(
                    db_path=db_path,
                    agent=f"source:{source_name}",
                    started_at=source_started,
                    finished_at=_now_utc(),
                    records_touched=new_count,
                    status="ok",
                    notes=f"{len(raw_rows)} fetched, {new_count} new",
                )
                source_summaries.append(
                    f"{source_name}: {len(raw_rows)} rows ({new_count} new)"
                )

            except (SourceError, Exception) as exc:
                short = type(exc).__name__
                print(f"  ⚠  fetcher: source '{source_name}' failed — {exc}")
                storage.log_cycle(
                    db_path=db_path,
                    agent=f"source:{source_name}",
                    started_at=source_started,
                    finished_at=_now_utc(),
                    records_touched=0,
                    status="failed",
                    notes=str(exc),
                )
                source_summaries.append(f"{source_name}: FAILED ({short})")

        notes = " | ".join(source_summaries) if source_summaries else "no sources configured"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=total_new,
            notes=notes,
        )
