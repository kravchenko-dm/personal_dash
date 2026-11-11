"""login events

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "login_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("result", sa.String(length=12), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("login_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_login_events_ip"), ["ip"], unique=False)
        batch_op.create_index(batch_op.f("ix_login_events_ts"), ["ts"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("login_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_login_events_ts"))
        batch_op.drop_index(batch_op.f("ix_login_events_ip"))
    op.drop_table("login_events")
