"""Tests for the OAuth 2.0 authorization-code + PKCE domain (mcp/oauth.py).

Covers the pure helpers (PKCE verification, loopback redirect-URI check), the
in-memory store primitives, and the 4 route handlers via a Starlette TestClient.
"""
import base64
import hashlib
import time

import pytest


# ── PKCE verification ───────────────────────────────────────────────────


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_verify_pkce_s256_correct():
    from agent_windows.mcp.oauth import _verify_pkce
    assert _verify_pkce("verifier-123", _s256_challenge("verifier-123"), "S256") is True


def test_verify_pkce_s256_wrong_verifier():
    from agent_windows.mcp.oauth import _verify_pkce
    assert _verify_pkce("wrong", _s256_challenge("verifier-123"), "S256") is False


def test_verify_pkce_plain_matches():
    from agent_windows.mcp.oauth import _verify_pkce
    assert _verify_pkce("abc", "abc", "plain") is True
    assert _verify_pkce("abc", "xyz", "plain") is False


def test_verify_pkce_unknown_method():
    from agent_windows.mcp.oauth import _verify_pkce
    assert _verify_pkce("abc", "abc", "weird") is False


# ── loopback redirect URI check ─────────────────────────────────────────


@pytest.mark.parametrize("uri", [
    "http://localhost:8765/cb",
    "http://127.0.0.1:8765/cb",
    "https://[::1]:8765/cb",
])
def test_is_loopback_redirect_uri_accepts_loopback(uri):
    from agent_windows.mcp.oauth import _is_loopback_redirect_uri
    assert _is_loopback_redirect_uri(uri) is True


@pytest.mark.parametrize("uri", [
    "http://example.com/cb",          # public hostname
    "http://192.168.1.5:8765/cb",     # non-loopback private IP
    "ftp://localhost/cb",             # wrong scheme
    "http://localhost/cb#frag",       # fragment disallowed
    "not-a-url",
])
def test_is_loopback_redirect_uri_rejects_non_loopback(uri):
    from agent_windows.mcp.oauth import _is_loopback_redirect_uri
    assert _is_loopback_redirect_uri(uri) is False


# ── OAuthStore primitives ───────────────────────────────────────────────


def test_store_starts_empty():
    from agent_windows.mcp.oauth import OAuthStore
    s = OAuthStore()
    assert s.clients == {} and s.codes == {} and s.tokens == {}


def test_store_validate_unknown_token():
    from agent_windows.mcp.oauth import OAuthStore, validate_oauth_token
    assert validate_oauth_token(OAuthStore(), "nope") is False


def test_store_validate_active_and_expired_token():
    from agent_windows.mcp.oauth import (OAuthStore, AccessToken,
                                         validate_oauth_token)
    s = OAuthStore()
    s.tokens["active"] = AccessToken("active", "cid", expires_at=time.time() + 100)
    s.tokens["expired"] = AccessToken("expired", "cid", expires_at=time.time() - 1)
    assert validate_oauth_token(s, "active") is True
    assert validate_oauth_token(s, "expired") is False
    # expired token is lazily deleted on the failed check
    assert "expired" not in s.tokens


# ── build_oauth_routes — helpers ────────────────────────────────────────


