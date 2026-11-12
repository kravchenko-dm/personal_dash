import re
from functools import lru_cache
from typing import Any

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INLINE_COMMENT = re.compile(r"\s+#.*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_domain: str = "localhost"
    app_secret: str = "dev-secret-change-me"
    dash_password: str = "dash"
    tz: str = "Europe/Moscow"

    database_url: str = "postgresql+asyncpg://dash:dash@localhost:5432/dash"

    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "change-me"
    telegram_owner_id: int | None = None

    # LLM for summaries. anthropic (default) or any OpenAI-compatible provider, see services/llm.
    llm_provider: str = (
        "anthropic"  # anthropic | kimi | deepseek | openrouter | openai | polza | qwen | ollama | custom
    )
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"

    # Speech-to-text for the bot (voice notes, audio/video files, YouTube links). Polza.AI only:
    # the key defaults to LLM_API_KEY when LLM_PROVIDER=polza. whisper-1 gives timecodes (subtitles);
    # the fallback is used when the main model fails.
    stt_api_key: str = ""
    stt_model: str = "openai/whisper-1"
    stt_fallback_model: str = "openai/whisper-large-v3-turbo"

    google_client_id: str = ""
    google_client_secret: str = ""

    yougile_api_key: str = ""

    icloud_username: str = ""
    icloud_app_password: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comments(cls, v: Any, info: ValidationInfo) -> Any:
        """docker compose passes `KEY=value  # note` verbatim; drop the note and treat blanks as unset."""
        if isinstance(v, str):
            v = _INLINE_COMMENT.sub("", v).strip()
            if v == "":
                return None if info.field_name == "telegram_owner_id" else ""
        return v

    @property
    def public_url(self) -> str:
        if self.app_domain in ("localhost", "127.0.0.1"):
            return f"http://{self.app_domain}:8000"
        return f"https://{self.app_domain}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def stt_key(self) -> str:
        if self.stt_api_key:
            return self.stt_api_key
        return self.llm_api_key if self.llm_provider.lower() == "polza" else ""

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    @property
    def llm_enabled(self) -> bool:
        from app.services.llm import describe

        return describe(self)["enabled"]

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
