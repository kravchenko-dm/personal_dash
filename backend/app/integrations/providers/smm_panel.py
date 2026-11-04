"""SMM panels (smmprime.com and hundreds of look-alikes built on the same "API v2" convention).

Every call is a POST to one endpoint with form fields `key` and `action`; `action=balance`
answers {"balance": "100.84", "currency": "USD"} or {"error": "..."}."""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class SmmPanelProvider(BalanceProvider):
    key = "smm_panel"
    title = "SMM-панель (API v2)"
    description = "Баланс SMM-панели (smmprime.com и подобные): POST key + action=balance."
    fields = [
        ProviderField("api_url", "API URL", kind="url", placeholder="https://smmprime.com/api/v2", help="Адрес из раздела API панели"),
        ProviderField("api_key", "API key", kind="secret"),
    ]

    @staticmethod
    def _parse(payload: Any) -> BalanceResult:
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ValueError("Панель вернула не JSON-объект")
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        raw = payload.get("balance", payload.get("Balance"))
        if raw is None:
            raise ValueError("В ответе нет поля balance")
        return BalanceResult(value=round(float(str(raw).replace(",", ".")), 4), currency=str(payload.get("currency") or ""))

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        url = str(config["api_url"]).strip()
        params = {"key": str(config["api_key"]).strip(), "action": "balance"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(url, data=params, headers={"Accept": "application/json"})
            try:
                return self._parse(resp.json())
            except ValueError as exc:
                if resp.status_code < 400 and "balance" in resp.text:
                    raise
                post_error = exc
            # a few panels only take GET with query parameters
            resp = await client.get(url, params=params, headers={"Accept": "application/json"})
            try:
                return self._parse(resp.json())
            except ValueError:
                raise ValueError(f"Панель не ответила балансом (HTTP {resp.status_code}): {post_error}") from None
