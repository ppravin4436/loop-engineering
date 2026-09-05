"""
System state inspection (steering rules 28, 31).

read_state(config, now) -> SystemState

Pure I/O: calls cheap storage queries, does arithmetic, returns a dataclass.
`now` is a parameter — datetime.now() is never called here, making this
fully testable without monkeypatching.

All timestamps stored in the DB are ISO-8601 strings in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from edgedash import storage
from edgedash.config import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string to an aware datetime, or None."""
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hours_since(ts: str | None, now: datetime) -> float | None:
    """Return fractional hours between a stored timestamp and now, or None."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    delta = now - dt
    return delta.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# SystemState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SystemState:
    """
    A snapshot of the system as observed at `observed_at`.

    All fields are derived from cheap storage queries (COUNT / MAX) —
    no full table loads.
    """

    # When this snapshot was taken
    observed_at: datetime

    # Fetch staleness
    last_fetch_at: str | None          # raw ISO string from DB
    hours_since_fetch: float | None    # None means never fetched

    # Scoring backlog
    unscored_count: int

    # Gap analysis staleness
    gaps_computed_at: str | None       # raw ISO string of latest gap run
    gaps_stale: bool                   # True if any score is newer than the gap snapshot

    # Last completed cycle
    last_cycle_verdict: str | None     # "ok" | "partial" | "failed" | None
    last_cycle_at: str | None          # finished_at of the last cycle_summary row

    def __str__(self) -> str:
        fetch_age = (
            f"{self.hours_since_fetch:.1f}h ago"
            if self.hours_since_fetch is not None
            else "never"
        )
        return (
            f"SystemState("
            f"fetch={fetch_age}, "
            f"unscored={self.unscored_count}, "
            f"gaps_stale={self.gaps_stale}, "
            f"last_cycle={self.last_cycle_verdict or 'none'}"
            f")"
        )


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def read_state(config: Config, now: datetime) -> SystemState:
    """
    Read cheap system metrics from storage and return a SystemState snapshot.

    Parameters
    ----------
    config : Config
        Provides db_path; no other fields are used here.
    now : datetime
        The reference time for all age calculations. Must be timezone-aware
        (UTC). Injected rather than called internally so callers control it
        and tests are deterministic.
    """
    db = config.db_path

    # 1. Fetch staleness — single MAX query
    last_fetch = storage.last_fetch_time(db)
    hours_since = _hours_since(last_fetch, now)

    # 2. Scoring backlog — single COUNT query
    unscored = storage.count_unscored(db)

    # 3. Gap staleness — two MAX queries, compared as strings (ISO sorts lexically)
    gaps_at = storage.last_gap_computed_at(db)
    last_score = storage.last_scored_at(db)

    # gaps_stale is True when:
    #   - we have never computed gaps (gaps_at is None), OR
    #   - a listing was scored more recently than the gap snapshot
    if gaps_at is None:
        gaps_stale = True
    elif last_score is not None and last_score > gaps_at:
        gaps_stale = True
    else:
        gaps_stale = False

    # 4. Last cycle outcome — single indexed query
    summary = storage.last_cycle_summary(db)
    last_verdict = summary["status"] if summary else None
    last_cycle_at = summary["finished_at"] if summary else None

    return SystemState(
        observed_at=now,
        last_fetch_at=last_fetch,
        hours_since_fetch=hours_since,
        unscored_count=unscored,
        gaps_computed_at=gaps_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=last_verdict,
        last_cycle_at=last_cycle_at,
    )
