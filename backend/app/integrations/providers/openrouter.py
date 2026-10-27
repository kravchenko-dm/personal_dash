from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class OpenRouterProvider(BalanceProvider):
    key = "openrouter"
    title = "OpenRouter"
    description = "Остаток кредитов OpenRouter (total_credits − total_usage), USD."
    fields = [ProviderField("api_key", "API key", kind="secret", placeholder="sk-or-v1-...")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {config['api_key']}"},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        total = float(data.get("total_credits", 0))
        used = float(data.get("total_usage", 0))
        return BalanceResult(
            value=round(total - used, 4),
            currency="USD",
            extra={"total_credits": total, "total_usage": used},
        )
