"""accounts social invite codes

Revision ID: a792510314a5
Revises: 004dad61f42b
Create Date: 2026-11-25 13:20:20.321552
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'a792510314a5'
down_revision: str | None = '004dad61f42b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing households get no expiry date: their shared link is off until the admin creates a new one.
    with op.batch_alter_table('households', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invite_code_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('invite_role', sa.String(length=20), server_default='adult', nullable=False))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('google_sub', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('apple_sub', sa.String(length=255), nullable=True))
        batch_op.create_unique_constraint('uq_users_google_sub', ['google_sub'])
        batch_op.create_unique_constraint('uq_users_apple_sub', ['apple_sub'])



def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('uq_users_apple_sub', type_='unique')
        batch_op.drop_constraint('uq_users_google_sub', type_='unique')
        batch_op.drop_column('apple_sub')
        batch_op.drop_column('google_sub')

    with op.batch_alter_table('households', schema=None) as batch_op:
        batch_op.drop_column('invite_role')
        batch_op.drop_column('invite_code_expires_at')

