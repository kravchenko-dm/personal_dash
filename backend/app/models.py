from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo | doing | done
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1 high, 2 normal, 3 low
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")  # manual | telegram | ai | apple
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BalanceAccount(Base):
    """A configured balance source (an API key for some service, a manual account, ...)."""

    __tablename__ = "balance_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))  # key in integrations.registry
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # provider-specific (keys, urls)
    currency: Mapped[str] = mapped_column(String(10), default="")
    alert_below: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BalanceSample(Base):
    __tablename__ = "balance_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("balance_accounts.id", ondelete="CASCADE"))
    value: Mapped[float] = mapped_column(Float)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict] = mapped_column(JSON)  # {p256dh, auth}
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramChat(Base):
    """A Telegram chat the bot is a member of."""

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20), default="group")  # private | group | supergroup
    collect: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    sender: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summarized: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    proposed_tasks: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthToken(Base):
    """Stored OAuth credentials, one row per provider (e.g. 'google')."""

    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Setting(Base):
    """Key/value settings editable from the UI (e.g. selected calendars)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class AppleReminder(Base):
    """Snapshot of open Apple Reminders, pushed by an iOS Shortcut (or read over CalDAV for
    legacy lists). Replaced wholesale on every sync, so no stable Apple id is required."""

    __tablename__ = "apple_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)  # hash(list,title,due) for dedupe
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    list_name: Mapped[str] = mapped_column(String(200), default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 0 none, 1 high, 5 medium, 9 low (Apple)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="shortcuts")  # shortcuts | caldav
    imported_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
