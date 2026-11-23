"""request collector: supplier mail, MAX private messages, inbox

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-20 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _user_id(**kw) -> sa.Column:
    return sa.Column(
        "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, **kw
    )


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        _user_id(),
        sa.Column("host", sa.String(length=200), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(length=200), nullable=False),
        sa.Column("password", sa.String(length=300), nullable=False),
        sa.Column("folder", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_uid", sa.BigInteger(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mail_accounts_user_id"), "mail_accounts", ["user_id"], unique=True)

    op.create_table(
        "mail_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        _user_id(),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("patterns", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mail_rules_user_id"), "mail_rules", ["user_id"], unique=False)

    op.create_table(
        "max_accounts",
        _user_id(),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("me_name", sa.String(length=200), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "inbox_items",
        sa.Column("id", sa.Integer(), nullable=False),
        _user_id(),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("external_id", sa.String(length=300), nullable=False),
        sa.Column("sender", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_inbox_items_source"),
    )
    op.create_index(op.f("ix_inbox_items_user_id"), "inbox_items", ["user_id"], unique=False)
    op.create_index(op.f("ix_inbox_items_created_at"), "inbox_items", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_table("inbox_items")
    op.drop_table("max_accounts")
    op.drop_table("mail_rules")
    op.drop_table("mail_accounts")
