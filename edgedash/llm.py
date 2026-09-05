"""
The single door to any language model (steering rule 15).

Public surface
--------------
    complete_json(prompt, schema, *, max_retries=1) -> dict

    LLMError  -- raised on unrecoverable failure; callers must handle it.

Internal design
---------------
- Provider dispatch is a dict of callables; adding a third provider never
  touches complete_json.
- Rate limiter enforces ≥1 s between calls and ≤15 calls per minute.
- Markdown fences and surrounding prose are stripped before json.loads.
- jsonschema validates every response before it leaves this module.
- 429 / quota responses get exponential backoff (up to 3 attempts).
- No other file may import google.generativeai or any LLM SDK.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
from typing import Any, Callable

import jsonschema

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when complete_json cannot produce a valid response."""


# ---------------------------------------------------------------------------
# Rate limiter (steering rule 15): ≥1 s between calls, ≤15 per minute
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Enforces two constraints:
      - Minimum interval of `min_interval` seconds between successive calls.
      - At most `max_per_minute` calls inside any rolling 60-second window.
    Sleeps to satisfy whichever constraint is tighter; never raises.
    """

    def __init__(self, min_interval: float = 1.0, max_per_minute: int = 15) -> None:
        self._min_interval = min_interval
        self._max_per_minute = max_per_minute
        self._call_times: deque[float] = deque()
        self._last_call: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()

        # Enforce minimum interval between consecutive calls
        since_last = now - self._last_call
        if since_last < self._min_interval:
            time.sleep(self._min_interval - since_last)
            now = time.monotonic()

        # Enforce rolling per-minute cap
        window_start = now - 60.0
        while self._call_times and self._call_times[0] < window_start:
            self._call_times.popleft()

        if len(self._call_times) >= self._max_per_minute:
            # Sleep until the oldest call in the window falls outside 60 s
            sleep_for = 60.0 - (now - self._call_times[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()

        self._call_times.append(now)
        self._last_call = now


_limiter = _RateLimiter(min_interval=1.0, max_per_minute=15)


# ---------------------------------------------------------------------------
# Response cleaning and validation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_to_json(text: str) -> str:
    """
    Remove markdown code fences and any surrounding prose, leaving only the
    JSON object or array. Models add fences constantly; handle it robustly.
    """
    # Prefer content inside a code fence if one exists
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()

    # Otherwise find the first { or [ and the last matching } or ]
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]

    return text.strip()


def _parse_and_validate(raw: str, schema: dict) -> dict:
    """
    Strip fences, parse JSON, validate against schema.
    Raises ValueError on parse failure, jsonschema.ValidationError on bad shape.
    """
    cleaned = _strip_to_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}  |  raw snippet: {cleaned[:200]}") from exc

    jsonschema.validate(instance=data, schema=schema)
    return data  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Provider implementations
# A provider callable has signature:
#   (prompt: str, model: str, config: Any) -> str   (returns raw model text)
# Rule 48: secrets come from `config`, never from os.environ inside this file.
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, model: str, config: Any) -> str:
    """Call Google Gemini. Imports the SDK lazily so Ollama users need not install it.
    Rule 48: reads the API key from config.llm_api_key only.
    """
    api_key = (getattr(config, "llm_api_key", None) or "").strip()
    if not api_key:
        raise LLMError(
            "The LLM service is not reachable right now. "
            "Ask the workspace owner to finish configuring the app."
        )

    try:
        import google.generativeai as genai  # noqa: PLC0415
    except ImportError as exc:
        raise LLMError(
            "The hosted LLM library is not installed. Ask the workspace owner to "
            "run pip install -r requirements.txt."
        ) from exc

    genai.configure(api_key=api_key)
    client = genai.GenerativeModel(model)
    response = client.generate_content(prompt)
    return response.text


def _call_ollama(prompt: str, model: str, config: Any) -> str:
    """Call a local Ollama instance via its HTTP API. No API key needed."""
    import requests  # noqa: PLC0415 — already a project dependency

    url = "http://localhost:11434/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False}

    resp = requests.post(url, json=payload, timeout=60)
    if resp.status_code != 200:
        raise LLMError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("response", "")


_PROVIDERS: dict[str, Callable[[str, str, Any], str]] = {
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# Backoff helper for 429 / quota errors
# ---------------------------------------------------------------------------

_QUOTA_PHRASES = ("429", "quota", "resource_exhausted", "rate_limit", "too many")


def _is_quota_error(exc: Exception) -> bool:
    return any(phrase in str(exc).lower() for phrase in _QUOTA_PHRASES)


def _call_with_backoff(provider_fn: Callable, prompt: str, model: str, config: Any) -> str:
    """
    Call provider_fn up to 3 times. On a 429/quota error, sleep with
    exponential backoff (5 s, 15 s, 45 s) before retrying. Other
    exceptions propagate immediately.
    """
    delays = [5.0, 15.0, 45.0]
    last_exc: Exception | None = None

    for attempt, delay in enumerate(delays):
        try:
            _limiter.wait()
            return provider_fn(prompt, model, config)
        except LLMError:
            raise  # config / key errors are not retriable
        except Exception as exc:
            if _is_quota_error(exc):
                last_exc = exc
                if attempt < len(delays) - 1:
                    print(
                        f"  [llm] quota/rate-limit error (attempt {attempt + 1}/3), "
                        f"sleeping {delay:.0f}s …",
                        flush=True,
                    )
                    time.sleep(delay)
                continue
            raise LLMError(f"Provider error: {exc}") from exc

    raise LLMError(
        f"Provider returned quota/rate-limit errors on all 3 attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def complete_json(
    prompt: str,
    schema: dict,
    *,
    config: Any = None,          # edgedash.config.Config; Any to avoid circular import
    max_retries: int = 1,
) -> dict:
    """
    Send prompt to the configured LLM and return a validated dict.

    Parameters
    ----------
    prompt      : The user prompt. Should ask for JSON output.
    schema      : A JSON Schema dict used to validate the response.
    config      : An edgedash Config instance. If None, load_config() is called.
    max_retries : How many times to retry after a parse/validation failure
                  (steering rule 17 requires exactly 1 retry, the default).

    Raises
    ------
    LLMError    : When all retries are exhausted or a non-retriable error occurs.
                  Callers are responsible for handling this (steering rule 17).
    """
    if config is None:
        from edgedash.config import load_config  # lazy to avoid import cycles
        config = load_config()

    provider_name: str = config.llm_provider
    model: str = config.llm_model

    provider_fn = _PROVIDERS.get(provider_name)
    if provider_fn is None:
        known = ", ".join(f'"{p}"' for p in _PROVIDERS)
        raise LLMError(
            f"Unknown llm_provider '{provider_name}' in config.yaml. "
            f"Valid options: {known}."
        )

    system_preamble = (
        "You are a structured data extraction assistant. "
        "Reply with a single JSON object only. "
        "No markdown, no code fences, no explanation — raw JSON only."
    )
    full_prompt = f"{system_preamble}\n\n{prompt}"

    last_error: str = ""

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # Append the exact validation error so the model can self-correct
            full_prompt = (
                f"{full_prompt}\n\n"
                f"Your previous response was invalid. Error: {last_error}\n"
                "Reply with raw JSON only. No prose. No markdown fences."
            )

        raw_text = _call_with_backoff(provider_fn, full_prompt, model, config)

        try:
            return _parse_and_validate(raw_text, schema)
        except (ValueError, jsonschema.ValidationError) as exc:
            last_error = str(exc)
            if attempt < max_retries:
                continue
            # Final attempt failed — raise LLMError (callers handle per rule 17)
            raise LLMError(
                f"Response failed validation after {max_retries + 1} attempt(s). "
                f"Last error: {last_error}"
            ) from exc

    # Unreachable, but satisfies type checkers
    raise LLMError("complete_json exhausted retries without returning.")


# ---------------------------------------------------------------------------
# CLI connectivity check:  python -m edgedash.llm --check
# ---------------------------------------------------------------------------

def _cli_check() -> None:
    from edgedash.config import load_config

    cfg = load_config()
    print(f"Provider : {cfg.llm_provider}")
    print(f"Model    : {cfg.llm_model}")
    print("Sending test prompt...")

    schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    }
    prompt = 'Reply with exactly: {"status": "ok"}'

    try:
        result = complete_json(prompt, schema, config=cfg)
        print(f"Response : {result}")
        print("OK  LLM connection working.")
    except LLMError as exc:
        print(f"FAIL  LLMError: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check")
        sys.exit(1)
