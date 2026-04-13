"""Tests for signed URL middleware."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from zestimate_agent.api.signed_url import sign_request


def _make_app(*, secret: str | None = None):
    """Build a minimal test app with the signed URL dependency."""
    # Minimal agent stub
    from tests.unit.test_never_raises import _BlockedFetcher, _GoodNormalizer, _GoodResolver
    from zestimate_agent.agent import ZestimateAgent
    from zestimate_agent.api.app import create_app
    from zestimate_agent.config import Settings

    settings = Settings(
        cache_backend="none",
        crosscheck_provider="none",
        unblocker_api_key="fake",
        playwright_enabled=False,
        signed_url_secret=secret,
    )
    agent = ZestimateAgent(
        settings,
        normalizer=_GoodNormalizer(),
        resolver=_GoodResolver(),
        fetcher=_BlockedFetcher(),
    )
    return create_app(agent=agent, settings=settings)


class TestSignedUrl:
    def test_no_secret_passes_without_sig(self) -> None:
        """When SIGNED_URL_SECRET is unset, requests pass without sig params."""
        app = _make_app(secret=None)
        with TestClient(app) as client:
            resp = client.post("/lookup", json={"address": "123 Test St"})
            # Should reach the handler (not 403)
            assert resp.status_code != 403

    def test_with_secret_rejects_missing_sig(self) -> None:
        app = _make_app(secret="test-secret-key")
        with TestClient(app) as client:
            resp = client.post("/lookup", json={"address": "123 Test St"})
            assert resp.status_code == 403
            assert "missing sig" in resp.json()["detail"]

    def test_with_secret_rejects_expired_sig(self) -> None:
        app = _make_app(secret="test-secret-key")
        with TestClient(app) as client:
            # Expired 2 minutes ago (past the 60s tolerance)
            expired = int(time.time()) - 120
            params = sign_request("test-secret-key", "POST", "/lookup", expired)
            resp = client.post(
                f"/lookup?sig={params['sig']}&exp={params['exp']}",
                json={"address": "123 Test St"},
            )
            assert resp.status_code == 403
            assert "expired" in resp.json()["detail"]

    def test_with_secret_rejects_bad_sig(self) -> None:
        app = _make_app(secret="test-secret-key")
        with TestClient(app) as client:
            expiry = int(time.time()) + 300
            resp = client.post(
                f"/lookup?sig=deadbeef&exp={expiry}",
                json={"address": "123 Test St"},
            )
            assert resp.status_code == 403
            assert "invalid signature" in resp.json()["detail"]

    def test_valid_signature_passes(self) -> None:
        app = _make_app(secret="test-secret-key")
        with TestClient(app) as client:
            expiry = int(time.time()) + 300
            params = sign_request("test-secret-key", "POST", "/lookup", expiry)
            resp = client.post(
                f"/lookup?sig={params['sig']}&exp={params['exp']}",
                json={"address": "123 Test St"},
            )
            # Should reach the handler (502 = handler ran, fetch was blocked)
            assert resp.status_code != 403

    def test_sign_request_utility(self) -> None:
        params = sign_request("secret", "POST", "/lookup", 1700000000)
        assert "sig" in params
        assert "exp" in params
        assert params["exp"] == "1700000000"
        assert len(params["sig"]) == 64  # SHA256 hex
