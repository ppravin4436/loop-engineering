"""
The ONLY module in EdgeDash that performs outbound HTTP requests.

All sources must call get_json() from here. No bare requests.get() anywhere
else in the codebase (steering rule 11).

Behaviour:
- 10-second timeout (overridable via the timeout parameter)
- 2 retry attempts with exponential backoff (1 s, then 2 s)
- Honest User-Agent header
- Raises SourceError on any unrecoverable failure
"""

from __future__ import annotations

import time
from typing import Any

import requests


_USER_AGENT = (
    "EdgeDash/0.1 (autonomous career intelligence agent; "
    "https://github.com/edgedash; respectful bot)"
)

_DEFAULT_TIMEOUT: int = 10
_MAX_RETRIES: int = 2
_BACKOFF_BASE: float = 1.0  # seconds; doubled on each retry


class SourceError(Exception):
    """Raised when a source HTTP request fails after all retries."""


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Any:
    """
    Fetch a URL and return parsed JSON.

    Retries up to _MAX_RETRIES times with exponential backoff.
    Raises SourceError if all attempts fail.
    """
    merged_headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            sleep_for = _BACKOFF_BASE * (2 ** (attempt - 1))
            time.sleep(sleep_for)

        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout as exc:
            last_exc = exc
        except requests.exceptions.HTTPError as exc:
            # 4xx errors are not retriable (bad request / auth); surface immediately
            status = exc.response.status_code if exc.response is not None else "?"
            raise SourceError(
                f"HTTP {status} from {url}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            last_exc = exc

    raise SourceError(
        f"Request to {url} failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc
