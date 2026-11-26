"""idempotency keys

Revision ID: 939f4fa9d8a5
Revises: 5be94a063c21
Create Date: 2026-11-25 12:52:17.782792
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '939f4fa9d8a5'
down_revision: str | None = '5be94a063c21'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('idempotency_keys',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('method', sa.String(length=8), nullable=False),
    sa.Column('path', sa.String(length=255), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'key', name='uq_idempotency_user_key')
    )
    with op.batch_alter_table('idempotency_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_idempotency_keys_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_idempotency_keys_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('idempotency_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_idempotency_keys_user_id'))
        batch_op.drop_index(batch_op.f('ix_idempotency_keys_created_at'))

    op.drop_table('idempotency_keys')
