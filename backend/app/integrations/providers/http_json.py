from typing import Any

import httpx
from jsonpath_ng import parse as jsonpath_parse

from app.integrations.base import BalanceProvider, BalanceResult, ProviderField


class HttpJsonProvider(BalanceProvider):
    """Generic GET request + JSONPath. Covers most 'show me my balance' APIs without code."""

    key = "http_json"
    title = "HTTP JSON (универсальный)"
    description = "GET-запрос к любому API и JSONPath до числа, например $.data.balance."
    fields = [
        ProviderField("url", "URL", kind="url", placeholder="https://api.example.com/v1/balance"),
        ProviderField(
            "headers",
            "Заголовки",
            required=False,
            placeholder="Authorization: Bearer xxx\nX-Api-Key: yyy",
            help="По одному на строку: Имя: значение",
        ),
        ProviderField("path", "JSONPath к значению", placeholder="$.data.balance"),
        ProviderField("currency_path", "JSONPath к валюте", required=False, placeholder="$.data.currency"),
    ]

    @staticmethod
    def _headers(raw: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in (raw or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return headers

    @staticmethod
    def _extract(data: Any, path: str) -> Any:
        matches = jsonpath_parse(path).find(data)
        if not matches:
            raise ValueError(f"JSONPath {path} ничего не нашёл")
        return matches[0].value

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(config["url"], headers=self._headers(config.get("headers")))
            resp.raise_for_status()
            data = resp.json()
        value = float(self._extract(data, config["path"]))
        currency = ""
        if config.get("currency_path"):
            currency = str(self._extract(data, config["currency_path"]))
        return BalanceResult(value=value, currency=currency)
