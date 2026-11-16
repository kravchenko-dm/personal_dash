"""users: personal workspaces for people other than the owner

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14 12:00:00
"""

import json
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OWNED = ("tasks", "telegram_chats", "summaries", "push_subscriptions")


def upgrade() -> None:
    users = op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("token_epoch", sa.BigInteger(), nullable=False),
        sa.Column("evening_digest", sa.Boolean(), nullable=False),
        sa.Column("morning_brief", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    base = {"token_epoch": 0, "evening_digest": True, "morning_brief": True, "created_at": now}
    # the owner is inserted first so the sequence hands out id 1 on both SQLite and Postgres
    conn.execute(users.insert().values(name="", username="", role="owner", level="workspace", **base))

    # bot guests used to live in the settings table: move them over as "media" members
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
    row = conn.execute(sa.select(settings.c.value).where(settings.c.key == "telegram.guests")).first()
    value = row[0] if row else None
    if isinstance(value, str):
        value = json.loads(value)
    for g in (value or {}).get("guests", []):
        conn.execute(
            users.insert().values(
                telegram_id=int(g["id"]), name=(g.get("name") or "")[:100],
                username=(g.get("username") or "")[:64], role="member", level="media", **base,
            )
        )  # fmt: skip
    conn.execute(settings.delete().where(settings.c.key == "telegram.guests"))

    for table in OWNED:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=False, server_default="1"))
            batch_op.create_index(batch_op.f(f"ix_{table}_user_id"), ["user_id"], unique=False)
            batch_op.create_foreign_key(f"fk_{table}_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE")
    with op.batch_alter_table("login_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("method", sa.String(length=12), nullable=False, server_default="password"))
        batch_op.create_index(batch_op.f("ix_login_events_user_id"), ["user_id"], unique=False)
        batch_op.create_foreign_key("fk_login_events_user_id", "users", ["user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("login_events", schema=None) as batch_op:
        batch_op.drop_constraint("fk_login_events_user_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_login_events_user_id"))
        batch_op.drop_column("method")
        batch_op.drop_column("user_id")
    for table in OWNED:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_user_id", type_="foreignkey")
            batch_op.drop_index(batch_op.f(f"ix_{table}_user_id"))
            batch_op.drop_column("user_id")
    op.drop_table("users")
