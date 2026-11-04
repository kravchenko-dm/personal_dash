"""CLO (clo.ru) cloud balance.

Public API v2: GET https://api.clo.ru/v2/balance with a JWT token (console.clo.ru → API) as
Authorization: Bearer. Response: {"result": {"balance": 1234.5, "cost": {"hour": .., "month": ..},
"cost_active": {...}, "credit_limit": 0, "days_left_until_zero": 17}}"""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class CloProvider(BalanceProvider):
    key = "clo"
    title = "CLO (clo.ru)"
    description = "Баланс облака CLO в рублях, плюс расход в месяц и дни до нуля. Токен: console.clo.ru → API."
    fields = [ProviderField("api_key", "API токен (JWT)", kind="secret")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.clo.ru/v2/balance",
                headers={"Authorization": f"Bearer {str(config['api_key']).strip()}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("result") if isinstance(payload, dict) and "result" in payload else payload
        if not isinstance(data, dict) or "balance" not in data:
            raise ValueError("Неожиданный ответ CLO: нет поля balance")
        cost = data.get("cost") or {}
        cost_active = data.get("cost_active") or {}
        return BalanceResult(
            value=round(float(data["balance"]), 2),
            currency="RUB",
            extra={
                "cost_month": cost.get("month"),
                "cost_active_month": cost_active.get("month"),
                "credit_limit": data.get("credit_limit"),
                "days_left_until_zero": data.get("days_left_until_zero"),
            },
        )
