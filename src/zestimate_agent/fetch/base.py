"""Fetcher protocol — pluggable HTML retrieval for Zillow.

Every concrete fetcher (unblocker, playwright, ...) conforms to this
interface so the orchestrator can swap them at runtime and tests can
substitute a fake.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zestimate_agent.models import FetchResult


@runtime_checkable
class Fetcher(Protocol):
    """Retrieve the fully-rendered HTML for a given Zillow URL."""

    name: str

    async def fetch(self, url: str) -> FetchResult: ...

    async def aclose(self) -> None: ...
