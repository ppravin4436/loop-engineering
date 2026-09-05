"""
EdgeDash — agent activity dashboard.

READ-ONLY. This file never writes data and never triggers a cycle.
The one deliberate, user-initiated exception: `ask()` writes a row to
`query_log` so question history is preserved. All other tables are NEVER
modified from this process.

Per rule 38:
  - Every data panel (listings, gaps) reads from the LAST PASSING CYCLE only,
    via storage.last_verified_cycle().
  - The activity log is the deliberate exception: it shows ALL cycles,
    because the failures are the point of that panel.

Per rule 50:
  - Startup must succeed even if the DB is missing / unreachable / empty.
  - A stranger must never see a Python traceback. Details go to stderr only.
  - Each panel is individually guarded; one failure cannot take down the page.

Per rule 49:
  - No scheduler, orchestrator, fetcher, or scoring agent is imported.
  - No cycle-triggering path exists in this process.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

# Ensure the repo root is importable regardless of how streamlit is launched
_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from edgedash import storage
from edgedash.config import Config, load_config
from edgedash.query import ask

# ---------------------------------------------------------------------------
# Rule 50 — user-facing error messages. Never leak exceptions verbatim.
# ---------------------------------------------------------------------------

_SAFE_GENERIC = (
    "Something unexpected happened. Check back in a minute or trigger a new "
    "scheduled run."
)
_DB_NOT_CONFIGURED = (
    "Database not configured yet. "
    "Set `DATABASE_URL` in the deployment secrets to connect to the hosted "
    "PostgreSQL database."
)
_DB_UNREACHABLE = (
    "Database is unreachable right now. Scheduled jobs may be running against a "
    "different environment, or credentials may need re-enter the deployment secrets."
)
_ASK_UNAVAILABLE = "This panel is unavailable right now."
_GITHUB_URL = "https://github.com/YOU/edgedash"

# ---------------------------------------------------------------------------
# Page config — must be the first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="EdgeDash",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _safe_stderr(exc: Exception, *, context: str) -> None:
    """Rule 50: detail stays server-side only."""
    print(f"[edgedash.app] ERROR [{context}] {type(exc).__name__}: {exc}", file=sys.stderr)


def _panel_guard(panel_title: str):
    """
    Use inside each `_render_*` function: catch all. Render an apologetic
    caption on failure.

    Usage at the TOP of a panel renderer:
        try:
            ...
        except Exception as exc:
            _panel_failed(panel_title, exc)
            return
    """
    # This is a helper factory; the real pattern is try/except in each renderer.
    raise NotImplementedError


def _panel_failed(panel_title: str, exc: Exception) -> None:
    _safe_stderr(exc, context=f"panel={panel_title}")
    st.caption(f"⚠️  {panel_title} — {_ASK_UNAVAILABLE}")


# ---------------------------------------------------------------------------
# Cached data loaders
# Short TTL: re-reads from storage every 60 s on rerun, not on every widget
# interaction. All loaders swallow Exception so failure is represented as empty / None.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def _load_config_cached() -> tuple[bool, Config | None]:
    """
    Returns (ok, cfg_or_None).
    Rule 50: never re-raise; failure yields (False, None).
    """
    try:
        return True, load_config()
    except Exception as exc:
        _safe_stderr(exc, context="load_config")
        return False, None


@st.cache_data(ttl=60)
def _db_reachable(db_path: str) -> bool:
    """
    Lightweight probe: try to count listings. Returns True on success,
    False on any failure. Rule 50: never raises.
    """
    try:
        storage.count_scored(db_path)
        return True
    except Exception as exc:
        _safe_stderr(exc, context="db_probe")
        return False


@st.cache_data(ttl=60)
def _load_recent_cycles(db_path: str) -> list[dict]:
    try:
        return storage.recent_cycle_logs(db_path, limit=30)
    except Exception as exc:
        _safe_stderr(exc, context="recent_cycle_logs")
        return []


@st.cache_data(ttl=60)
def _load_last_verified(db_path: str) -> dict | None:
    try:
        return storage.last_verified_cycle(db_path)
    except Exception as exc:
        _safe_stderr(exc, context="last_verified_cycle")
        return None


@st.cache_data(ttl=60)
def _load_last_any_cycle(db_path: str) -> dict | None:
    try:
        return storage.last_cycle_summary(db_path)
    except Exception as exc:
        _safe_stderr(exc, context="last_cycle_summary")
        return None


@st.cache_data(ttl=60)
def _load_top_listings(db_path: str, limit: int = 10) -> list[dict]:
    try:
        return storage.get_listings(db_path, limit=limit, min_score=0)
    except Exception as exc:
        _safe_stderr(exc, context="get_listings")
        return []


@st.cache_data(ttl=60)
def _load_top_gaps(db_path: str) -> list[dict]:
    try:
        return storage.get_latest_gap_snapshot(db_path)[:10]
    except Exception as exc:
        _safe_stderr(exc, context="get_latest_gap_snapshot")
        return []


@st.cache_data(ttl=60)
def _load_counts(db_path: str) -> tuple[int, int]:
    """Returns (total_listings, total_scored)."""
    try:
        total = len(storage.get_listings(db_path, limit=100_000))
        scored = storage.count_scored(db_path)
        return total, scored
    except Exception as exc:
        _safe_stderr(exc, context="count_listings_scored")
        return 0, 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VERDICT_COLOURS = {
    "ok":            "#1a9641",
    "partial":       "#f4a900",
    "nothing_to_do": "#5c85d6",
    "degraded":      "#c0392b",
    "failed":        "#c0392b",
}

_VERDICT_ICONS = {
    "ok":            "✓",
    "partial":       "⚠",
    "nothing_to_do": "–",
    "degraded":      "✗",
    "failed":        "✗",
}


def _colour(verdict: str) -> str:
    return _VERDICT_COLOURS.get(verdict, "#888888")


def _icon(verdict: str) -> str:
    return _VERDICT_ICONS.get(verdict, "?")


def _fmt_ts(ts: str | None) -> str:
    """ISO string → human-friendly local-ish display."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d  %H:%M UTC")
    except ValueError:
        return ts


