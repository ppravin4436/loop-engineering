"""
Shared contract for every EdgeDash agent.

An Agent has one goal and one stop condition. It receives the project config,
a storage path, and an explicit StopConditions instance set by the Orchestrator
(steering rule 29). It does its work and returns an AgentResult.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from edgedash.config import Config

if TYPE_CHECKING:
    # Imported only for type-checking to avoid a circular import at runtime.
    # Agents import base.py; planning.py imports state.py which imports storage.
    # The TYPE_CHECKING guard keeps the runtime import graph clean.
    from edgedash.planning import StopConditions


@dataclass
class AgentResult:
    agent: str
    status: str           # "ok" | "failed" | "suspect"
    records_touched: int
    notes: str = ""


class Agent(Protocol):
    """Structural protocol every agent must satisfy."""

    name: str

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
    ) -> AgentResult:
        ...
