import httpx
import pytest

from app.integrations.providers.http_json import HttpJsonProvider
from app.integrations.providers.openrouter import OpenRouterProvider


async def test_providers_listed(client, auth):
    keys = {p["key"] for p in (await client.get("/api/balances/providers", headers=auth)).json()}
    assert {"manual", "http_json", "openrouter", "deepseek"} <= keys


async def test_manual_account_and_history(client, auth):
    body = {"name": "Наличные", "provider": "manual", "config": {"value": "1500"}, "currency": "₽"}
    created = await client.post("/api/balances", json=body, headers=auth)
    assert created.status_code == 201, created.text
    acc = created.json()
    assert acc["last_value"] == 1500 and acc["last_error"] is None

    hist = (await client.get(f"/api/balances/{acc['id']}/history", headers=auth)).json()
    assert len(hist) == 1 and hist[0]["value"] == 1500

    body["config"] = {"value": "900"}
    updated = (await client.put(f"/api/balances/{acc['id']}", json=body, headers=auth)).json()
    assert updated["last_value"] == 900


async def test_missing_required_field_rejected(client, auth):
    resp = await client.post(
        "/api/balances", json={"name": "x", "provider": "openrouter", "config": {}}, headers=auth
    )
    assert resp.status_code == 400


async def test_secret_is_masked(client, auth, monkeypatch):
    async def fake_fetch(self, config):
        from app.integrations.base import BalanceResult

        return BalanceResult(value=12.5, currency="USD")

    monkeypatch.setattr(OpenRouterProvider, "fetch", fake_fetch)
    body = {"name": "OR", "provider": "openrouter", "config": {"api_key": "sk-or-secret"}}
    acc = (await client.post("/api/balances", json=body, headers=auth)).json()
    assert acc["config"]["api_key"] == "••••••"
    assert acc["last_value"] == 12.5 and acc["currency"] == "USD"

    # sending the mask back keeps the stored key
    body["config"] = {"api_key": "••••••"}
    await client.put(f"/api/balances/{acc['id']}", json=body, headers=auth)
    from app.db import SessionLocal
    from app.models import BalanceAccount

    async with SessionLocal() as db:
        row = await db.get(BalanceAccount, acc["id"])
        assert row.config["api_key"] == "sk-or-secret"


async def test_http_json_provider_parses(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "k"
        return httpx.Response(200, json={"data": {"balance": "42.5", "currency": "EUR"}})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(**kw):
        return real(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    result = await HttpJsonProvider().fetch(
        {
            "url": "https://x/balance",
            "headers": "X-Api-Key: k",
            "path": "$.data.balance",
            "currency_path": "$.data.currency",
        }
    )
    assert result.value == 42.5 and result.currency == "EUR"


async def test_http_json_bad_path(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"a": 1}))
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(ValueError):
        await HttpJsonProvider().fetch({"url": "https://x", "path": "$.b"})