def _duration(row: dict) -> str:
    """Compute duration between started_at and finished_at."""
    try:
        s = datetime.fromisoformat(row.get("started_at") or "")
        e = datetime.fromisoformat(row.get("finished_at") or "")
        secs = int((e - s).total_seconds())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return "—"


def _parse_notes(notes: str | None) -> dict:
    """
    Parse the structured notes string written by _write_cycle_summary.
    Format: "verdict=ok | retries=1 | failed_checks=score_spread | fetcher:ok(3.1s) | ..."
    Returns a dict with keys: verdict, retries, failed_checks, agents.
    """
    out = {"verdict": "?", "retries": "0", "failed_checks": "", "agents": ""}
    if not notes:
        return out
    parts = [p.strip() for p in notes.split("|")]
    agent_parts = []
    for p in parts:
        if p.startswith("verdict="):
            out["verdict"] = p[len("verdict="):]
        elif p.startswith("retries="):
            out["retries"] = p[len("retries="):]
        elif p.startswith("failed_checks="):
            out["failed_checks"] = p[len("failed_checks="):]
        elif p:
            agent_parts.append(p)
    out["agents"] = "  ".join(agent_parts)
    return out


def _next_run_hint(config: Config | None) -> str:
    """
    Produce a "next run is scheduled for <time> hint for the empty-DB message.
    """
    try:
        hours = int(getattr(config, "fetch_interval_hours", 6) or 6)
    except Exception:
        hours = 6
    soon = datetime.now(timezone.utc) + timedelta(hours=hours)
    ts = soon.strftime("%Y-%m-%d  %H:%M UTC")
    return f"No cycles yet — first run is scheduled for {ts} (every {hours}h)"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def main() -> None:
    # Rule 50: one umbrella try/except around the whole page.
    try:
        _main_inner()
    except Exception as exc:
        _safe_stderr(exc, context="main")
        st.error(_SAFE_GENERIC)
        _render_footer(None, None)


