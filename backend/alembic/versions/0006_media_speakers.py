"""media jobs: speaker names

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12 22:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("speakers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.drop_column("speakers")
