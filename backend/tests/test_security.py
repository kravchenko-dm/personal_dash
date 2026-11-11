"""Login protection: attempt log, per-IP lockout, alerts, new-address alert, log out everywhere."""

import logging
import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app import auth as auth_mod
from app.main import _RedactTokens, app
from app.services import push, security, telegram_bot


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []

    async def notify_all(db, title, body, url="/", tag=None):
        sent.append(body)
        return 1

    async def send_to_owner(text, reply_markup=None):
        pass

    monkeypatch.setattr(push, "notify_all", notify_all)
    monkeypatch.setattr(telegram_bot, "send_to_owner", send_to_owner)
    yield sent
    auth_mod.set_token_epoch(0)


def client_from(ip: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 5555)), base_url="http://test")


async def test_lockout_after_repeated_wrong_passwords(alerts):
    async with client_from("203.0.113.7") as c:
        for _ in range(security.MAX_FAILS):
            r = await c.post("/api/auth/login", json={"password": "nope"})
            assert r.status_code == 401 and r.json()["detail"] == "Неверный пароль"
        assert len(alerts) == 1 and "203.0.113.7" in alerts[0]
        # locked: even the right password is refused now
        r = await c.post("/api/auth/login", json={"password": "secret"})
        assert r.status_code == 429 and "Попробуйте через" in r.json()["detail"] and r.headers["Retry-After"]
    # another address is not affected
    async with client_from("198.51.100.1") as c:
        r = await c.post("/api/auth/login", json={"password": "secret"})
        assert r.status_code == 200
        info = (
            await c.get("/api/auth/events", headers={"Authorization": f"Bearer {r.json()['token']}"})
        ).json()
    assert info["failed_24h"] == security.MAX_FAILS and info["locked_ips"] == ["203.0.113.7"]
    assert [e["result"] for e in info["events"]][:2] == ["ok", "blocked"]


async def test_login_from_new_address_alerts(alerts):
    async with client_from("192.0.2.10") as c:
        assert (await c.post("/api/auth/login", json={"password": "secret"})).status_code == 200
        assert (await c.post("/api/auth/login", json={"password": "secret"})).status_code == 200
    assert alerts == []  # the very first login and repeat logins are quiet
    async with client_from("192.0.2.99") as c:
        assert (await c.post("/api/auth/login", json={"password": "secret"})).status_code == 200
    assert len(alerts) == 1 and "192.0.2.99" in alerts[0]


async def test_logout_everywhere_voids_old_tokens(client, alerts):
    now = int(time.time())
    old = jwt.encode(
        {"sub": "owner", "iat": now - 60, "exp": now + 3600}, auth_mod.get_settings().app_secret, "HS256"
    )
    hdr = {"Authorization": f"Bearer {old}"}
    assert (await client.get("/api/tasks", headers=hdr)).status_code == 200
    r = await client.post("/api/auth/logout-all", headers=hdr)
    assert r.status_code == 200
    assert (await client.get("/api/tasks", headers=hdr)).status_code == 401
    assert (
        await client.get("/api/tasks", headers={"Authorization": f"Bearer {r.json()['token']}"})
    ).status_code == 200


def test_access_log_hides_tokens():
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:1", "POST", "/api/apple/reminders/sync?a=1&token=eyJhbGci.secret&b=2", "1.1", 200),
        None,
    )
    _RedactTokens().filter(rec)
    assert "secret" not in rec.getMessage() and "token=***&b=2" in rec.getMessage()