def _main_inner() -> None:
    # ---- 1. Config + DB availability
    ok_cfg, cfg = _load_config_cached()
    if not ok_cfg or cfg is None:
        st.error(
            "Dashboard configuration is incomplete. "
            "Contact the workspace owner to finish setup."
        )
        _render_footer(None, None)
        return

    db_path: str = cfg.db_path

    # Rule 48: never render / print connection string, even truncated.
    # No code in this module ever interpolates db_path to st.markdown/print.

    reachable = _db_reachable(db_path)
    if not reachable:
        # Rule 50: distinguish "not configured" (SQLite default) vs "reachable".
        looks_default_sqlite = (
            isinstance(db_path, str)
            and not db_path.startswith("postgres")
            and db_path.endswith((".db", ".sqlite3"))
        )
        if looks_default_sqlite:
            st.warning(_DB_NOT_CONFIGURED, icon="🗄️")
        else:
            st.warning(_DB_UNREACHABLE, icon="🗄️")
        # Still render a polite page — header, footer, empty-friendly Ask panel.
        st.markdown("## 📊 EdgeDash — Career Intelligence")
        _render_footer(None, cfg)
        return

    # =========================================================================
    # Section 1 — Header strip
    # =========================================================================

    st.markdown("## 📊 EdgeDash — Career Intelligence")

    try:
        last_verified = _load_last_verified(db_path)
    except Exception as exc:
        _safe_stderr(exc, context="load_last_verified_main")
        last_verified = None
    try:
        last_any = _load_last_any_cycle(db_path)
    except Exception as exc:
        _safe_stderr(exc, context="load_last_any_main")
        last_any = None
    try:
        total, scored = _load_counts(db_path)
    except Exception as exc:
        _safe_stderr(exc, context="load_counts_main")
        total, scored = 0, 0

    verified_ts = _fmt_ts(last_verified.get("finished_at")) if last_verified else None

    # Empty DB but reachable → friendly status + footer + ask panel (no stop).
    if last_any is None:
        st.info(_next_run_hint(cfg), icon="⏱️")
        try:
            _render_ask_panel(cfg)
        except Exception as exc:
            _panel_failed("Ask your data", exc)
        _render_footer(verified_ts, cfg)
        return

    latest_status = last_any.get("status", "?")
    latest_ts = _fmt_ts(last_any.get("finished_at"))

    # Header metrics
    try:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total listings", total)
        with col2:
            st.metric("Scored", scored)
        with col3:
            st.metric("Latest cycle", latest_ts)
        with col4:
            colour = _colour(latest_status)
            icon_s = _icon(latest_status)
            st.markdown(
                f"<div style='padding:8px 0'>"
                f"<span style='font-size:0.85rem;color:#888'>Verdict</span><br>"
                f"<span style='font-size:1.4rem;font-weight:700;color:{colour}'>"
                f"{icon_s} {latest_status.upper()}</span></div>",
                unsafe_allow_html=True,
            )
    except Exception as exc:
        _panel_failed("Header metrics", exc)

    # Stale-data banner (rule 38 — never silently show unverified data)
    try:
        if latest_status not in ("ok", "nothing_to_do"):
            if verified_ts:
                st.warning(
                    f"⚠ The most recent cycle is **{latest_status}**. "
                    f"The data panels below show the last **verified** cycle "
                    f"from **{verified_ts}**.",
                    icon="⚠️",
                )
            else:
                st.error(
                    "No verified cycle exists yet. Data panels are unavailable "
                    "until at least one cycle passes verification.",
                    icon="🚫",
                )
                try:
                    _render_activity_log(db_path)
                except Exception as exc:
                    _panel_failed("Agent activity log", exc)
                _render_footer(verified_ts, cfg)
                return
    except Exception as exc:
        _safe_stderr(exc, context="stale_banner")

    st.divider()

    # =========================================================================
    # Section 2 — Ask your data (Natural language queries, rules 40–46)
    # =========================================================================

    if last_verified is not None:
        try:
            _render_ask_panel(cfg)
        except Exception as exc:
            _panel_failed("Ask your data", exc)
        st.divider()

    # =========================================================================
    # Section 3 — Data panels (read from last verified cycle only)
    # =========================================================================

    if last_verified is None:
        st.caption("No verified cycle yet — data panels will appear after the first passing run.")
    else:
        left, right = st.columns(2)
        with left:
            try:
                _render_top_listings(db_path)
            except Exception as exc:
                _panel_failed("Top scored listings", exc)
        with right:
            try:
                _render_top_gaps(db_path)
            except Exception as exc:
                _panel_failed("Top skill gaps", exc)

    st.divider()

    # =========================================================================
    # Section 4 — Agent activity log
    # =========================================================================

    try:
        _render_activity_log(db_path)
    except Exception as exc:
        _panel_failed("Agent activity log", exc)

    # =========================================================================
    # Footer
    # =========================================================================

    _render_footer(verified_ts, cfg)


