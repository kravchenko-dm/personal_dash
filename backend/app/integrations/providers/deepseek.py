from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class DeepSeekProvider(BalanceProvider):
    key = "deepseek"
    title = "DeepSeek"
    description = "Баланс аккаунта DeepSeek API."
    fields = [ProviderField("api_key", "API key", kind="secret", placeholder="sk-...")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {config['api_key']}"},
            )
            resp.raise_for_status()
            data = resp.json()
        infos = data.get("balance_infos") or []
        if not infos:
            return BalanceResult(value=0.0, extra={"is_available": data.get("is_available")})
        first = infos[0]
        return BalanceResult(
            value=float(first.get("total_balance", 0)),
            currency=str(first.get("currency", "")),
            extra={"is_available": data.get("is_available"), "all": infos},
        )
