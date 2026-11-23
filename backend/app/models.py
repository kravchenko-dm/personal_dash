from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


OWNER_ID = 1  # the dashboard owner's row in `users`, created by migration 0007


class User(Base):
    """Someone who may use the bot. The owner (id 1) has everything; a member with level "workspace" has
    their own tasks, chats, summaries and digests; level "media" may only transcribe recordings."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(10), default="member")  # owner | member
    level: Mapped[str] = mapped_column(String(10), default="media")  # media | workspace
    token_epoch: Mapped[int] = mapped_column(BigInteger, default=0)  # this user's "log out everywhere"
    evening_digest: Mapped[bool] = mapped_column(Boolean, default=True)
    morning_brief: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _owner_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), default=OWNER_ID, index=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner_fk()
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
    user_id: Mapped[int] = _owner_fk()
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict] = mapped_column(JSON)  # {p256dh, auth}
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramChat(Base):
    """A Telegram chat the bot is a member of."""

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    user_id: Mapped[int] = _owner_fk()  # who added the bot: their summaries and digests read this chat
    title: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20), default="group")  # private | group | supergroup
    collect: Mapped[bool] = mapped_column(Boolean, default=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    user_id: Mapped[int] = _owner_fk()
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
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


class Project(Base):
    """One of the services under development (lives on its own VPS)."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(String(20), default="dev")  # idea | dev | beta | prod | paused
    color: Mapped[str] = mapped_column(String(10), default="#63a0ff")
    repo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    site_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    yougile_board_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # "what is happening now"
    status_note_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Feature(Base):
    """A card on a project's feature board."""

    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="idea"
    )  # idea | planned | in_progress | testing | done
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1 high, 2 normal, 3 low
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | ai
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectNote(Base):
    """Important data about a project: environments, URLs, checklists, contacts. Markdown body."""

    __tablename__ = "project_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Server(Base):
    """A VPS that runs the dash-agent. Authenticates with its own token (hash stored)."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hostname: Mapped[str | None] = mapped_column(String(200), nullable=True)
    os: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # metrics + services as sent
    disk_alert_pct: Mapped[int] = mapped_column(Integer, default=85)
    silence_alert_min: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    load1: Mapped[float] = mapped_column(Float, default=0)
    mem_pct: Mapped[float] = mapped_column(Float, default=0)
    disk_pct: Mapped[float] = mapped_column(Float, default=0)  # worst disk


class Monitor(Base):
    """External HTTP check performed by the dashboard itself."""

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(1000))
    method: Mapped[str] = mapped_column(String(10), default="GET")
    expect_status: Mapped[int] = mapped_column(Integer, default=200)
    expect_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_s: Mapped[int] = mapped_column(Integer, default=10)
    interval_s: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckSample(Base):
    __tablename__ = "check_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Incident(Base):
    """Something is wrong (or was). One open incident per (project, kind, key)."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), nullable=True)
    monitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(
        String(40)
    )  # http_down | agent_silent | disk_high | service_down | unit_failed | ...
    key: Mapped[str] = mapped_column(String(200), default="")
    severity: Mapped[str] = mapped_column(String(10), default="critical")  # warning | critical
    message: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentCommand(Base):
    __tablename__ = "agent_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # ping | restart | stop | start | logs | script
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | sent | done | failed
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackupPlan(Base):
    """What to back up on a server, how often, how long to keep. Executed by the agent via restic."""

    __tablename__ = "backup_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    items: Mapped[list] = mapped_column(
        JSON, default=list
    )  # [{type: path|pg_dump|mysql_dump|docker_volume, ...}]
    schedule: Mapped[str] = mapped_column(String(100), default="0 3 * * *")  # cron, dashboard TZ
    retention: Mapped[dict] = mapped_column(JSON, default=dict)  # {daily, weekly, monthly}
    max_age_hours: Mapped[int] = mapped_column(Integer, default=36)  # overdue alert threshold
    repo_subpath: Mapped[str] = mapped_column(String(200), default="")  # under destination base path
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(12), nullable=True)  # ok | failed | running
    repo_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshots: Mapped[list | None] = mapped_column(JSON, nullable=True)  # cached list from the agent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("backup_plans.id", ondelete="CASCADE"), index=True)
    command_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_commands.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="backup")  # backup | restore | check | snapshots
    trigger: Mapped[str] = mapped_column(String(10), default="schedule")  # schedule | manual
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)


class LoginEvent(Base):
    """Every attempt to log in to the dashboard: kept in the DB so it survives deploys."""

    __tablename__ = "login_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    result: Mapped[str] = mapped_column(String(12))  # ok | failed | blocked
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(12), default="password")  # password | telegram


class MediaJob(Base):
    """A transcribed voice note, audio/video file or link (YouTube etc.) sent to the bot."""

    __tablename__ = "media_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(500))  # URL or "telegram:<kind>"
    title: Mapped[str] = mapped_column(String(300), default="")
    language: Mapped[str] = mapped_column(String(10), default="")
    duration: Mapped[float] = mapped_column(Float, default=0)
    stt_model: Mapped[str] = mapped_column(String(60), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list] = mapped_column(JSON, default=list)  # [[start, end, text(, speaker)], ...]
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_title: Mapped[str] = mapped_column(String(300), default="")  # the LLM's title, in Russian
    proposed_tasks: Mapped[list] = mapped_column(JSON, default=list)
    cost_rub: Mapped[float] = mapped_column(Float, default=0)
    # cached translations: {"lines:en": [...per segment...], "summary:en": {"title": ..., "text": ...}}
    translations: Mapped[dict] = mapped_column(JSON, default=dict)
    # speaker label from the diarizing STT ("1:S0") -> name the summary gave them ("Ведущий")
    speakers: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------- request collector: mail from suppliers, private MAX messages ----------


def _user_fk(**kw) -> Mapped[int]:
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, **kw)


class MailAccount(Base):
    """A user's IMAP mailbox (Mail.ru by default), read-only; new mail from their suppliers goes to the bot."""

    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _user_fk(unique=True)
    host: Mapped[str] = mapped_column(String(200), default="imap.mail.ru")
    port: Mapped[int] = mapped_column(Integer, default=993)
    login: Mapped[str] = mapped_column(String(200))
    password: Mapped[str] = mapped_column(String(300))  # an app password; never returned by the API
    folder: Mapped[str] = mapped_column(String(200), default="INBOX")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # None: not read yet
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    errors: Mapped[int] = mapped_column(Integer, default=0)  # failures in a row
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MailRule(Base):
    """A supplier: mail from any of `patterns` ("ivan@x.ru" or "@supplier.ru") is forwarded."""

    __tablename__ = "mail_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _user_fk()
    name: Mapped[str] = mapped_column(String(100))
    patterns: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MaxAccount(Base):
    """A user's MAX account connected by QR; the session itself lives in /app/data/max/<user_id>/."""

    __tablename__ = "max_accounts"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="off")  # off|waiting_qr|online|error|logged_out
    me_name: Mapped[str] = mapped_column(String(200), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboxItem(Base):
    """Something the collector forwarded to the bot (a supplier's mail, a MAX message)."""

    __tablename__ = "inbox_items"
    __table_args__ = (UniqueConstraint("user_id", "source", "external_id", name="uq_inbox_items_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _user_fk()
    source: Mapped[str] = mapped_column(String(10))  # mail | max
    external_id: Mapped[str] = mapped_column(String(300))
    sender: Mapped[str] = mapped_column(String(200), default="")  # supplier or contact name
    subject: Mapped[str] = mapped_column(String(500), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
