"""Vercel Python function entrypoint.

Vercel's Python runtime auto-discovers any module under `/api/` that exposes
an ASGI `app` symbol and hosts it as a serverless function. This file is a
thin adapter over the real app factory.

Serverless caveats (vs. a real container deployment):
    * No persistent disk — we force `CACHE_BACKEND=memory` so diskcache
      doesn't try to open a SQLite file on a read-only filesystem.
    * No long-lived process — cold start per invocation, so the in-memory
      cache resets constantly. The synthetic/healthz/version/metrics
      endpoints still work perfectly. Live /lookup calls may exceed Vercel's
      10s hobby execution limit because real Zillow scraping takes 15–30s.
    * No Rentcast counter persistence — we also disable cross-check on
      Vercel so the ephemeral counter file never matters.

Env vars to set in the Vercel dashboard (Settings → Environment Variables):
    ZESTIMATE_API_KEY=<shared secret>         (optional, enables auth)
    UNBLOCKER_API_KEY=<scraperapi key>        (required for live lookups)
    CROSSCHECK_PROVIDER=none                  (already forced below)

For production, deploy the Dockerfile instead — see README.md §Docker.
"""

from __future__ import annotations

import os

# ─── Force serverless-safe defaults before importing the agent ──
# These MUST be set before `zestimate_agent.config` is first imported,
# because get_settings() is @lru_cache'd and locks in whatever it sees.
os.environ.setdefault("CACHE_BACKEND", "memory")
os.environ.setdefault("CROSSCHECK_PROVIDER", "none")
os.environ.setdefault("LOG_FORMAT", "json")
os.environ.setdefault("LOG_LEVEL", "INFO")

from zestimate_agent.api import create_app  # noqa: E402

app = create_app()
