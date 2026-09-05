---
inclusion: always
---

# EdgeDash — Project Steering Rules

## What This Project Is

EdgeDash is an autonomous AI career intelligence agent. It runs on a schedule,
fetches live job listings, scores them for fit against a user profile, surfaces
skill gaps, verifies its own output, and publishes results to a Streamlit
dashboard.

---

## Architecture

```
Trigger (scheduled)
  └── Orchestrator
        ├── Fetcher        (fetches live job listings)
        ├── Scorer         (scores jobs against profile)
        └── GapAnalyzer    (identifies skill gaps)
              └── Verifier
                    └── Storage
                          └── Dashboard (read-only)
```

**Rules that cannot be deviated from without explicit notice to the user:**

- The Orchestrator reads state and delegates work. It never fetches or scores
  directly.
- Each sub-agent has exactly one goal and one stop condition.
- The Dashboard is read-only. It never writes to storage.

If a proposed change would alter this flow, say so explicitly before
implementing anything.

---

## Hard Rules

### 1. Python version and dependencies
- Target Python 3.11+.
- Reach for the standard library first.
- Before adding any third-party dependency, state what it is, why the standard
  library is insufficient, and what real work it saves. Wait for confirmation.

### 2. Storage access is centralized
- All storage access (reads and writes) goes through a single `storage` module
  that exposes a thin interface.
- No other module may import `sqlite3` directly. This is an absolute rule.
- The storage module must be written so that swapping SQLite for hosted Postgres
  in week 4 is a one-file change.

### 3. No hardcoded user-specific values
- Role, city, keywords, skills profile, and any other user-specific data must
  live in config (a config file or environment variable), never in code.
- If writing code that references these values, read them from config.

### 4. No secrets in code
- API keys, tokens, database URLs, and any other secrets are environment
  variables only.
- Secrets are loaded in exactly one place. No other module reads `os.environ`
  for secrets.

### 5. Cycle logging is mandatory
- Every agent run must write a row to the `cycle_log` table containing:
  - which agent ran
  - timestamp
  - number of records touched
  - pass or fail status
  - retry reason (if applicable)
- This is not optional. Implement it from the start, not as an afterthought.

### 6. Fail loudly
- No bare `except: pass` or silent swallowing of exceptions.
- If something goes wrong, raise or log with enough context to diagnose it.
- Prefer specific exception types over catching `Exception` broadly.

### 7. Type hints and docstrings
- Every function signature must have type hints (parameters and return type).
- Add a docstring only when the intent is not obvious from the function name.
  Do not write docstrings that merely restate the name.

### 8. File length
- Keep files under approximately 150 lines.
- Split a module before it becomes a problem, not after.

---

## Network & Sources

### 9. Sources are pluggable and isolated
- Every external job board lives behind a `Source` class with a uniform
  interface.
- The Fetcher never contains source-specific parsing logic. It only iterates
  registered sources and collects results.
- Adding a new source must never require editing the Fetcher.

### 10. Normalised output contract
- Every Source returns a `list[dict]` where each dict has **exactly** these
  keys: `source`, `external_id`, `title`, `company`, `location`, `url`,
  `description`, `posted_at`, `raw`.
- Missing values are `None`. Never an empty string, never `"N/A"`, never
  a missing key.

### 11. One network helper, used everywhere
- All outbound HTTP calls go through a single helper function with:
  - a 10-second timeout (configurable)
  - 2 retry attempts with exponential backoff
  - an explicit `User-Agent` header
- No bare `requests.get(...)` anywhere else in the codebase.

### 12. A failing source must not kill the cycle
- Catch exceptions per-source inside the Fetcher loop.
- Log each failure to `cycle_log` with `status="failed"` and a descriptive
  note.
- Continue to the next source after a failure.
- One dead job board must never stop the other sources from running.

### 13. Secrets for sources come from environment variables via `.env`
- The `.env` file is gitignored. Never put a literal key in code or in
  `config.yaml`.
- All env loading happens in one place (the existing secrets loader).
- If a required key is absent, that source skips itself and writes a clear
  log line. It does not raise, and it does not crash the cycle.

### 14. Be a good HTTP citizen
- Rate-limit to at most 1 request per second per source.
- Always set a descriptive, honest `User-Agent` header.
- Honour any documented page limits or `robots.txt` constraints for the
  source.

---

## Intelligence & Scoring

### 15. All LLM calls go through one module
- Every call to a language model goes through `edgedash/llm.py`, which exposes
  a single function.
- The provider and model name come from config, never hardcoded.
- Rate-limit to stay inside a free tier: default 1 request per second,
  maximum 15 per minute. The limiter lives inside `llm.py`.
