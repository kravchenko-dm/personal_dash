from typing import Any

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class ManualProvider(BalanceProvider):
    key = "manual"
    title = "Вручную"
    description = "Баланс, который вы обновляете сами (наличные, карта без API и т.п.)."
    fields = [ProviderField("value", "Текущее значение", kind="number")]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        return BalanceResult(value=float(config.get("value") or 0))
