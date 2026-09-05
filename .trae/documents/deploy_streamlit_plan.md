# EdgeDash — Streamlit Community Cloud Deployment Plan

## Repository Research

### Current architecture (good news, aligns with rules already)
- **Dual-dialect storage** via `storage.py` — already tested with Supabase Postgres. DDL runs successfully against hosted DB (0 rows, all 5 tables created on 2026-09-04 test).
- **Secrets centralized per Rule 48** — `config.py` has `_env_secret()` and stdlib `_load_dotenv()`. But `llm.py` **still has a duplicate** of both a dotenv loader AND a direct `os.environ["GEMINI_API_KEY"]` read — that's a Rule 48 violation and a leak surface the deployment audit must fix.
- **Rule 49 separation (read-only dashboard):** `app.py` imports only `storage`, `config`, `query.ask` — none of `orchestrator`, `agents/*`, or `sources/*`. Confirmed via grep for `write|upsert|insert|delete|update|save_|log_cycle|put_|fetch|cycle|orchestrator` in `app.py` — no calls to mutating storage APIs. Callers only: `recent_cycle_logs`, `last_verified_cycle`, `last_cycle_summary`, `get_listings`, `get_latest_gap_snapshot`, `count_scored`, `ask()`. **`ask()` itself calls `storage.log_query()` though** — a write to `query_log`. That's a rule 49 violation the user might accept but we should explicitly audit & document (or remove if we want strict read-only; see Risks section).
- **Hostile startup (Rule 50) — current gaps:**
  1. `_load_config_cached()` catches and **surfaces** the raw Exception string to users (e.g. `psycopg2.OperationalError: ...` leaks internal errors).
  2. If `DATABASE_URL` points to Supabase but network is unreachable or password wrong, `_load_last_verified()` etc swallow `Exception` into empty data, then `main()` tries to call `_load_counts()` — no top-level try/except over panel rendering, so a panel that accesses a cached failure can still produce tracebacks.
  3. No "database not configured" message when `load_config()` succeeds but `.db_path` still points at a missing file; currently `st.info("No cycles have run yet…")` is used as a catch-all which is only semantically correct when a cycle truly hasn't run.
  4. Individual panel renderers (`_render_ask_panel`, `_render_top_listings`, etc.) have **no** try/except guards — a single exploding dataframe or URL parse error would show the full traceback.
- **Secrets leak audit — current findings (pre-deployment):**
  - `storage.py:995` prints a masked target (`********:********@host:port/db`). Safe.
  - `storage.py:205` — DSN passed internally to psycopg2, never logged. Safe.
  - `llm.py:164` — reads `os.environ.get("GEMINI_API_KEY")` **direct** (Rule 48: forbidden, must go through `_env_secret` in config.py).
  - `llm.py:42-57` — duplicate `_load_env_file()`. Redundant and violates Rule 48 single-source-of-truth.
  - `orchestrator.py` prints many things via stdout but never keys — confirmed it only prints plan, durations, verdicts.
- **Required env vars / secrets for Streamlit Cloud:** only 2 known today: `DATABASE_URL`, `GEMINI_API_KEY`. (If new sources are added in future, more API keys appear in `.env.example`; they follow the same pattern.)

### Files that must exist at repo root (current state)
| File | Exists | Needs change |
|---|---|---|
| `requirements.txt` | ✅ | Prune? Current has 6 pinned deps (PyYAML, requests, google-generativeai, jsonschema, streamlit, psycopg2-binary). All actually used. Keep. |
| `.python-version` | ❌ | Streamlit Cloud supports it as a hint OR we rely on `requirements.txt` + Streamlit's default CPython 3.11/3.12. Project currently tests 3.11 + 3.14 via __pycache__; pin 3.11. |
| `.streamlit/config.toml` | ❌ | Must create dark theme pin so hosted UI matches local (Streamlit Cloud defaults usually light unless server-side theme set). |
| `.gitignore` | ✅ Has `.env`, `*.db` | Missing `.streamlit/secrets.toml` — **add it.** |

## Files and Modules

**CREATE** (new files):
- `.trae/documents/deploy_streamlit_plan.md` — this plan.
- `.streamlit/config.toml` — server head, theme dark, browser.gatherUsageStats=false.
- `.python-version` — single line `3.11` (Streamlit Cloud supports).