- No other file may import an LLM SDK directly.

### 16. The model extracts facts; Python does the maths
- Never ask a model for a final score, ranking, or numeric rating.
- The model's only job is to extract structured facts from a job description
  (required skills, seniority signals, etc.).
- All scoring arithmetic is deterministic Python in one function.
- The model never sees the scoring weights.

### 17. Every model response is validated before use
- Validate every response against an explicit schema before touching its
  contents.
- A response that fails validation is retried once, then logged as a failure
  for that listing only.
- A validation failure must not crash the cycle or prevent other listings
  from being processed.
- Never call `json.loads` on raw model text without a validation and repair
  path.

### 18. Scoring is idempotent; extraction is cached
- Never re-score a listing that already has a score. Query only
  `WHERE fit_score IS NULL`.
- Cache extraction results keyed on a hash of the job description. The same
  text must never be sent to the model twice.

### 19. Score reasons are generated by code, not by the model
- Every score must carry a human-readable reason string.
- That string is generated by our code from the score components.
- The model must never write free-text justifications that are surfaced to the
  user.

### 20. Log the score distribution on every scoring run
- After each scoring run, log to `cycle_log`: count, min, max, mean, and
  spread of scores in that batch.
- A run where all scores fall within a 10-point range is a suspect run and
  must be flagged explicitly in the log note.

### 21. Cap listings scored per cycle
- A configurable `score_batch_size` (default 25) limits how many listings are
  scored in a single cycle.
- This cap is structural: a cost spike or rate-limit blowup is impossible by
  design, not just by convention.

---

## Aggregate Analysis

### 22. Deterministic aggregate analysis
- Aggregate analysis is deterministic SQL and Python.
- No LLM call may produce, adjust, or rank an aggregate number.
- A model may only SUGGEST canonical groupings for a human to approve.

### 23. Explicit canonicalisation
- Skill names are canonicalised through an explicit alias map in `config.yaml`
  that I own and can read.
- Never auto-merge skill names by model judgement or string similarity alone.

### 24. Fit-score weighted gap ranking
- Gap ranking is weighted by the fit score of the listing the gap came from.
- A gap in a listing I score 20 on is worth far less than a gap in a listing
  I score 85 on.
- Never rank gaps by raw frequency alone.

### 25. Snapshots over overwrites
- Every gap report run writes a timestamped SNAPSHOT.
- Never overwrite the previous report.
- Trend over time is a first-class output, not an afterthought.

### 26. Traceability to source rows
- Every aggregate number must be traceable to the rows that produced it.
- Any reported gap must be able to list the specific listing IDs it was
  computed from.
- No number appears in the dashboard that I cannot drill into.

### 27. Sample size reporting
- Report the sample size alongside every aggregate.
- A gap computed from 3 listings and a gap computed from 90 listings must
  never be presented as equally reliable.

---

## Orchestration

### 28. The Orchestrator decides dynamically — skipping is success
- The Orchestrator reads system state and decides which agents to run based
  on that state.
- It never runs a fixed sequence unconditionally.
- Skipping an agent because there is no work for it is a successful outcome,
  not a failure. Log it as such.

### 29. Every delegation carries explicit limits set by the Orchestrator
- Every agent invocation includes an explicit goal and an explicit stop
  condition (maximum items to process, maximum duration, or both).
- A sub-agent never decides its own limits. The Orchestrator sets them and
  passes them in.

### 30. The Orchestrator never does an agent's work
- The Orchestrator reads state, delegates to agents, collects results, and
  logs. Nothing else.
- Fetching, scoring, gap analysis, and verification logic must never appear
  in the Orchestrator. If it does, move it into the appropriate agent.

### 31. Print and log the plan before executing it
- Before any agent runs, the Orchestrator prints and logs its full plan:
  which agents will run, which are skipped, and the specific state value
  (e.g. `unscored_count=12`) that drove each decision.
- The plan must be observable without running the agents.

### 32. One failing agent does not stop the cycle
- If a sub-agent raises an unhandled exception, the Orchestrator catches it,
  logs the failure against that agent, and continues executing the remaining
  agents in the plan.
- The cycle is marked `partial` in the summary row, not `failed`.

### 33. Every cycle writes exactly one summary row
- At the end of every cycle, a single summary row is written to `cycle_log`
  containing: which agents ran, which were skipped and why, duration per
  agent, and the overall cycle outcome (`ok` or `partial`).
- This row is written even if every agent failed.

---

## Verification

### 34. The Verifier judges; the Orchestrator acts
- The Verifier returns a verdict and a reason. It never repairs, rewrites,
  or adjusts any data.
