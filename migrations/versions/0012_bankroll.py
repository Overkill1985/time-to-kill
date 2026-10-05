"""bankroll: append-only deposits/withdrawals and staking-limit policies

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-05

bets and parlays gain bankroll_at_bet and limit_override, added natively (no
batch rebuild, so the existing tables keep their triggers).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("bankroll_entries", "bankroll_policies")


def upgrade() -> None:
    for table in ("bets", "parlays"):
        op.add_column(table, sa.Column("bankroll_at_bet", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("limit_override", sa.Text(), nullable=True))
    op.create_table(
        "bankroll_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bankroll_entries")),
    )
    op.create_table(
        "bankroll_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("kelly_multiplier", sa.Float(), nullable=False),
        sa.Column("max_stake_fraction", sa.Float(), nullable=False),
        sa.Column("max_daily_fraction", sa.Float(), nullable=False),
        sa.Column("max_open_fraction", sa.Float(), nullable=False),
        sa.Column("stop_drawdown_fraction", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bankroll_policies")),
    )
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("Add immutability triggers for this database dialect")
    for table in APPEND_ONLY:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )


def downgrade() -> None:
    for table in APPEND_ONLY:
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{action}")
    op.drop_table("bankroll_policies")
    op.drop_table("bankroll_entries")
    for table in ("parlays", "bets"):
        op.drop_column(table, "limit_override")
        op.drop_column(table, "bankroll_at_bet")
