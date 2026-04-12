"""Exception taxonomy.

The agent's top-level `get()` method never raises — it returns a
`ZestimateResult` with an appropriate status. Internally, however, modules
raise these typed exceptions so the orchestrator can map them to statuses
without stringly-typed error handling.
"""

from __future__ import annotations


class ZestimateError(Exception):
    """Base class for all agent errors."""


class NormalizationError(ZestimateError):
    """Raised when the input address cannot be parsed into components."""


class ResolverError(ZestimateError):
    """Raised when Zillow's resolver fails or returns nothing usable."""


class PropertyNotFoundError(ResolverError):
    """Raised when no Zillow property matches the given address."""


class AmbiguousAddressError(ResolverError):
    """Raised when multiple Zillow properties match with similar confidence."""


class FetchError(ZestimateError):
    """Base class for all fetch-related errors."""


class FetchBlockedError(FetchError):
    """Raised when the fetcher is blocked (captcha, 403, WAF)."""


class FetchTimeoutError(FetchError):
    """Raised when a fetch exceeds the configured timeout after retries."""


class ParseError(ZestimateError):
    """Raised when the page was fetched but the Zestimate could not be parsed."""


class NoZestimateError(ParseError):
    """Raised when the property page exists but has no Zestimate field.

    This is a legitimate non-error outcome (e.g. rentals, off-market) —
    the orchestrator maps it to `ZestimateStatus.NO_ZESTIMATE`.
    """


class ValidationError(ZestimateError):
    """Raised when a parsed Zestimate fails sanity validation."""
