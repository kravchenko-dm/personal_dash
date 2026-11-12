"""HikerAPI (hikerapi.com) account balance.

GET https://api.hikerapi.com/sys/balance with the key in the `x-access-key` header; the call is free.
{"requests": 129627, "rate": 11, "currency": "USD", "amount": 2592.5777}
`requests` (calls left) is real time; `amount` is settled once per hour and drops to 0.0 when
`requests` reaches 0. So the card can show either: money for accounting, requests for alerts."""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class HikerApiProvider(BalanceProvider):
    key = "hikerapi"
    title = "HikerAPI"
    description = (
        "Баланс HikerAPI: деньги на счёте (USD, пересчёт раз в час) или остаток запросов "
        "(в реальном времени). Ключ — в личном кабинете hikerapi.com."
    )
    fields = [
        ProviderField("api_key", "Access key", kind="secret"),
        ProviderField(
            "show",
            "Что показывать",
            kind="select",
            required=False,
            options=[["usd", "Деньги, USD"], ["requests", "Остаток запросов"]],
            help="Порог уведомления сравнивается с тем, что выбрано здесь.",
        ),
    ]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.hikerapi.com/sys/balance",
                headers={"x-access-key": str(config["api_key"]).strip(), "Accept": "application/json"},
            )
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise ValueError("HikerAPI вернул не JSON") from None
        if resp.status_code >= 400 or not isinstance(data, dict):
            detail = data.get("detail") or data.get("error") if isinstance(data, dict) else None
            raise ValueError(str(detail or f"HTTP {resp.status_code}"))
        if data.get("amount") is None and data.get("requests") is None:
            raise ValueError("В ответе HikerAPI нет amount/requests")
        extra = {
            "amount": data.get("amount"),
            "currency": data.get("currency"),
            "requests": data.get("requests"),
            "rate": data.get("rate"),
        }
        if config.get("show") == "requests":
            return BalanceResult(value=float(data.get("requests") or 0), currency="запр.", extra=extra)
        return BalanceResult(
            value=round(float(data.get("amount") or 0), 2),
            currency=str(data.get("currency") or "USD"),
            extra=extra,
        )
