"""
State-driven Orchestrator (steering rules 28–33, 36).

Cycle flow
----------
1.  Read system state         (state.read_state)
2.  Build a plan              (planning.build_plan)
3.  Print the plan            (rule 31)
4.  Execute RUN tasks         (rule 29 — stop_conditions passed to every agent)
5.  Run Verifier              (rule 36)
6.  On fail: one retry of the failing agent, then re-verify
7.  On second fail: mark "degraded", log, stop
8.  Write one cycle summary   (rule 33 — always, even on degraded)

The Orchestrator resolves agents by name from the registry and knows
nothing else about them (rule 30).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.scorer import Scorer
from edgedash.agents.verifier import Verifier
from edgedash.config import Config
from edgedash.planning import Decision, Plan, build_plan
from edgedash.state import read_state


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
# To add a new agent:
#   1. Import and add it here (one line).
#   2. Add a _decide_<name>() function in planning.py (one function).
#   3. Append it to build_plan() (one line).
# Nothing else changes.

def _placeholder(agent_name: str):
    class _Placeholder:
        name: str = agent_name

        def run(self, config: Config, db_path: str, stop=None) -> AgentResult:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="not implemented yet — skipping",
            )
    return _Placeholder()


def _build_registry(config: Config) -> dict:
    fetcher = MockFetcher() if config.use_mock_fetcher else Fetcher()
    return {
        "fetcher":      fetcher,
        "scorer":       Scorer(),
        "gap_analyzer": GapAnalyzer(),
        "verifier":     Verifier(),
    }


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def _divider(char: str = "─", width: int = 60) -> str:
    return char * width


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _print_state(state) -> None:
    fetch_age = (
        f"{state.hours_since_fetch:.1f}h ago"
        if state.hours_since_fetch is not None
        else "never"
    )
    print(_divider())
    print("  STATE")
    print(_divider())
    print(f"  Last fetch     : {fetch_age}")
    print(f"  Unscored jobs  : {state.unscored_count}")
    print(f"  Gaps stale     : {state.gaps_stale}")
    print(f"  Last cycle     : {state.last_cycle_verdict or 'none'}")


def _print_result(result: AgentResult, elapsed_s: float) -> None:
    icon = "✓" if result.status in ("ok", "suspect") else "✗"
    tag = f" [{result.status.upper()}]" if result.status != "ok" else ""
    print(
        f"  {icon}{tag}  {result.agent:<14}  "
        f"records={result.records_touched:<4}  "
        f"{elapsed_s:.2f}s  —  {result.notes}"
    )


def _print_summary(
    cycle_start: str,
    cycle_end: str,
    outcomes: list[dict],
    verdict: str,
) -> None:
    print(_divider("═"))
    print(f"  CYCLE SUMMARY  [{verdict.upper()}]")
    print(_divider("═"))
    print(f"  Started  : {cycle_start}")
    print(f"  Finished : {cycle_end}")
    for o in outcomes:
        tag = "RAN " if o["ran"] else "SKIP"
        dur = f"{o['elapsed']:.2f}s" if o["elapsed"] is not None else "—"
        print(f"  [{tag}] {o['agent']:<14}  {dur:<8}  {o['status']:<8}  {o['reason']}")
    print(_divider("═"))


# ---------------------------------------------------------------------------
# Agent execution helper
# ---------------------------------------------------------------------------

def _run_agent(
    agent_name: str,
    registry: dict,
    config: Config,
    task_reason: str = "",
    stop=None,
    **kwargs,
) -> tuple[AgentResult, float]:
    """
    Run one agent, catch any exception, return (result, elapsed_seconds).
    kwargs are forwarded to agent.run() for retry-specific flags (e.g.
    widen_distribution=True on the Scorer retry).
    """
    agent = registry.get(agent_name)
    if agent is None:
        return AgentResult(
            agent=agent_name,
            status="failed",
            records_touched=0,
            notes=f"not found in registry: {agent_name}",
        ), 0.0

    t_start = time.monotonic()
    try:
        result = agent.run(config, config.db_path, stop=stop, **kwargs)
    except Exception as exc:
        result = AgentResult(
            agent=agent_name,
            status="failed",
            records_touched=0,
            notes=str(exc),
        )
    elapsed = time.monotonic() - t_start
    return result, elapsed


# ---------------------------------------------------------------------------
# Verification + retry logic (rule 36)
# ---------------------------------------------------------------------------

def _run_verifier(registry: dict, config: Config) -> AgentResult:
    result, elapsed = _run_agent("verifier", registry, config, task_reason="post-cycle check")
    _print_result(result, elapsed)
    storage.log_cycle(
        db_path=config.db_path,
        agent="verifier",
        started_at=_now_utc(),
        finished_at=_now_utc(),
        records_touched=result.records_touched,
        status=result.status,
        notes=result.notes or None,
    )
    return result


def _failing_check_names(verifier_result: AgentResult) -> list[str]:
    """Extract the names of failed checks from the verifier's notes string."""
    # Notes format: "VERDICT: fail — score_spread: FAILED ...; freshness: FAILED ..."
    notes = verifier_result.notes or ""
    if "—" in notes:
        notes = notes.split("—", 1)[1]
    elif "-" in notes:
        notes = notes.split("-", 1)[1]
    names: list[str] = []
    for part in notes.split(";"):
        part = part.strip()
        if "FAILED" in part or "failed" in part.lower():
            # Each check message starts with "check_name: "
            name = part.split(":")[0].strip()
            if name:
                names.append(name)
    return names


