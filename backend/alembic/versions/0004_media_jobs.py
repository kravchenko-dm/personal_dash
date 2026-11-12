"""media jobs (bot transcription)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("stt_model", sa.String(length=60), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("proposed_tasks", sa.JSON(), nullable=False),
        sa.Column("cost_rub", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_jobs_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_jobs_created_at"))
    op.drop_table("media_jobs")
