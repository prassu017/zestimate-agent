"""Signed URL middleware — HMAC-based request authentication for CDN caching.

When a CDN sits in front of ``/lookup``, you want the CDN to cache responses
but prevent unauthorized callers from crafting valid cache keys. Signed URLs
solve this: the client includes a signature and expiry in query params, and
this middleware validates them before the request reaches the handler.

Signing protocol
----------------

1. Client builds the canonical string: ``{method}:{path}:{expiry}``
   e.g. ``POST:/lookup:1712937600``

2. Client computes ``HMAC-SHA256(secret, canonical)`` and hex-encodes it.

3. Client appends ``?sig={hex}&exp={unix_timestamp}`` to the URL.

4. Middleware rejects the request if:
   - ``exp`` is in the past (clock tolerance: 60 s)
   - ``sig`` doesn't match the re-computed HMAC
   - ``SIGNED_URL_SECRET`` is unset (middleware becomes a no-op)

Configuration
-------------

Set ``SIGNED_URL_SECRET`` in the environment. When unset, the middleware
passes all requests through — convenient for local dev.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status


def _compute_signature(secret: str, method: str, path: str, expiry: str) -> str:
    canonical = f"{method}:{path}:{expiry}"
    return hmac.new(
        secret.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()


def make_signed_url_dependency(secret: str | None, *, clock_tolerance: int = 60):
    """Return a FastAPI dependency that validates signed URL params.

    When ``secret`` is None, returns a no-op dependency (open access).
    """

    async def _verify_signature(request: Request) -> None:
        if secret is None:
            return  # no-op when secret is unset

        sig = request.query_params.get("sig")
        exp = request.query_params.get("exp")

        if not sig or not exp:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="missing sig or exp query parameters",
            )

        # Check expiry
        try:
            expiry_ts = int(exp)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid exp parameter",
            ) from e

        now = int(time.time())
        if expiry_ts + clock_tolerance < now:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="signed URL expired",
            )

        # Verify HMAC
        expected = _compute_signature(
            secret, request.method, request.url.path, exp
        )
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid signature",
            )

    return _verify_signature


def sign_request(secret: str, method: str, path: str, expiry: int) -> dict[str, str]:
    """Generate sig and exp query params for a request.

    Utility for clients and tests to create valid signed URLs.

    Returns dict with 'sig' and 'exp' keys.
    """
    sig = _compute_signature(secret, method, path, str(expiry))
    return {"sig": sig, "exp": str(expiry)}