def _retry_failing_agents(
    failing_check_names: list[str],
    registry: dict,
    config: Config,
    outcomes: list[dict],
) -> None:
    """
    Re-run the agent(s) responsible for the failing checks, with adjusted
    context. Rule 36: maximum one retry for the whole cycle.
    """
    print(_divider())
    print("  RETRY (verification failed — one attempt)")
    print(_divider())

    # Map check names to the agent responsible and any retry kwargs
    # score_spread → Scorer with widen_distribution=True
    # extraction_sanity → Scorer (extractor problem)
    # gap_sample_size → GapAnalyzer
    # freshness → Fetcher (but stale data can't be fixed mid-cycle; log only)
    _CHECK_TO_AGENT: dict[str, tuple[str, dict]] = {
        "score_spread":        ("scorer",       {"widen_distribution": True}),
        "extraction_sanity":   ("scorer",       {}),
        "gap_sample_size":     ("gap_analyzer", {}),
        "freshness":           ("fetcher",      {}),
    }

    retried: set[str] = set()
    for check_name in failing_check_names:
        entry = _CHECK_TO_AGENT.get(check_name)
        if entry is None:
            print(f"  [RETRY] no agent mapped for check '{check_name}' — skipping")
            continue

        agent_name, extra_kwargs = entry
        if agent_name in retried:
            continue   # already retried this agent in this cycle
        retried.add(agent_name)

        print(f"  [RETRY] {agent_name} (failed check: {check_name})")
        result, elapsed = _run_agent(
            agent_name, registry, config,
            task_reason=f"retry after {check_name} failure",
            **extra_kwargs,
        )
        _print_result(result, elapsed)
        storage.log_cycle(
            db_path=config.db_path,
            agent=f"{agent_name}:retry",
            started_at=_now_utc(),
            finished_at=_now_utc(),
            records_touched=result.records_touched,
            status=result.status,
            notes=result.notes or None,
        )
        outcomes.append({
            "agent":   f"{agent_name}:retry",
            "ran":     True,
            "elapsed": elapsed,
            "status":  result.status,
            "reason":  f"retry after verification check '{check_name}' failed",
        })


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_cycle(config: Config) -> None:
    """
    Execute one full EdgeDash cycle, state-driven.

    Outcome values written to cycle_summary:
      "ok"            — agents ran and verification passed
      "partial"       — an agent failed, but verification passed or was skipped
      "nothing_to_do" — plan had no RUN tasks (success, exit 0)
      "degraded"      — verification failed twice; last-known-good data preserved
    """
    cycle_start = _now_utc()
    now = datetime.now(timezone.utc)

    print()
    print(_divider("═"))
    print(f"  EDGEDASH  —  cycle started {cycle_start}")
    print(_divider("═"))

    storage.init_db(config.db_path)

    # 1. Read state
    state = read_state(config, now)
    _print_state(state)

    # 2. Build plan (pure — no I/O)
    plan: Plan = build_plan(state, config)

    # 3. Print plan before executing (rule 31)
    print(plan.render())

    registry = _build_registry(config)
    outcomes: list[dict] = []
    any_agent_failed = False

    # 4. Nothing to do?
    if not plan.runs():
        for task in plan.skips():
            outcomes.append({
                "agent": task.agent, "ran": False, "elapsed": None,
                "status": "skipped", "reason": task.reason,
            })
        verdict = "nothing_to_do"
        cycle_end = _now_utc()
        print(f"\n  All agents skipped — {verdict}")
        _print_summary(cycle_start, cycle_end, outcomes, verdict)
        _write_cycle_summary(config.db_path, cycle_start, cycle_end, outcomes, verdict,
                             retry_count=0, failed_checks=[])
        return

    print(_divider())
    print("  AGENT RUNS")
    print(_divider())

    # 5. Execute plan tasks (excluding verifier — that runs separately below)
    for task in plan:
        if task.agent == "verifier":
            # Verifier is always run after all other agents; skip here
            continue

        if task.decision is Decision.SKIP:
            outcomes.append({
                "agent": task.agent, "ran": False, "elapsed": None,
                "status": "skipped", "reason": task.reason,
            })
            print(f"  [-]  {task.agent:<14}  skipped — {task.reason}")
            continue

        agent_started = _now_utc()
        result, elapsed = _run_agent(
            task.agent, registry, config, task.reason, stop=task.stop
        )

        if result.status == "failed":
            any_agent_failed = True

        _print_result(result, elapsed)
        storage.log_cycle(
            db_path=config.db_path,
            agent=task.agent,
            started_at=agent_started,
            finished_at=_now_utc(),
            records_touched=result.records_touched,
            status=result.status,
            notes=result.notes or None,
        )
        outcomes.append({
            "agent": task.agent, "ran": True, "elapsed": elapsed,
            "status": result.status, "reason": task.reason,
        })

    # 6. Verify (always runs after the main agents, rule 36)
    print(_divider())
    print("  VERIFICATION")
    print(_divider())

    retry_count = 0
    failed_checks: list[str] = []

    verifier_result = _run_verifier(registry, config)
    outcomes.append({
        "agent": "verifier", "ran": True,
        "elapsed": None,       # logged inside _run_verifier
        "status": verifier_result.status,
        "reason": "post-cycle plausibility check",
    })

    if verifier_result.status == "failed":
        # --- One retry of the failing agents (rule 36) ---
        failed_checks = _failing_check_names(verifier_result)
        _retry_failing_agents(failed_checks, registry, config, outcomes)
        retry_count = 1

        # --- Re-verify once ---
        print(_divider())
        print("  RE-VERIFICATION (after retry)")
        print(_divider())
        second_result = _run_verifier(registry, config)
        outcomes.append({
            "agent": "verifier:retry", "ran": True,
            "elapsed": None,
            "status": second_result.status,
            "reason": "re-verification after retry",
        })

        if second_result.status == "failed":
            # Rule 36: two failures → degraded. Stop. Do not retry again.
            # Rule 38: do NOT overwrite last-known-good data (write degraded
            # status so last_verified_cycle() still returns the previous ok row).
            second_fails = _failing_check_names(second_result)
            failed_checks = list(set(failed_checks + second_fails))
            print(f"\n  ⚠  Verification failed twice — cycle DEGRADED")
            print(f"     Failed checks: {', '.join(failed_checks)}")
            cycle_end = _now_utc()
            _print_summary(cycle_start, cycle_end, outcomes, "degraded")
            _write_cycle_summary(
                config.db_path, cycle_start, cycle_end, outcomes, "degraded",
                retry_count=retry_count, failed_checks=failed_checks,
            )
            return   # stop — do not raise, do not proceed

    # 7. Write one summary row (rule 33)
    verdict = "partial" if any_agent_failed else "ok"
    cycle_end = _now_utc()
    _print_summary(cycle_start, cycle_end, outcomes, verdict)
    _write_cycle_summary(
        config.db_path, cycle_start, cycle_end, outcomes, verdict,
        retry_count=retry_count, failed_checks=failed_checks,
    )


