"""estimated duration

Revision ID: 004dad61f42b
Revises: 939f4fa9d8a5
Create Date: 2026-11-25 13:05:18.426304
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '004dad61f42b'
down_revision: str | None = '939f4fa9d8a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('occurrences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('duration_estimated', sa.Boolean(), server_default=sa.false(), nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('occurrences', schema=None) as batch_op:
        batch_op.drop_column('duration_estimated')

