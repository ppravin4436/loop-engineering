# EdgeDash — Career Intelligence Dashboard

An automated, self-hosted job-market intelligence dashboard. Agents run on a schedule to fetch live job listings, score them against your profile, analyze skill gaps, and surface trends — all visualized in a Streamlit dashboard with a natural-language "Ask your data" panel backed by deterministic, parameterized queries.

> This repository is the **dashboard process** (read-only UI + query layer). Scheduled agents live in the same codebase but run as a **separate process** and communicate with the dashboard only through the shared database (Rule 49).

---

## 1. Features

| Area | What it does |
|---|---|
| **Pipeline** | Periodic fetch → extract → score → gap-analyze → verify cycle with idempotent retries |
| **Dashboard** | Header metrics, top scored listings, top skill gaps, agent activity log (all verdicts incl. failures) |
| **Ask your data** | Natural language questions routed through a 7-tool deterministic registry + grounded LLM phrasing (no hallucinated data) |
| **Storage** | Dual-dialect: **SQLite** for local dev, **PostgreSQL (Supabase)** for production. Dialect-agnostic via `edgedash/storage.py` (Rule 2) |
| **Graceful degradation** | Page renders + status message even when DB is unreachable or empty; no Python tracebacks shown to users (Rule 50) |
| **Secrets** | All env vars read in exactly one place — `edgedash/config.py` — never logged or interpolated into user-facing output (Rule 48) |

---

## 2. Architecture

```
                  ┌──────────────────────┐
     schedule     │  run_cycle.py (sched │
 ───────────────► │  orchestrator loop)  │
                  └─────────┬────────────┘
                            │ writes via storage.py
                            ▼
                  ┌──────────────────────┐
                  │  PostgreSQL (Supab…  │◄────── env: DATABASE_URL
                  │  - listings          │
                  │  - skill_gaps        │
                  │  - cycle_log         │
                  │  - extraction_cache  │
                  │  - query_log         │
                  └─────────┬────────────┘
                            │ reads via storage.py
                            ▼
                  ┌──────────────────────┐
   user's browser │      app.py          │
  ◄──────────────►│  (Streamlit UI)      │
                  │  • metrics/gaps/log  │
                  │  • "Ask your data"   │
                  └──────────────────────┘
```

### Key modules

- [app.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/app.py) — Streamlit dashboard (read-only; writes only `query_log`)
- [edgedash/storage.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/edgedash/storage.py) — Dialect-agnostic DB layer; entry point: `python -m edgedash.storage --migrate --check`
- [edgedash/config.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/edgedash/config.py) — Config loader (YAML + stdlib `.env` parser; single source of secrets)
- [edgedash/orchestrator.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/edgedash/orchestrator.py) + [run_cycle.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/run_cycle.py) — Scheduled agent pipeline (separate process)
- [edgedash/query/tools.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/edgedash/query/tools.py) — 7 deterministic query tools (`@tool`-decorated registry)
- [edgedash/query/ask.py](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/edgedash/query/ask.py) — Two-call NL pipeline: **Route** (classify + params) → **Execute** (deterministic SQL) → **Phrase** (grounded summary)
- Agents: [fetcher](edgedash/agents/fetcher.py) · [extractor](edgedash/agents/extractor.py) · [scorer](edgedash/agents/scorer.py) · [gap_analyzer](edgedash/agents/gap_analyzer.py) · [verifier](edgedash/agents/verifier.py)
- [tests/](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/tests) — 8 test modules for skills, scoring, planning, gaps, verification, and the query pipeline

---

## 3. Ask your data — Available tools

The 7 registered tools in `edgedash.query.tools.TOOLS`:

| Tool | Purpose |
|---|---|
| `companies_hiring(days=7, limit=15)` | Companies that posted listings recently |
| `best_matches(limit=10, min_score=60)` | Listings ranked by fit score for your profile |
| `top_gaps(limit=10)` | Skill gaps ranked by opportunity cost |
| `gap_detail(skill, days=30)` | Sample listings blocked by a specific skill |
| `trend(skill, days=30)` | 30-day demand trend for a skill |
| `listing_count()` | Total + scored listing counts in DB |
| `skill_demand(limit=15, days=30)` | Most in-demand skills by listing mentions |

