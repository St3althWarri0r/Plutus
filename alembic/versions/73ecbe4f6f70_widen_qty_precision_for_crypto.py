"""widen qty precision for crypto

Broker crypto quantities carry 9+ decimal places (LINKUSD 20788.024399992);
Numeric(18, 6) rounding manufactured phantom reconcile mismatches. SQLite
cannot ALTER COLUMN, so these run in batch (table-recreate) mode.

Revision ID: 73ecbe4f6f70
Revises: 1ab30779a371
Create Date: 2026-08-16 22:41:55.146935

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '73ecbe4f6f70'
down_revision: str | None = '1ab30779a371'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ['bot_positions', 'fills', 'manual_baseline', 'orders']


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                'qty',
                existing_type=sa.NUMERIC(precision=18, scale=6),
                type_=sa.Numeric(precision=24, scale=10),
                existing_nullable=False,
            )


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                'qty',
                existing_type=sa.Numeric(precision=24, scale=10),
                type_=sa.NUMERIC(precision=18, scale=6),
                existing_nullable=False,
            )
