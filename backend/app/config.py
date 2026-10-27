from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"

    google_client_id: str = ""
    google_client_secret: str = ""

    yougile_api_key: str = ""

    icloud_username: str = ""
    icloud_app_password: str = ""

    @property
    def public_url(self) -> str:
        if self.app_domain in ("localhost", "127.0.0.1"):
            return f"http://{self.app_domain}:8000"
        return f"https://{self.app_domain}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
