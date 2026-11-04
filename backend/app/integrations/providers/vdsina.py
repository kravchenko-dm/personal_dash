"""VDSina hosting balance (vdsina.ru = RUB, vdsina.com = USD).

Public API: https://userapi.vdsina.<ru|com>/v1/account.balance, the permanent token from the
control panel goes into the Authorization header. Response:
{"status": "ok", "data": {"real": "123.45", "bonus": "0", "partner": "0"}}"""

from typing import Any

import httpx

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField

HOSTS = {"vdsina.ru": ("userapi.vdsina.ru", "RUB"), "vdsina.com": ("userapi.vdsina.com", "USD")}


class VdsinaProvider(BalanceProvider):
    key = "vdsina"
    title = "VDSina"
    description = "Баланс хостинга VDSina: vdsina.ru (₽) или vdsina.com ($). Токен: личный кабинет → Настройки → API."
    fields = [
        ProviderField("api_key", "API токен", kind="secret"),
        ProviderField(
            "host", "Сайт", required=False, placeholder="vdsina.ru", help="vdsina.ru (рубли) или vdsina.com (доллары)"
        ),
    ]

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        host = str(config.get("host") or "vdsina.ru").strip().lower().removeprefix("https://").strip("/")
        if host not in HOSTS:
            raise ValueError("Сайт должен быть vdsina.ru или vdsina.com")
        api_host, currency = HOSTS[host]
        token = str(config["api_key"]).strip()
        url = f"https://{api_host}/v1/account.balance"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers={"Authorization": token})
            if resp.status_code in (401, 403):  # some accounts expect the Bearer form
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            payload = resp.json()
        if payload.get("status") not in (None, "ok"):
            raise ValueError(str(payload.get("status_msg") or payload.get("status")))
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        real = float(data.get("real") or 0)
        bonus = float(data.get("bonus") or 0)
        partner = float(data.get("partner") or 0)
        return BalanceResult(
            value=round(real, 2),
            currency=currency,
            extra={"bonus": bonus, "partner": partner, "total": round(real + bonus + partner, 2)},
        )
