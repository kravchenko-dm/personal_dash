"""Polza.AI (polza.ai) wallet balance.

GET https://polza.ai/api/v2/balance with the API key as a Bearer token. All amounts are strings
in rubles with eight decimal places:
{"amount": "1234.56000000", "available": "1200.06000000", "reservedAmount": "12.50000000",
 "spentAmount": "845.30000000", "updatedAt": "2026-04-30T12:00:00.000Z"}
`reservedAmount`/`spentAmount` are omitted for member keys with a personal spending limit.
Errors come as {"error": {"code": ..., "message": ...}, "trace_id": ...}."""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value), 2)


class PolzaProvider(BalanceProvider):
    key = "polza"
    title = "Polza.AI"
    description = "Баланс кошелька Polza.AI (₽), доступно к списанию и потрачено всего. Ключ: консоль → API-ключи."
    fields = [ProviderField("api_key", "API key", kind="secret")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://polza.ai/api/v2/balance",
                headers={"Authorization": f"Bearer {str(config['api_key']).strip()}", "Accept": "application/json"},
            )
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise ValueError("Polza.AI вернул не JSON") from None
        if not isinstance(data, dict):
            raise ValueError("Polza.AI вернул не JSON-объект")
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            raise ValueError(str(msg or f"HTTP {resp.status_code}"))
        if data.get("amount") is None:
            raise ValueError("В ответе Polza.AI нет поля amount")
        return BalanceResult(
            value=_num(data["amount"]) or 0.0,
            currency="RUB",
            extra={
                "available": _num(data.get("available")),
                "reserved": _num(data.get("reservedAmount")),
                "spent_total": _num(data.get("spentAmount")),
                "updated_at": data.get("updatedAt"),
            },
        )