All queries read **storage-only** and are bounded by parameter clamping. The LLM never generates SQL or free-form data — it only:
1. Selects which tool to call and parses parameters (Call 1 — Route)
2. Writes a grounded prose summary of the returned `rows` (Call 2 — Phrase)

---

## 4. Getting started

### 4.1 Prerequisites
- Python 3.11+
- (Optional but recommended) A hosted Postgres instance like Supabase — set `DATABASE_URL` in `.env`

### 4.2 Install
```powershell
# Clone
git clone https://github.com/ppravin4436/loop-engineering.git edgedash
cd edgedash

# Create + activate venv (Windows PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install deps
pip install -r requirements.txt
```

### 4.3 Configure
Copy [.env.example](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/.env.example) to `.env` and fill in:
```
GEMINI_API_KEY=...                    # Used only for route + phrase calls
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

Edit [config.yaml](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/config.yaml) with:
- Your target `city`, `role`, `my_skills`, `experience_years`
- `fetch_interval_hours`, `min_fit_score`, `sources`

### 4.4 Initialize the database
```powershell
# Create tables + verify connectivity
python -m edgedash.storage --migrate --check
```

On first run you should see `dialect=postgres` (or `sqlite`) and 5 empty tables:
`listings`, `skill_gaps`, `cycle_log`, `extraction_cache`, `query_log`.

### 4.5 Run a full agent cycle
```powershell
python run_cycle.py
```

### 4.6 Open the dashboard
```powershell
streamlit run app.py
```
Visit the printed `http://localhost:8501`.

---

## 5. Project rules / Steering constraints

The codebase is built around a set of hard rules captured in [.kiro/steering/edgedash.md](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/.kiro/steering/edgedash.md). The most impactful ones for contributors:

| # | Rule |
|---|---|
| 2 | All DB interactions go through `edgedash/storage.py` — no raw driver calls elsewhere |
| 38 | Listing & gap panels read the **last verified cycle** only; the activity log is the exception |
| 47 | Local filesystem is ephemeral — all persistent state lives in the hosted DB |
| 48 | Secrets read from env vars in exactly **one** place (`config.py`); never logged/shown |
| 49 | Scheduler (`run_cycle.py`) and dashboard (`app.py`) are separate processes — share DB only |
| 50 | Page must render with a status message even when DB is empty/unreachable — no user-visible tracebacks |
| 51 | Scheduled jobs: idempotent, bounded by a hard timeout, stay within free-tier limits |

---

## 6. Verify everything is healthy

Run these from the repo root:

```powershell
# 1 — No syntax errors anywhere
python -m compileall -q edgedash tests app.py

# 2 — Config wired up correctly (both True)
python -c "from edgedash.config import load_config; c=load_config(); print('db OK:', c.db_path.startswith('postgres')); print('llm OK:', bool(c.llm_api_key))"

# 3 — DB reachable + all tables present
python -m edgedash.storage --check

# 4 — Query pipeline tests pass
.venv\Scripts\python.exe -m pytest tests/test_query_tools.py tests/test_ask.py -q

# 5 — Full test suite (optional)
.venv\Scripts\python.exe -m pytest tests/ -q
```

---

## 7. Deployment

See [.trae/documents/deploy_streamlit_plan.md](file:///c:/Users/pravi/OneDrive/Desktop/edgedash/.trae/documents/deploy_streamlit_plan.md) for step-by-step Streamlit Community Cloud deployment.

Key points:
- Add `DATABASE_URL` and `GEMINI_API_KEY` as **Secrets** in the deployment platform
- The `run_cycle.py` scheduler must run as a **separate job** (GitHub Actions, cron, Supabase pg_cron, or similar)
- Dashboard and scheduler share **only the database** — no direct RPC

---

## 8. License

Proprietary — © current owner. Open a discussion if you'd like to reuse or extend.