# ---------------------------------------------------------------------------
# Natural Language Query Panel (Rules 42–45)
# ---------------------------------------------------------------------------

def _render_ask_panel(config: Config) -> None:
    st.markdown("### 💬 Ask Your Data")
    st.caption("Ask natural language questions about your verified job market data, skill gaps, and hiring trends (Rules 40–46).")

    # Three clickable example buttons
    col1, col2, col3 = st.columns(3)
    example_q = None
    with col1:
        if st.button("🏢 Which companies are hiring?", use_container_width=True):
            example_q = "Which companies are hiring in the last 7 days?"
    with col2:
        if st.button("🎯 What are my top skill gaps?", use_container_width=True):
            example_q = "What are my top 5 skill gaps by opportunity cost?"
    with col3:
        if st.button("📈 How many total listings exist?", use_container_width=True):
            example_q = "How many total and scored job listings are in the database?"

    if "user_question" not in st.session_state:
        st.session_state["user_question"] = ""

    if example_q:
        st.session_state["user_question"] = example_q

    user_query = st.text_input(
        "Ask a question:",
        value=st.session_state.get("user_question", ""),
        placeholder="e.g. Which companies posted jobs recently? or What are my best matches?",
        key="query_input_box",
    )

    if user_query:
        with st.spinner("Analyzing verified data..."):
            try:
                ans = ask(user_query, config=config)
            except Exception as exc:
                # Rule 50: never surface an LLMError / psycopg2 error verbatim.
                _safe_stderr(exc, context="ask.query")
                st.warning(
                    "⚠️  Could not answer that question right now. "
                    "Try again in a moment."
                )
                return

        # 1. Answer text
        if getattr(ans, "answerable", True):
            st.info(f"💡 **Answer:**\n\n{getattr(ans, 'text', '')}")
        else:
            st.warning(f"⚠️  {getattr(ans, 'text', '')}")

        # 2. Scope & tool metadata
        tool_used = getattr(ans, "tool_used", None)
        if tool_used:
            st.caption(
                f"🔧 **Tool:** `{tool_used}` "
                f"({getattr(ans, 'params', '')}) &nbsp;|&nbsp; 📋 "
                f"**Scope:** {getattr(ans, 'summary', '')}"
            )

        # 3. Rule 44: Underlying rows alongside the prose answer
        rows = getattr(ans, "rows", None) or []
        if rows:
            with st.expander(f"📊 Underlying Data Rows ({len(rows)})", expanded=True):
                st.dataframe(rows, use_container_width=True)


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------

def _render_activity_log(db_path: str) -> None:
    """
    Most recent 30 cycle_summary rows, all verdicts.
    Failed/degraded rows are visually distinct.
    This is the exception to rule 38 — failures are the point.
    """
    st.markdown("### 🗂 Agent Activity Log")
    st.caption("Most recent 30 cycles — all verdicts including failures.")

    cycles = _load_recent_cycles(db_path)
    if not cycles:
        st.info("No cycles logged yet.")
        return

    for row in cycles:
        status = row.get("status", "?")
        ts = _fmt_ts(row.get("started_at"))
        dur = _duration(row)
        parsed = _parse_notes(row.get("notes"))
        row_id = row.get("id", ts)   # unique per row for widget keys

        colour = _colour(status)
        icon_s = _icon(status)

        border_colour = colour
        bg = "#1a1a1a" if status in ("degraded", "failed") else "#111111"

        with st.container():
            st.markdown(
                f"""<div style="
                    border-left: 4px solid {border_colour};
                    background: {bg};
                    padding: 8px 12px;
                    margin-bottom: 6px;
                    border-radius: 4px;
                ">
                <span style='color:{colour};font-weight:700;font-size:0.95rem'>
                    {icon_s} {status.upper()}
                </span>
                &nbsp;&nbsp;
                <span style='color:#aaa;font-size:0.85rem'>{ts}</span>
                &nbsp;&nbsp;
                <span style='color:#777;font-size:0.8rem'>⏱ {dur}</span>
                {'&nbsp;&nbsp;<span style="color:#e74c3c;font-size:0.8rem">🔁 retried</span>' if parsed["retries"] not in ("0", "", "?") else ""}
                </div>""",
                unsafe_allow_html=True,
            )

            with st.expander("Details", expanded=False):
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown(f"**Started:** {_fmt_ts(row.get('started_at'))}")
                    st.markdown(f"**Finished:** {_fmt_ts(row.get('finished_at'))}")
                    st.markdown(f"**Duration:** {dur}")
                    st.markdown(f"**Retries:** {parsed['retries']}")
                with c2:
                    if parsed["failed_checks"]:
                        st.markdown(
                            f"**Failed checks:** "
                            f"<span style='color:#e74c3c'>{parsed['failed_checks']}</span>",
                            unsafe_allow_html=True,
                        )
                    if parsed["agents"]:
                        st.markdown(f"**Agents:** `{parsed['agents']}`")
                    raw_notes = row.get("notes") or ""
                    if raw_notes:
                        st.text_area(
                            "Raw notes",
                            value=raw_notes,
                            height=80,
                            disabled=True,
                            label_visibility="collapsed",
                            key=f"notes_{row_id}",
                        )


