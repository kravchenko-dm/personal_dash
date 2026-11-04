"""SubGram (subgram.org) account balance.

POST https://api.subgram.org/get-balance with the account *API token* (the one for statistics
and balance, not a bot's API key) in the `Auth` header. Response:
{"status": "ok", "code": 200, "balance": 1234.5,
 "bots_info": [{"bot_id": 1, "bot_username": "x_bot", "total_followers": 10, "revenue": 12.3}]}"""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class SubgramProvider(BalanceProvider):
    key = "subgram"
    title = "SubGram"
    description = "Баланс аккаунта SubGram (₽) и доход по ботам. Нужен API token аккаунта, не ключ бота."
    fields = [ProviderField("api_token", "API token", kind="secret", help="subgram.org → аккаунт → API token")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.subgram.org/get-balance",
                headers={"Auth": str(config["api_token"]).strip(), "Accept": "application/json"},
            )
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise ValueError("SubGram вернул не JSON") from None
        if not isinstance(data, dict):
            raise ValueError("SubGram вернул не JSON-объект")
        if data.get("status") == "error" or resp.status_code >= 400:
            raise ValueError(str(data.get("message") or f"HTTP {resp.status_code}"))
        if data.get("balance") is None:
            raise ValueError("В ответе SubGram нет поля balance")
        bots = data.get("bots_info") or []
        return BalanceResult(
            value=round(float(data["balance"]), 2),
            currency="RUB",
            extra={
                "bots": [
                    {"bot": b.get("bot_username"), "revenue": b.get("revenue"), "followers": b.get("total_followers")}
                    for b in bots
                    if isinstance(b, dict)
                ],
                "message": data.get("message"),
            },
        )
