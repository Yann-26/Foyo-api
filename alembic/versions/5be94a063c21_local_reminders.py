"""local reminders

Revision ID: 5be94a063c21
Revises: 16cd30e60208
Create Date: 2026-11-25 11:32:26.478095
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '5be94a063c21'
down_revision: str | None = '16cd30e60208'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_reminders', sa.Boolean(), server_default=sa.false(), nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('local_reminders')