- On a failed verdict, the Orchestrator decides what happens next: retry,
  mark degraded, or skip publishing. The Verifier has no opinion on that.

### 35. Verification checks plausibility, not correctness
- There is no ground truth for a fit score. Verification never asserts
  that any single value is right.
- Checks assert properties of the output distribution and shape:
  score spread, count of results, presence of required fields, absence of
  impossible values (e.g. scores outside 0–100), and statistical sanity.

### 36. At most one retry; then mark degraded and stop
- A failed verification may trigger at most one retry of the failing agent
  with adjusted context (e.g. a tighter batch or cleared cache).
- After one retry, if the verdict is still failing, the cycle is marked
  `degraded` and stops. No further retries.
- Never retry in an unbounded loop.

### 37. Every verdict is logged with the specific failure
- Every verdict — pass or fail — is logged to `cycle_log`.
- A failure log must include: which check failed, and the observed value
  that triggered it (e.g. `spread=3, threshold=10`).
- Logging `status="failed"` with no detail is not acceptable.

### 38. Stale verified data always beats fresh unverified data
- Only cycles with a passing verdict may expose data to the dashboard.
- A failed or degraded cycle must never overwrite the last known-good
  dataset.
- The dashboard reads from a verified snapshot, not from the live
  listings table directly.

### 39. Verification thresholds live in config, not in code
- Every threshold used by the Verifier lives in `config.yaml`.
- Every threshold entry must have a comment explaining what failure
  condition it is designed to catch.
- Hardcoding a threshold value in a Python file is a rule violation.

---

## Natural Language Queries

### 40. NEVER generate SQL from a model
- No text-to-SQL, ever, in any form.
- The model selects from a fixed registry of parameterised query functions that I wrote. It never composes a query.

### 41. Query tools are read-only, parameterised, and clamped
- Every query tool is read-only, parameterised, and takes typed parameters that are validated and clamped to a safe range before execution.
- A model-supplied parameter is untrusted input.

### 42. The model appears exactly twice per question
- Once to ROUTE (pick a tool and its parameters) and once to PHRASE (turn returned rows into prose).
- It never touches the database in either call.

### 43. The phrasing call may use ONLY the numbers present in the rows
- It must not estimate, extrapolate, add outside context, or infer a value that is not in the data.
- If the rows are empty it must say so plainly.

### 44. Every answer displays the underlying rows alongside it
- No prose answer appears without the data that produced it.

### 45. If no tool matches the question, say so and list what CAN be asked
- Never guess at the closest tool and never answer from general knowledge.

### 46. Query tools read from the last passing cycle only
- Per rule 38.

---

## Deployment

### 47. Ephemeral filesystem; only the hosted DB is durable
- Never rely on the local filesystem for anything that must survive a
  restart. Hosting filesystems are ephemeral.
- All persistent state lives in the hosted Postgres database.
- No scheduled job result, cache, snapshot, or log is stored as a file on
  disk.

### 48. Secrets are env vars, loaded in one place
- Every secret (database URL, API keys, tokens) comes from an environment
  variable.
- All env-var reads for secrets happen in exactly one place (the config
  loader). No other module reads `os.environ` for secrets.
- A secret is never committed, printed, logged, or shown in an error
  message, stack trace, or dashboard panel. Redact before logging.

### 49. Scheduler and dashboard are separate processes
- The scheduled job and the dashboard are separate processes that share
  only the database.
- The dashboard never runs a fetch/score/analysis cycle.
- The scheduler never serves a page or renders the UI.
- They can be deployed to different runtimes; neither references the
  other's code path.

### 50. Graceful degradation; no tracebacks to strangers
- The deployed app must start and render even when the database is empty,
  unreachable, or mid-migration.
- It shows a clear, user-facing status message instead of a stack trace.
- A stranger visiting the page must never see a Python traceback or
  internal error text.

### 51. Scheduled job is idempotent, time-bounded, free-tier safe
- Running the scheduled job twice in a row produces the same end state
  (no duplicate writes, no double-counting).
- Every cycle has a hard timeout wall-clock cap so it never exceeds free
  tier CPU-minutes or LLM budget.
- Concurrent invocations must not corrupt the database or double-score
  listings.

---

## Style

- Small, testable functions. One function does one thing.
- Plain readable Python over clever Python. Optimize for the next reader.
- When asked to build one module, build that module only. Do not scaffold the
  whole application unless explicitly asked.

---

## Before Writing Any Code

1. Confirm which module or component is being built.
2. Check that it fits the architecture above.
3. If a dependency or deviation is needed, say so before writing a single line.
