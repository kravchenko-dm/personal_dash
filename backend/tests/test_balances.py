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
    resp = await client.post("/api/balances", json={"name": "x", "provider": "openrouter", "config": {}}, headers=auth)
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
        {"url": "https://x/balance", "headers": "X-Api-Key: k", "path": "$.data.balance", "currency_path": "$.data.currency"}
    )
    assert result.value == 42.5 and result.currency == "EUR"


async def test_http_json_bad_path(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"a": 1}))
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(ValueError):
        await HttpJsonProvider().fetch({"url": "https://x", "path": "$.b"})