def _write_cycle_summary(
    db_path: str,
    cycle_start: str,
    cycle_end: str,
    outcomes: list[dict],
    verdict: str,
    retry_count: int,
    failed_checks: list[str],
) -> None:
    """
    Write the single summary row required by rule 33.
    Agent='cycle_summary' so last_cycle_summary() and last_verified_cycle()
    can locate it by agent name + status.

    Records: verdict, failed checks, retry count, per-agent outcome.
    """
    ran     = [o for o in outcomes if o["ran"]]
    skipped = [o for o in outcomes if not o["ran"]]

    ran_parts  = [
        f"{o['agent']}:{o['status']}({o['elapsed']:.1f}s)"
        if o["elapsed"] is not None
        else f"{o['agent']}:{o['status']}"
        for o in ran
    ]
    skip_parts = [f"{o['agent']}:skipped" for o in skipped]

    checks_str = (
        f" | failed_checks={','.join(failed_checks)}" if failed_checks else ""
    )
    notes = (
        f"verdict={verdict}"
        f" | retries={retry_count}"
        f"{checks_str}"
        f" | " + " | ".join(ran_parts + skip_parts)
    )

    storage.log_cycle(
        db_path=db_path,
        agent="cycle_summary",
        started_at=cycle_start,
        finished_at=cycle_end,
        records_touched=sum(
            o.get("records_touched", 0) for o in outcomes
            if isinstance(o.get("records_touched"), int)
        ),
        status=verdict,
        notes=notes,
    )
