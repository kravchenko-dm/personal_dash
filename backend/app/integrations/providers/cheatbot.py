"""CheatBot (cheatbot.ru) balance.

GET https://api.cheatbot.ru/pb/profile/info with the API key as the `access-token` query
parameter (OpenAPI security scheme ApiKeyAuth; the docs also call it a Bearer token, so the
same key is sent in the Authorization header too). Response:
{"balance": 123.45, "email": "me@example.com", "activeTask": 3}"""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class CheatBotProvider(BalanceProvider):
    key = "cheatbot"
    title = "CheatBot"
    description = "Баланс cheatbot.ru (₽) и число активных задач. Ключ: личный кабинет → настройки → API."
    fields = [ProviderField("api_key", "API key", kind="secret")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        key = str(config["api_key"]).strip()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.cheatbot.ru/pb/profile/info",
                params={"access-token": key},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise ValueError("CheatBot вернул не JSON") from None
        if resp.status_code >= 400:
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else None
            raise ValueError(str(msg or f"HTTP {resp.status_code}"))
        if not isinstance(data, dict) or data.get("balance") is None:
            raise ValueError("В ответе CheatBot нет поля balance")
        return BalanceResult(
            value=round(float(data["balance"]), 2),
            currency="RUB",
            extra={"email": data.get("email"), "active_tasks": data.get("activeTask", data.get("active_task"))},
        )