async def test_vdsina_provider(monkeypatch):
    from app.integrations.providers.vdsina import VdsinaProvider

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["Authorization"]))
        if request.headers["Authorization"] == "tok":
            return httpx.Response(401, json={"status": "error"})
        return httpx.Response(200, json={"status": "ok", "data": {"real": "150.50", "bonus": "10", "partner": "0"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await VdsinaProvider().fetch({"api_key": "tok", "host": "vdsina.com"})
    assert result.value == 150.5 and result.currency == "USD" and result.extra["total"] == 160.5
    assert seen == [("userapi.vdsina.com", "tok"), ("userapi.vdsina.com", "Bearer tok")]
    result = await VdsinaProvider().fetch({"api_key": "ok-token"})
    assert result.currency == "RUB" and seen[-1][0] == "userapi.vdsina.ru"
    with pytest.raises(ValueError):
        await VdsinaProvider().fetch({"api_key": "x", "host": "example.com"})


async def test_clo_provider(monkeypatch):
    from app.integrations.providers.clo import CloProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.clo.ru/v2/balance" and request.headers["Authorization"] == "Bearer jwt"
        return httpx.Response(
            200,
            json={
                "result": {
                    "balance": 1234.56,
                    "cost": {"hour": 1.2, "month": 870.0},
                    "cost_active": {"hour": 1.0, "month": 700.0},
                    "credit_limit": 0,
                    "days_left_until_zero": 42,
                }
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await CloProvider().fetch({"api_key": "jwt"})
    assert result.value == 1234.56 and result.currency == "RUB"
    assert result.extra["cost_month"] == 870.0 and result.extra["days_left_until_zero"] == 42


async def test_hosting_providers_listed(client, auth):
    r = await client.get("/api/balances/providers", headers=auth)
    keys = {p["key"] for p in r.json()}
    assert {"vdsina", "clo"} <= keys


async def test_smm_panel_provider(monkeypatch):
    from app.integrations.providers.smm_panel import SmmPanelProvider

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            body = request.content.decode()
            assert "key=secret" in body and "action=balance" in body
            return httpx.Response(200, json={"balance": "100.84", "currency": "USD"})
        return httpx.Response(405)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await SmmPanelProvider().fetch({"api_url": "https://panel.test/api/v2", "api_key": "secret"})
    assert result.value == 100.84 and result.currency == "USD" and calls == ["POST"]

    # panel that only accepts GET: POST answers with HTML, GET with JSON
    def handler2(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="<html>nope</html>")
        assert request.url.params["action"] == "balance"
        return httpx.Response(200, json={"balance": "12,5", "currency": "RUB"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler2), **kw))
    result = await SmmPanelProvider().fetch({"api_url": "https://panel.test/api/v2", "api_key": "k"})
    assert result.value == 12.5 and result.currency == "RUB"

    # wrong key: the panel's error message is surfaced
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "Invalid API key"})), **kw),
    )
    with pytest.raises(ValueError, match="Invalid API key"):
        await SmmPanelProvider().fetch({"api_url": "https://panel.test/api/v2", "api_key": "bad"})


async def test_subgram_provider(monkeypatch):
    from app.integrations.providers.subgram import SubgramProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url == "https://api.subgram.org/get-balance"
        if request.headers["Auth"] != "tok":
            return httpx.Response(401, json={"status": "error", "code": 401, "message": "Invalid token"})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "code": 200,
                "balance": 1532.4,
                "bots_info": [{"bot_id": 1, "bot_username": "my_bot", "total_followers": 120, "revenue": 532.4}],
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await SubgramProvider().fetch({"api_token": "tok"})
    assert result.value == 1532.4 and result.currency == "RUB"
    assert result.extra["bots"] == [{"bot": "my_bot", "revenue": 532.4, "followers": 120}]
    with pytest.raises(ValueError, match="Invalid token"):
        await SubgramProvider().fetch({"api_token": "bad"})


async def test_cheatbot_provider(monkeypatch):
    from app.integrations.providers.cheatbot import CheatBotProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.cheatbot.ru" and request.url.path == "/pb/profile/info"
        if request.url.params.get("access-token") != "k1":
            return httpx.Response(401, json={"name": "Unauthorized", "message": "Your request was made with invalid credentials."})
        return httpx.Response(200, json={"balance": 250.75, "email": "me@example.com", "activeTask": 2})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await CheatBotProvider().fetch({"api_key": "k1"})
    assert result.value == 250.75 and result.currency == "RUB" and result.extra["active_tasks"] == 2
    with pytest.raises(ValueError, match="invalid credentials"):
        await CheatBotProvider().fetch({"api_key": "nope"})


async def test_polza_provider(monkeypatch):
    from app.integrations.providers.polza import PolzaProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url == "https://polza.ai/api/v2/balance"
        if request.headers["Authorization"] != "Bearer pk":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "Invalid API key"}, "trace_id": "t"})
        return httpx.Response(
            200,
            json={
                "amount": "1234.56000000",
                "available": "1200.06000000",
                "reservedAmount": "12.50000000",
                "spentAmount": "845.30000000",
                "updatedAt": "2026-04-30T12:00:00.000Z",
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = await PolzaProvider().fetch({"api_key": "pk"})
    assert result.value == 1234.56 and result.currency == "RUB"
    assert result.extra["available"] == 1200.06 and result.extra["spent_total"] == 845.3
    with pytest.raises(ValueError, match="Invalid API key"):
        await PolzaProvider().fetch({"api_key": "bad"})