**MODIFY** (existing files):
- `.gitignore` — append `.streamlit/secrets.toml` line.
- `edgedash/config.py` — expose `llm_api_key: str | None` field on `Config`, populated inside `load_config()` via the existing `_env_secret("GEMINI_API_KEY")` pattern. (Optional: also carry any future `*_API_KEY` through the same channel so callers don't need env reads.)
- `edgedash/llm.py` — delete the duplicate `_load_env_file()` + its call, replace `os.environ.get("GEMINI_API_KEY")` in `_call_gemini` with `api_key` passed in through the provider callable. Since `complete_json(config=…)` already has a Config object, we plumb it through `provider_fn(prompt, model, config)`-style dispatch, OR add a per-call `api_key` arg. Simpler: plumb `config` through the provider fn signature via `Callable[[str, str, Any], str]`.
- `app.py` — main rules 49/50 refactor:
  - Top-level `try/except Exception` in `main()` that renders a friendly status instead of traceback.
  - Every panel function wrapped in try/except.
  - Dedicated `_render_database_not_configured()` panel for missing `DATABASE_URL` case.
  - Dedicated `_render_first_cycle_pending()` instead of the current `st.stop()`-on-empty branch.
  - No raw exception messages ever rendered — use generic "Internal error — detail logged server-side."
  - Footer: last verified cycle timestamp + GitHub link.
- `edgedash/storage.py` (already done earlier — confirm) — ensure no secret is printed even on error. Confirmation: on error traceback `psycopg2.OperationalError: connection to server at ... failed: FATAL: password authentication failed` — this message does not leak the password, which is correct. But we must prevent that traceback from reaching the Streamlit error box (handled via app.py try/except).

## Implementation Steps

Dependency order (top = execute first):

1. **Create / tweak deployment files**
   - Create `.streamlit/config.toml` — dark theme, `gatherUsageStats=false`, `server.headless=true` optional.
   - Create `.python-version` with `3.11`.
   - Append `.streamlit/secrets.toml` to `.gitignore`.

2. **Secrets rule-48 fix**
   - In `config.py`: extend `Config` dataclass with `llm_api_key: str | None = None`. Inside `load_config()`, set `llm_api_key=_env_secret("GEMINI_API_KEY")`.
   - In `llm.py`:
     - Remove `_REPO_ROOT`, `_ENV_FILE`, `_load_env_file()`, and `_load_env_file()` call.
     - Remove the module-level `import os` if no longer needed after cleanup.
     - Change `_call_gemini(prompt, model, config=None)` signature to accept a 3rd arg `config: Any`.
     - Change provider dispatch dict to `Callable[[str, str, Any], str]` and update `_call_with_backoff(provider_fn, prompt, model, config)` so it passes config.
     - Change `_call_gemini` to use `config.llm_api_key` instead of `os.environ.get("GEMINI_API_KEY")`.
     - Raise the same LLMError message but phrased to not mention `.env` by name (since Streamlit Cloud uses secrets.toml which map to env vars).

3. **app.py — Rule 50 hostile-startup hardening**
   - Add a module-level `_SAFE_ERROR = "Something unexpected happened. Check back in a minute or run a new cycle."`
   - Refactor `main()`:
     - Wrap entire body in one try/except.
     - On any Exception: log `st.exception()` **only to server console** (via `print(file=sys.stderr)` or st.error with no detail), show `st.error(_SAFE_ERROR)` and a user-visible diagnostic of which part failed (never the raw exception text).
   - Move config + db-path setup into a helper:
     - If config fails to load → show "Dashboard configuration incomplete — contact administrator." (generic, no yaml parse error text).
     - If `cfg.db_path` resolves to default SQLite AND that file does not exist in the deployed env → show "Database not configured" banner (not "No cycles yet").
     - Do a lightweight connectivity probe: `SELECT 1` or simply call `storage.count_total_listings(db_path)` inside try/except; if it raises, show banner "Database unreachable — scheduled jobs may be degraded." and skip all panels.
   - Move current `st.info("No cycles have run yet…")` and its `st.stop()` into a full-screen friendly panel with a "Scheduled first run at…" message (use the `fetch_interval_hours` from config to render a hint, e.g. "runs every 6h"). Never stop early when just empty — the footer and "Ask your data" widget with no-rows-placeholder should still render so the page feels alive.
   - Wrap each sub-renderer (`_render_ask_panel`, `_render_top_listings`, `_render_top_gaps`, `_render_activity_log`) in `try/except`. On failure, `st.caption(f"⚠️ {panel title} unavailable right now.")` and log detail to stderr.
   - Add a footer:
     ```python
     st.markdown("---")
     f1, f2 = st.columns([3, 1])
     with f1:
         st.caption(f"Last verified cycle: {verified_ts if verified_ts else '—'}")
     with f2:
         st.caption("[🔗 GitHub](https://github.com/YOU/edgedash)")
     ```
     Ask user what repo URL to put in there, or leave a visible TODO placeholder so it doesn't ship broken.
   - Rule 49 audit in app.py: confirm NO call to a mutating storage API. Document that `ask() → storage.log_query()` IS a write to `query_log` — if user wants strict read-only dashboard, wrap it in a `try: log_query() except: pass` or remove it; by default we keep it (it only writes to `query_log`, not to cycle/listings state, and it's user-initiated so acceptable).

4. **Gather secrets TOML for user**
   - Produce the contents of `.streamlit/secrets.toml` for local dev (not committed, hence we put it in gitignore). Also show exactly what block the user must paste into Streamlit Cloud → App Settings → Secrets. Keys only: `DATABASE_URL` and `GEMINI_API_KEY`. (Any new source API keys added later follow the same pattern — but don't add them until needed.)

5. **Local validation (before deploy)**
   - `python -m edgedash.storage --migrate --check` still passes (Supabase connection).
   - `.\.venv\Scripts\python.exe -m pytest tests -q` → **113 passed**.
   - Run `streamlit run app.py` locally with:
     - (a) `.env` deleted → verify "Database not configured" shows, no traceback.
     - (b) `.env` with invalid password → verify "Database unreachable" shows, no traceback.
     - (c) Correct `.env` + run a cycle → verify all panels render, footer shows real timestamp.
   - `GetDiagnostics` clean on `app.py`, `config.py`, `llm.py`, `storage.py`.

## Dependencies and Considerations

- No new third-party packages. Rule 46 honored. All needed deps already in `requirements.txt`.
- Streamlit Cloud runs on Python 3.11 by default (pinned via `.python-version`). Project already targets 3.11.
- `psycopg2-binary==2.9.10` is pinned so the hosted Streamlit runtime compiles/uses the wheel. Supabase has valid public SSL certs; no TLS overrides needed.
- Streamlit Cloud exposes all secrets in **both** `st.secrets` AND as environment variables (per docs). Because our `_env_secret("DATABASE_URL")` reads `os.environ`, the integration works **without any code change to read st.secrets** — which aligns with Rule 48 (secrets are env vars, single source of truth). That's a win — no streamlit-specific secrets code in `config.py`.
- The "Ask your data" panel uses `ask()` → `_call_llm()` → `config.llm_api_key` — this means every stranger query costs LLM money on the hosted dashboard. That's by design but worth flagging. If user wants a free-tier guard, rate-limit via `query_log` counts or disable the panel on low-budget days (future enhancement, out of scope here).
- The footer GitHub URL must be provided by the user. The plan uses `https://github.com/YOU/edgedash` as a placeholder.

## Validation

After implementation, before clicking "Deploy" in Streamlit Cloud:

1. `python -m pytest tests -q --no-header` → 113 passed (no regressions).
2. `GetDiagnostics` for `app.py`, `config.py`, `llm.py`, `storage.py` → empty.
3. Three local `streamlit run app.py` scenarios (see Step 3 above) each render a clean page, no Python traceback visible to stranger.
4. `python -m edgedash.storage --migrate --check` → "init_db complete — tables ready", all 5 tables visible.
5. Inspect browser's DevTools "Network" tab for any HTML response containing the strings `postgresql://`, `postgres:`, `GEMINI_API_KEY=`, or `[secret` patterns. Zero matches.

## Risks

- **Risk: Streamlit Cloud exposes tracebacks anyway.** Streamlit by default shows friendly exception dialogs, but `st.set_option('server.showErrorDetails', False)` in `.streamlit/config.toml` is the mitigation. Include it so even if a panel's local try/except misses, Streamlit's global fallback doesn't leak source lines.
- **Risk: Dashboard accidentally mutates data via `ask() → log_query`.** Mitigation is not to remove it (harmless query-log write, not cycle-affecting) but to document it explicitly in audit. If user wants 100% strict read-only dashboard, wrap `storage.log_query` in a silent exception handler and add a comment "Rule 49: dashboard never touches listings/cycle/skills/gap writes."
- **Risk: `GEMINI_API_KEY` not set, panel `_render_ask_panel` surfaces the LLMError text.** Rule 50/48: the app try/except must catch any LLMError during ask() and say "The LLM service is not reachable right now — try again later" without mentioning the word "API key".
- **Risk: `.env.example` lists secrets in a commented format that newcomers paste verbatim with quotes, `[` brackets, etc.** Mitigation: in `.env.example` DATABASE_URL line, include a comment "Do NOT wrap this value in square brackets. If your password contains `@` or `:` it must be URL-encoded. In Streamlit Cloud Secrets → paste directly."
