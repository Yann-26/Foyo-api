"""premium

Revision ID: e0b201f4a96d
Revises: a792510314a5
Create Date: 2026-11-25 14:43:17.684168
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'e0b201f4a96d'
down_revision: str | None = 'a792510314a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('households', schema=None) as batch_op:
        batch_op.add_column(sa.Column('premium_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('premium_source', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('premium_period', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('premium_will_renew', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('trial_used', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.drop_column('is_premium')



def downgrade() -> None:
    with op.batch_alter_table('households', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_premium', sa.BOOLEAN(), server_default=sa.false(), nullable=False))
        batch_op.drop_column('trial_used')
        batch_op.drop_column('premium_will_renew')
        batch_op.drop_column('premium_period')
        batch_op.drop_column('premium_source')
        batch_op.drop_column('premium_until')