def _oauth_app(client_id="test-client", client_secret="test-secret"):
    """Build a Starlette app mounting OAuth routes; return (app, store)."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from agent_windows.mcp.oauth import OAuthStore, build_oauth_routes
    store = OAuthStore()
    routes = build_oauth_routes(
        store, "http://localhost:8000", client_id, client_secret)
    starlette_routes = [
        Route(path, handler, methods=methods)
        for path, (handler, methods) in routes.items()
    ]
    return Starlette(routes=starlette_routes), store


def _authorize(client, **overrides):
    """Call GET /oauth/authorize with PKCE defaults; return response."""
    params = {
        "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "response_type": "code",
        "code_challenge": _s256_challenge("v"),
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    params.update(overrides)
    return client.get("/oauth/authorize", params=params, follow_redirects=False)


def _full_flow(client, store, code_verifier="v"):
    """Run authorize→return the issued code (helper for token tests)."""
    r = _authorize(client, code_challenge=_s256_challenge(code_verifier))
    assert r.status_code == 302
    return r.headers["location"].split("code=")[1].split("&")[0]


# ── build_oauth_routes — metadata + register ────────────────────────────


def test_metadata_returns_oauth_server_metadata():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = TestClient(app).get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == "http://localhost:8000"
    assert body["authorization_endpoint"] == "http://localhost:8000/oauth/authorize"
    assert body["token_endpoint"] == "http://localhost:8000/oauth/token"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["grant_types_supported"] == ["authorization_code"]


def test_register_is_disabled():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = TestClient(app).post("/oauth/register", json={"redirect_uris": []})
    assert r.status_code == 403
    assert r.json()["error"] == "registration_disabled"


# ── build_oauth_routes — authorize ──────────────────────────────────────


def test_authorize_rejects_wrong_response_type():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = _authorize(TestClient(app), response_type="token")
    assert r.status_code == 400 and r.json()["error"] == "unsupported_response_type"


def test_authorize_requires_code_challenge():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = _authorize(TestClient(app), code_challenge="")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_authorize_requires_s256():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = _authorize(TestClient(app), code_challenge_method="plain")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_authorize_rejects_non_loopback_redirect():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = _authorize(TestClient(app), redirect_uri="http://example.com/cb")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_authorize_rejects_unknown_client():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = _authorize(TestClient(app), client_id="who")
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"


def test_authorize_issues_code_and_redirects():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    r = _authorize(TestClient(app))
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://localhost:8765/cb")
    assert "code=" in r.headers["location"]
    assert "state=xyz" in r.headers["location"]
    # code was stored
    code = r.headers["location"].split("code=")[1].split("&")[0]
    assert code in store.codes


# ── build_oauth_routes — token ──────────────────────────────────────────


def test_token_rejects_wrong_grant_type():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = TestClient(app).post("/oauth/token",
                             data={"grant_type": "refresh_token"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


def test_token_rejects_unknown_code():
    from starlette.testclient import TestClient
    app, _ = _oauth_app()
    r = TestClient(app).post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "nope",
        "code_verifier": "v", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "test-secret"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_token_rejects_pkce_mismatch():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    code = _full_flow(TestClient(app), store)
    r = TestClient(app).post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "WRONG", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "test-secret"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_token_rejects_wrong_client_secret():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    code = _full_flow(TestClient(app), store)
    r = TestClient(app).post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "v", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "WRONG"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_token_full_success_and_code_single_use():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    client = TestClient(app)
    code = _full_flow(client, store)
    body = {
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "v", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "test-secret"}
    r = client.post("/oauth/token", data=body)
    assert r.status_code == 200
    tok = r.json()
    assert tok["token_type"] == "Bearer"
    assert tok["expires_in"] == 3600
    assert tok["access_token"] in store.tokens
    # one-time use: second exchange with same code fails
    r2 = client.post("/oauth/token", data=body)
    assert r2.status_code == 400 and r2.json()["error"] == "invalid_grant"


def test_token_accepts_json_body():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    code = _full_flow(TestClient(app), store)
    client = TestClient(app)
    r = client.post("/oauth/token", json={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "v", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "test-secret"})
    assert r.status_code == 200 and r.json()["token_type"] == "Bearer"


def test_token_rejects_expired_code():
    from starlette.testclient import TestClient
    app, store = _oauth_app()
    client = TestClient(app)
    code = _full_flow(client, store)
    # Force-expire the stored code.
    store.codes[code].expires_at = time.time() - 1
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "v", "client_id": "test-client",
        "redirect_uri": "http://localhost:8765/cb",
        "client_secret": "test-secret"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # expired code is cleaned up on the failed check
    assert code not in store.codes
