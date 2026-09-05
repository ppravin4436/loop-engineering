"""
Deterministic planning (steering rules 28–31).

build_plan(state, config) -> Plan

Pure function of (SystemState, Config). No I/O, no randomness, no LLM.
Every agent — whether it runs or is skipped — appears in the Plan with an
explicit reason naming the state value that drove the decision (rule 31).

Data model
----------
    StopConditions  : limits passed to the agent so it never decides its own
                      bounds (rule 29).
    Task            : one agent slot — either RUN or SKIP — with goal, limits,
                      and reason.
    Plan            : ordered list of Tasks plus Plan.render() for printing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator

from edgedash.config import Config
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class Decision(Enum):
    RUN  = auto()
    SKIP = auto()


@dataclass(frozen=True)
class StopConditions:
    """
    Explicit limits the Orchestrator passes to each agent (rule 29).
    Fields that do not apply to a given agent are None.
    """
    max_pages: int | None = None          # Fetcher
    max_listings: int | None = None       # Fetcher
    max_items: int | None = None          # Scorer
    max_seconds: int | None = None        # Scorer, Analyser

    def render(self) -> str:
        parts: list[str] = []
        if self.max_pages     is not None: parts.append(f"max_pages={self.max_pages}")
        if self.max_listings  is not None: parts.append(f"max_listings={self.max_listings}")
        if self.max_items     is not None: parts.append(f"max_items={self.max_items}")
        if self.max_seconds   is not None: parts.append(f"max_seconds={self.max_seconds}")
        return ", ".join(parts) if parts else "—"


@dataclass(frozen=True)
class Task:
    """
    A single agent slot in the Plan.

    decision  : RUN or SKIP
    agent     : canonical agent name used by the registry
    goal      : one-sentence statement of what the agent should accomplish
    stop      : hard limits (populated even for SKIP so the Plan is self-documenting)
    reason    : the state value(s) that caused the decision, e.g.
                  "hours_since_fetch=7.2 >= threshold=6"
                  "skipped: unscored_count=0"
    """
    decision: Decision
    agent: str
    goal: str
    stop: StopConditions
    reason: str

    def render(self) -> str:
        tag = "RUN " if self.decision is Decision.RUN else "SKIP"
        return (
            f"  [{tag}] {self.agent:<14}  "
            f"goal={self.goal!r}  "
            f"stop=({self.stop.render()})  "
            f"reason={self.reason!r}"
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    # ---- convenience accessors ----

    def runs(self) -> list[Task]:
        return [t for t in self.tasks if t.decision is Decision.RUN]

    def skips(self) -> list[Task]:
        return [t for t in self.tasks if t.decision is Decision.SKIP]

    def __iter__(self) -> Iterator[Task]:
        return iter(self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)

    # ---- rendering (rule 31) ----

    def render(self) -> str:
        """
        Compact printable plan — one line per agent showing decision, goal,
        stop conditions, and the state value that drove the decision.
        Printed by the Orchestrator before any agent runs.
        """
        header = f"  PLAN  ({len(self.runs())} run / {len(self.skips())} skip)"
        divider = "  " + "─" * 58
        lines = [divider, header, divider]
        for task in self.tasks:
            lines.append(task.render())
        lines.append(divider)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decision rules — one private function per agent
# ---------------------------------------------------------------------------

def _decide_fetch(state: SystemState, config: Config) -> Task:
    stop = StopConditions(
        max_pages=config.max_fetch_pages,
        max_listings=config.max_fetch_listings,
    )

    # Never fetched — always run
    if state.hours_since_fetch is None:
        return Task(
            decision=Decision.RUN,
            agent="fetcher",
            goal="fetch new job listings from all enabled sources",
            stop=stop,
            reason="hours_since_fetch=never (first run)",
        )

    # Interval elapsed
    threshold = config.fetch_interval_hours
    if state.hours_since_fetch >= threshold:
        return Task(
            decision=Decision.RUN,
            agent="fetcher",
            goal="fetch new job listings from all enabled sources",
            stop=stop,
            reason=(
                f"hours_since_fetch={state.hours_since_fetch:.1f} "
                f">= threshold={threshold}"
            ),
        )

    return Task(
        decision=Decision.SKIP,
        agent="fetcher",
        goal="fetch new job listings from all enabled sources",
        stop=stop,
        reason=(
            f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} "
            f"< threshold={threshold}"
        ),
    )


def _decide_score(state: SystemState, config: Config) -> Task:
    stop = StopConditions(
        max_items=config.score_batch_size,
        max_seconds=config.max_score_seconds,
    )

    if state.unscored_count > 0:
        return Task(
            decision=Decision.RUN,
            agent="scorer",
            goal=f"score up to {config.score_batch_size} unscored listings",
            stop=stop,
            reason=f"unscored_count={state.unscored_count}",
        )

    return Task(
        decision=Decision.SKIP,
        agent="scorer",
        goal=f"score up to {config.score_batch_size} unscored listings",
        stop=stop,
        reason="skipped: unscored_count=0",
    )


def _decide_analyse(state: SystemState, config: Config) -> Task:
    stop = StopConditions(max_seconds=config.max_analyse_seconds)

    # No gap snapshot exists yet
    if state.gaps_computed_at is None:
        return Task(
            decision=Decision.RUN,
            agent="gap_analyzer",
            goal="compute skill-gap snapshot from all scored listings",
            stop=stop,
            reason="gaps_computed_at=never (first analysis run)",
        )

    # Scores have changed since the last snapshot
    if state.gaps_stale:
        return Task(
            decision=Decision.RUN,
            agent="gap_analyzer",
            goal="compute skill-gap snapshot from all scored listings",
            stop=stop,
            reason=(
                f"gaps_stale=True "
                f"(a listing was scored after gaps_computed_at={state.gaps_computed_at})"
            ),
        )

    return Task(
        decision=Decision.SKIP,
        agent="gap_analyzer",
        goal="compute skill-gap snapshot from all scored listings",
        stop=stop,
        reason=(
            f"skipped: gaps_stale=False "
            f"(gaps_computed_at={state.gaps_computed_at})"
        ),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_plan(state: SystemState, config: Config) -> Plan:
    """
    Derive an ordered execution Plan from the current system state and config.

    Pure function — no I/O of any kind. The order is fixed:
      fetcher → scorer → gap_analyzer

    Each Task carries explicit stop conditions set by the Orchestrator (rule 29)
    and a human-readable reason naming the state value that drove the decision
    (rule 31). Skipped agents appear with decision=SKIP, never silently absent.
    """
    return Plan(tasks=[
        _decide_fetch(state, config),
        _decide_score(state, config),
        _decide_analyse(state, config),
    ])
