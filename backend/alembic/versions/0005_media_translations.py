"""media jobs: cached translations, summary title

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12 20:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("summary_title", sa.String(length=300), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("translations", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table("media_jobs", schema=None) as batch_op:
        batch_op.drop_column("translations")
        batch_op.drop_column("summary_title")
