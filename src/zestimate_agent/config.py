"""Runtime configuration loaded from environment variables.

All configuration is centralized here via pydantic-settings. Any module that
needs configuration should import `get_settings()` rather than reading env vars
directly. This makes the codebase testable (override via env in tests) and
keeps secrets out of code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

FetcherName = Literal["unblocker", "playwright"]
UnblockerProvider = Literal["zenrows", "scraperapi", "brightdata"]
CrosscheckProvider = Literal["none", "rentcast", "attom"]
CacheBackend = Literal["sqlite", "memory", "none"]
LogFormat = Literal["pretty", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Top-level settings object. Instantiated once via `get_settings()`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Let aliased fields (e.g. `api_key` ↔ `ZESTIMATE_API_KEY`) also be
        # constructed by their field name, so test code can do
        # `Settings(api_key="x")` directly without knowing the alias.
        populate_by_name=True,
    )

    # ─── Fetcher ────────────────────────────────────────────────
    fetcher_primary: FetcherName = "unblocker"
    unblocker_provider: UnblockerProvider = "zenrows"
    unblocker_api_key: SecretStr | None = None

    playwright_enabled: bool = True
    playwright_headless: bool = True
    playwright_proxy_url: str | None = None

    # ─── Address normalization ──────────────────────────────────
    google_geocoding_api_key: SecretStr | None = None

    # ─── Cross-check ────────────────────────────────────────────
    crosscheck_provider: CrosscheckProvider = "none"
    crosscheck_api_key: SecretStr | None = None
    crosscheck_tolerance_pct: float = 10.0
    crosscheck_enabled: bool = True

    # Rentcast free tier is 50 requests/month. We cap ourselves at 40 to keep
    # budget headroom for ad-hoc debugging and eval runs. This is a HARD cap —
    # enforced before the HTTP call leaves the process.
    rentcast_monthly_cap: int = 40
    rentcast_usage_path: Path = Path(".cache/rentcast_usage.json")
    # Escape hatch: set to true to intentionally exceed the cap. Also
    # controllable per-call via ZestimateAgent.aget(force_crosscheck=True).
    rentcast_allow_overage: bool = False
    rentcast_base_url: str = "https://api.rentcast.io/v1"

    # ─── Cache ──────────────────────────────────────────────────
    cache_backend: CacheBackend = "sqlite"
    cache_path: Path = Path(".cache/zestimate.db")
    cache_ttl_seconds: int = 21600  # 6 hours

    # ─── HTTP ───────────────────────────────────────────────────
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 1.5

    # ─── Observability ──────────────────────────────────────────
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "pretty"

    # ─── API ────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # Optional shared-secret API key. When set, every /lookup request must
    # include `X-API-Key: <value>` or receive a 401. When unset (the default),
    # the API is open — convenient for local dev but **not** for production.
    # The env var is `ZESTIMATE_API_KEY` (explicit alias to avoid clashing
    # with the generic `API_KEY` name used by other processes).
    api_key: SecretStr | None = Field(default=None, validation_alias="ZESTIMATE_API_KEY")
    # CORS allowed origins. Comma-separated via `ZESTIMATE_CORS_ORIGINS`.
    cors_origins: str = Field(default="", validation_alias="ZESTIMATE_CORS_ORIGINS")

    # ─── Helpers ────────────────────────────────────────────────
    @property
    def unblocker_key(self) -> str | None:
        return self.unblocker_api_key.get_secret_value() if self.unblocker_api_key else None

    @property
    def crosscheck_key(self) -> str | None:
        return self.crosscheck_api_key.get_secret_value() if self.crosscheck_api_key else None

    @property
    def google_key(self) -> str | None:
        return (
            self.google_geocoding_api_key.get_secret_value()
            if self.google_geocoding_api_key
            else None
        )

    @property
    def api_key_value(self) -> str | None:
        return self.api_key.get_secret_value() if self.api_key else None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton (cached)."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Use in tests when overriding env."""
    get_settings.cache_clear()
