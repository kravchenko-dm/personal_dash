from app.integrations.base import BalanceProvider
from app.integrations.providers.cheatbot import CheatBotProvider
from app.integrations.providers.clo import CloProvider
from app.integrations.providers.deepseek import DeepSeekProvider
from app.integrations.providers.http_json import HttpJsonProvider
from app.integrations.providers.manual import ManualProvider
from app.integrations.providers.openrouter import OpenRouterProvider
from app.integrations.providers.polza import PolzaProvider
from app.integrations.providers.smm_panel import SmmPanelProvider
from app.integrations.providers.subgram import SubgramProvider
from app.integrations.providers.vdsina import VdsinaProvider

PROVIDERS: dict[str, type[BalanceProvider]] = {
    p.key: p
    for p in (ManualProvider, HttpJsonProvider, OpenRouterProvider, DeepSeekProvider, VdsinaProvider, CloProvider, SmmPanelProvider, SubgramProvider, CheatBotProvider, PolzaProvider)
}


def get_provider(key: str) -> BalanceProvider:
    cls = PROVIDERS.get(key)
    if not cls:
        raise KeyError(f"Unknown balance provider: {key}")
    return cls()


def describe_providers() -> list[dict]:
    return [
        {
            "key": p.key,
            "title": p.title,
            "description": p.description,
            "fields": [f.as_dict() for f in p.fields],
        }
        for p in PROVIDERS.values()
    ]
