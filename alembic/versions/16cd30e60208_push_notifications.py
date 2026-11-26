"""push notifications

Revision ID: 16cd30e60208
Revises: 4d42f7ccecf9
Create Date: 2026-11-25 10:21:17.243767
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '16cd30e60208'
down_revision: str | None = '4d42f7ccecf9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('devices',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('token', sa.String(length=512), nullable=False),
    sa.Column('platform', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token')
    )
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_devices_user_id'), ['user_id'], unique=False)

    op.create_table('notification_logs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'key', name='uq_notification_user_key')
    )
    with op.batch_alter_table('notification_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_logs_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('push_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('quiet_hours', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('daily_report', sa.Boolean(), nullable=False, server_default=sa.true()))



def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('daily_report')
        batch_op.drop_column('quiet_hours')
        batch_op.drop_column('push_enabled')

    with op.batch_alter_table('notification_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_logs_user_id'))

    op.drop_table('notification_logs')
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_devices_user_id'))

    op.drop_table('devices')
