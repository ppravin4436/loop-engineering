"""
Source protocol and registration machinery.

Every job board lives behind a class that satisfies the Source protocol.
The Fetcher iterates SOURCES and never touches board-specific logic directly.

Adding a new source:
    1. Create a module in edgedash/sources/
    2. Decorate your class with @register
    3. Import the module somewhere (e.g. the Fetcher) so the decorator runs

That's it — no other file needs to change.
"""

from __future__ import annotations

from typing import Protocol, Type

from edgedash.config import Config


# ---------------------------------------------------------------------------
# Normalised row contract (steering rule 10)
# Every source must return dicts with exactly these keys.
# ---------------------------------------------------------------------------
REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "source",
        "external_id",
        "title",
        "company",
        "location",
        "url",
        "description",
        "posted_at",
        "raw",
    }
)


class Source(Protocol):
    """
    Structural protocol every source class must satisfy.

    fetch() returns a list of normalised dicts. Each dict must contain
    exactly the keys listed in REQUIRED_KEYS. Missing values are None —
    never an empty string or "N/A".
    """

    name: str

    def fetch(self, config: Config) -> list[dict]:
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type] = {}


def register(cls: type) -> type:
    """
    Class decorator that registers a Source implementation.

    Usage:
        @register
        class MySource:
            name = "my_source"
            def fetch(self, config): ...
    """
    source_name: str = getattr(cls, "name", None)
    if not source_name:
        raise ValueError(
            f"Source class {cls.__name__} must define a non-empty class attribute 'name'."
        )
    SOURCES[source_name] = cls
    return cls
