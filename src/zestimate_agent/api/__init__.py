"""HTTP API for the Zestimate agent.

A thin FastAPI layer on top of `ZestimateAgent` that exposes:

    POST /lookup          — run the pipeline for one address
    GET  /healthz         — liveness (always 200 if process alive)
    GET  /readyz          — readiness (agent + cache initialized)
    GET  /version         — package version
    GET  /metrics         — Prometheus exposition format

The agent is constructed once at startup via FastAPI's lifespan context so
httpx clients, cache backend, and Rentcast counter are shared across requests.

Usage
-----

    from zestimate_agent.api import create_app
    app = create_app()

Or via the CLI:

    zestimate serve --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from zestimate_agent.api.app import create_app

__all__ = ["create_app"]
