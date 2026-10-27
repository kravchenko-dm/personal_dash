from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["todo", "doing", "done"]


class LoginIn(BaseModel):
    password: str


class TokenOut(BaseModel):
    token: str


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    notes: str | None = None
    status: TaskStatus = "todo"
    priority: int = Field(default=2, ge=1, le=3)
    due_at: datetime | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = None
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    due_at: datetime | None = None
    clear_due: bool = False


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    notes: str | None
    status: TaskStatus
    priority: int
    due_at: datetime | None
    source: str
    source_ref: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class BalanceAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str
    config: dict[str, Any] = Field(default_factory=dict)
    currency: str = ""
    alert_below: float | None = None
    enabled: bool = True


class BalanceAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    config: dict[str, Any]
    currency: str
    alert_below: float | None
    enabled: bool
    last_value: float | None
    last_extra: dict[str, Any] | None
    last_error: str | None
    last_checked_at: datetime | None


class BalanceSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    value: float
    taken_at: datetime


class ProviderInfo(BaseModel):
    key: str
    title: str
    description: str
    fields: list[dict[str, Any]]


class PushSubscriptionIn(BaseModel):
    endpoint: str
    keys: dict[str, str]
    user_agent: str | None = None


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int | None
    period_start: datetime
    period_end: datetime
    text: str
    proposed_tasks: list[dict[str, Any]]
    created_at: datetime


class TelegramChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    title: str
    kind: str
    collect: bool


class StatusOut(BaseModel):
    push: bool
    telegram: bool
    claude: bool
    google: bool
    google_connected: bool
    yougile: bool
    icloud: bool
    vapid_public_key: str
    bot_username: str | None
