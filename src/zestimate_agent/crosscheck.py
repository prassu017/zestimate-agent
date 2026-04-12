"""Cross-check providers — second opinion on the Zillow Zestimate.

Currently supports **Rentcast** (`api.rentcast.io`). The Rentcast free tier
gives ~50 requests/month, so we enforce a self-imposed **hard cap of 40
requests/month** to leave budget headroom. The cap is persisted to disk so it
survives process restarts and is honored across CLI, API server, and eval runs.

Design notes
------------

* The usage counter lives in a small JSON file keyed by `YYYY-MM`, not in the
  cache DB — we want it to survive `zestimate --clear-cache` and be trivially
  inspectable by humans (`cat .cache/rentcast_usage.json`).
* The counter is *optimistic*: we increment **before** the HTTP call. If the
  call fails mid-flight, the count still moves. That's the right trade-off —
  under-counting would let us silently blow the budget under flaky network.
* Exceeding the cap never raises. We log a warning, record a `CrossCheck`
  with `skipped=True`, and the main pipeline proceeds. Cross-check is
  **advisory** — a missing cross-check must never block a Zillow answer.
* Overrides: `RENTCAST_ALLOW_OVERAGE=1` env var (sticky) or per-call
  `force_crosscheck=True` argument (one-shot).

Rentcast AVM response shape (observed 2026-04):

    {
      "price": 512000,
      "priceRangeLow": 487000,
      "priceRangeHigh": 539000,
      "latitude": 47.6,
      "longitude": -122.3,
      ...
    }
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from zestimate_agent.config import get_settings
from zestimate_agent.logging import get_logger
from zestimate_agent.models import CrossCheck, NormalizedAddress

log = get_logger(__name__)


# ─── Persistent usage counter ───────────────────────────────────


@dataclass(frozen=True)
class UsageSnapshot:
    """Point-in-time view of monthly Rentcast usage."""

    month: str  # "YYYY-MM"
    used: int
    cap: int

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap


class UsageCounter:
    """File-backed monthly request counter.

    Storage format (`.cache/rentcast_usage.json`):

        {"2026-04": 12, "2026-03": 40}

    Keyed by month so historical months are preserved for debugging.
    Thread-safe via a process-local lock — this is *not* a cross-process
    lock, so a single writer (the agent) is assumed. That's fine for our
    CLI + single-replica API server deployment.
    """

    def __init__(self, path: Path, cap: int) -> None:
        self._path = path
        self._cap = cap
        self._lock = threading.Lock()

    # ─── Read ───────────────────────────────────────────────────

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text() or "{}")
        except json.JSONDecodeError:
            log.warning("rentcast usage file corrupt — resetting", path=str(self._path))
            return {}
        # Coerce to int so corrupted values don't poison arithmetic.
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def snapshot(self, *, month: str | None = None) -> UsageSnapshot:
        month = month or _current_month()
        with self._lock:
            data = self._load()
            return UsageSnapshot(month=month, used=data.get(month, 0), cap=self._cap)

    # ─── Write ──────────────────────────────────────────────────

    def _save(self, data: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file to survive crashes mid-write.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)

    def increment(self, *, month: str | None = None) -> UsageSnapshot:
        """Unconditionally bump the counter for `month` and return the new snapshot.

        Use `try_consume()` for the cap-aware flow.
        """
        month = month or _current_month()
        with self._lock:
            data = self._load()
            data[month] = data.get(month, 0) + 1
            self._save(data)
            return UsageSnapshot(month=month, used=data[month], cap=self._cap)

    def try_consume(self, *, allow_overage: bool = False) -> tuple[bool, UsageSnapshot]:
        """Atomically check-and-increment.

        Returns `(allowed, snapshot)`. If the current month is already at
        or above the cap and `allow_overage` is False, returns `(False, snap)`
        *without* incrementing. Otherwise increments and returns `(True, snap)`.
        """
        month = _current_month()
        with self._lock:
            data = self._load()
            used = data.get(month, 0)
            if used >= self._cap and not allow_overage:
                return False, UsageSnapshot(month=month, used=used, cap=self._cap)
            data[month] = used + 1
            self._save(data)
            return True, UsageSnapshot(month=month, used=data[month], cap=self._cap)

    def reset(self, *, month: str | None = None) -> None:
        """Reset the counter for a given month (or all history if None)."""
        with self._lock:
            if month is None:
                self._save({})
                return
            data = self._load()
            data.pop(month, None)
            self._save(data)


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


# ─── Rentcast HTTP client ───────────────────────────────────────


class RentcastError(Exception):
    """Rentcast API returned an unexpected error. Non-fatal — cross-check will be skipped."""


class RentcastClient:
    """Thin async client for the Rentcast AVM endpoint.

    `cross_check()` is the only public method. It enforces the monthly cap
    before making the HTTP call, performs the request, and returns a
    `CrossCheck` model (never raises).
    """

    def __init__(
        self,
        *,
        api_key: str,
        counter: UsageCounter,
        tolerance_pct: float,
        base_url: str = "https://api.rentcast.io/v1",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._counter = counter
        self._tolerance_pct = tolerance_pct
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    # ─── Public API ─────────────────────────────────────────────

    async def cross_check(
        self,
        *,
        address: NormalizedAddress,
        zillow_value: int,
        force: bool = False,
    ) -> CrossCheck:
        """Compare Zillow's Zestimate against Rentcast's AVM.

        `force=True` bypasses the monthly cap (use sparingly — eval harness only).
        """
        now = datetime.now(UTC)

        # ─── Cap check ──────────────────────────────────────────
        allow_overage = force or get_settings().rentcast_allow_overage
        allowed, snap = self._counter.try_consume(allow_overage=allow_overage)
        if not allowed:
            log.warning(
                "rentcast cap reached — skipping cross-check",
                month=snap.month,
                used=snap.used,
                cap=snap.cap,
            )
            return CrossCheck(
                provider="rentcast",
                skipped=True,
                skipped_reason=(
                    f"monthly cap of {snap.cap} reached for {snap.month} "
                    f"(used={snap.used}). Override with --force-crosscheck or "
                    "RENTCAST_ALLOW_OVERAGE=1."
                ),
                fetched_at=now,
            )

        log.info(
            "rentcast cross-check",
            month=snap.month,
            used=snap.used,
            cap=snap.cap,
            zillow_value=zillow_value,
        )

        # ─── HTTP call ──────────────────────────────────────────
        try:
            payload = await self._call_avm(address)
        except RentcastError as e:
            return CrossCheck(
                provider="rentcast",
                skipped=True,
                skipped_reason=f"rentcast error: {e}",
                fetched_at=now,
            )

        price = _coerce_int(payload.get("price"))
        if price is None:
            return CrossCheck(
                provider="rentcast",
                skipped=True,
                skipped_reason="rentcast response had no 'price' field",
                fetched_at=now,
            )

        delta_pct = ((price - zillow_value) / zillow_value) * 100.0
        within = abs(delta_pct) <= self._tolerance_pct

        return CrossCheck(
            provider="rentcast",
            estimate=price,
            range_low=_coerce_int(payload.get("priceRangeLow")),
            range_high=_coerce_int(payload.get("priceRangeHigh")),
            delta_pct=round(delta_pct, 2),
            within_tolerance=within,
            fetched_at=now,
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ─── Internal ───────────────────────────────────────────────

    async def _call_avm(self, address: NormalizedAddress) -> dict[str, Any]:
        url = f"{self._base_url}/avm/value"
        params = {"address": address.canonical}
        headers = {"X-Api-Key": self._api_key, "Accept": "application/json"}

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            r = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise RentcastError(f"transport: {e}") from e
        finally:
            if self._owns_client and self._client is None:
                await client.aclose()

        if r.status_code == 401:
            raise RentcastError("unauthorized (check RENTCAST_API_KEY)")
        if r.status_code == 429:
            raise RentcastError("rentcast rate-limited (429)")
        if r.status_code == 404:
            raise RentcastError("rentcast: no AVM for this address")
        if r.status_code >= 400:
            raise RentcastError(f"HTTP {r.status_code}: {r.text[:200]}")

        try:
            data = r.json()
        except ValueError as e:
            raise RentcastError(f"invalid JSON: {e}") from e

        # Rentcast sometimes returns a list (batch shape) even for single lookups.
        if isinstance(data, list):
            if not data:
                raise RentcastError("empty response list")
            data = data[0]
        if not isinstance(data, dict):
            raise RentcastError(f"unexpected response shape: {type(data).__name__}")
        return data


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return round(float(v))
    except (TypeError, ValueError):
        return None


# ─── Factory ────────────────────────────────────────────────────


def build_rentcast_client() -> RentcastClient | None:
    """Return a configured RentcastClient, or None if cross-check is disabled."""
    settings = get_settings()
    if not settings.crosscheck_enabled:
        return None
    if settings.crosscheck_provider != "rentcast":
        return None
    key = settings.crosscheck_key
    if not key:
        log.warning("crosscheck_provider=rentcast but no key — cross-check disabled")
        return None
    counter = UsageCounter(settings.rentcast_usage_path, settings.rentcast_monthly_cap)
    return RentcastClient(
        api_key=key,
        counter=counter,
        tolerance_pct=settings.crosscheck_tolerance_pct,
        base_url=settings.rentcast_base_url,
        timeout=settings.http_timeout_seconds,
    )


def get_usage_counter() -> UsageCounter:
    """Return the Rentcast UsageCounter based on current settings."""
    settings = get_settings()
    return UsageCounter(settings.rentcast_usage_path, settings.rentcast_monthly_cap)
