"""Google OAuth callback must send the user back to /calendar with the reason, never a bare 500."""

from urllib.parse import unquote

from app.services import calendar as svc


def _error(resp) -> str:
    assert resp.status_code in (302, 307), resp.text
    loc = resp.headers["location"]
    assert loc.startswith("/calendar?error=")
    return unquote(loc.split("error=", 1)[1])


async def test_callback_reports_exchange_error(client, monkeypatch):
    async def boom(db, code, state):
        raise RuntimeError("(invalid_grant) Missing code verifier.")

    monkeypatch.setattr(svc, "finish_oauth", boom)
    r = await client.get("/api/calendar/oauth/callback", params={"code": "x", "state": svc._state()})
    assert "Missing code verifier" in _error(r)


async def test_callback_without_code(client):
    r = await client.get("/api/calendar/oauth/callback", params={"error": "access_denied", "state": "s"})
    assert "access_denied" in _error(r)


async def test_callback_bad_state(client):
    r = await client.get("/api/calendar/oauth/callback", params={"code": "x", "state": "garbage"})
    assert "state" in _error(r)


async def test_callback_success_redirects(client, monkeypatch):
    async def ok(db, code, state):
        return None

    monkeypatch.setattr(svc, "finish_oauth", ok)
    r = await client.get("/api/calendar/oauth/callback", params={"code": "x", "state": svc._state()})
    assert r.status_code in (302, 307) and r.headers["location"] == "/calendar?connected=1"


def test_flow_has_no_pkce_verifier(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        svc, "get_settings", lambda: Settings(google_client_id="id", google_client_secret="s")
    )
    assert svc._flow().code_verifier is None
    assert "code_challenge" not in svc.auth_url()