def _render_top_listings(db_path: str) -> None:
    st.markdown("#### 🏆 Top 10 Scored Listings")
    listings = _load_top_listings(db_path, limit=10)
    if not listings:
        st.caption("No scored listings yet.")
        return

    for i, row in enumerate(listings, 1):
        score = row.get("fit_score") or 0
        title = row.get("title") or "—"
        co = row.get("company") or "—"
        reason = row.get("fit_reason") or ""
        url = row.get("url") or ""

        # Score badge colour: green ≥ 70, amber ≥ 50, red below
        badge_colour = "#1a9641" if score >= 70 else "#f4a900" if score >= 50 else "#c0392b"

        st.markdown(
            f"""<div style="
                border: 1px solid #333;
                border-radius: 4px;
                padding: 8px 10px;
                margin-bottom: 6px;
            ">
            <span style="
                background:{badge_colour};
                color:#fff;
                font-weight:700;
                padding:2px 8px;
                border-radius:3px;
                font-size:0.9rem;
            ">{score}</span>
            &nbsp;
            <span style="font-weight:600">{title}</span>
            <span style="color:#888;font-size:0.8rem"> · {co}</span>
            {'<br><a href="' + url + '" target="_blank" style="font-size:0.75rem;color:#5c85d6">↗ Open</a>' if url else ""}
            </div>""",
            unsafe_allow_html=True,
        )
        if reason:
            with st.expander("Score reason", expanded=False):
                st.caption(reason)


def _render_top_gaps(db_path: str) -> None:
    st.markdown("#### 🎯 Top 10 Skill Gaps")
    gaps = _load_top_gaps(db_path)
    if not gaps:
        st.caption("No gap snapshot yet.")
        return

    for gap in gaps:
        skill = gap.get("skill") or "?"
        blocked = gap.get("listings_blocked", 0)
        cost = gap.get("opportunity_cost", 0.0)
        sample = blocked  # listings_blocked IS the sample size (rule 27)

        # Confidence colour: green ≥ 5, amber ≥ 3, red below
        bar_colour = "#1a9641" if sample >= 5 else "#f4a900" if sample >= 3 else "#c0392b"
        # Bar width proportional to opportunity_cost (max 100% at cost=10)
        bar_pct = min(100, int(cost / 10.0 * 100))

        st.markdown(
            f"""<div style="margin-bottom:6px">
            <div style="display:flex;justify-content:space-between;margin-bottom:2px">
                <span style="font-weight:600">{skill}</span>
                <span style="color:#aaa;font-size:0.8rem">
                    {sample} listing{"s" if sample != 1 else ""} · cost {cost:.1f}
                </span>
            </div>
            <div style="background:#333;border-radius:3px;height:6px">
                <div style="
                    background:{bar_colour};
                    width:{bar_pct}%;
                    height:6px;
                    border-radius:3px;
                "></div>
            </div>
            </div>""",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def _render_footer(verified_ts: str | None, _cfg: Config | None) -> None:
    try:
        st.markdown("---")
        f1, f2 = st.columns([3, 1])
        with f1:
            st.caption(
                f"Last successful verified cycle: {verified_ts if verified_ts else '—'}"
            )
        with f2:
            st.caption(f"[🔗 Source on GitHub]({_GITHUB_URL})")
    except Exception as exc:
        _safe_stderr(exc, context="footer")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
