"""prop legs: player, prop market and graded value on bets

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bets", sa.Column("player", sa.String(length=100), nullable=True))
    op.add_column("bets", sa.Column("prop_market", sa.String(length=60), nullable=True))
    op.add_column("bets", sa.Column("actual_value", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bets") as batch:
        batch.drop_column("actual_value")
        batch.drop_column("prop_market")
        batch.drop_column("player")
