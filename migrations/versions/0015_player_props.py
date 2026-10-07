"""player props: append-only prop_pulls and prop_quotes

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("prop_pulls", "prop_quotes")


def upgrade() -> None:
    op.create_table(
        "prop_pulls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("pulled_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("books", sa.Integer(), nullable=False),
        sa.Column("quotes", sa.Integer(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_prop_pulls_game_id_games")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prop_pulls")),
        sa.UniqueConstraint(
            "game_id", "horizon_hours", name=op.f("uq_prop_pulls_game_id_horizon_hours")
        ),
    )
    op.create_index(op.f("ix_prop_pulls_game_id"), "prop_pulls", ["game_id"], unique=False)
    op.create_table(
        "prop_quotes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pull_id", sa.Integer(), nullable=False),
        sa.Column("sportsbook_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=60), nullable=False),
        sa.Column("player", sa.String(length=100), nullable=False),
        sa.Column("team_side", sa.String(length=4), nullable=False),
        sa.Column("selection", sa.String(length=100), nullable=False),
        sa.Column("point", sa.Float(), nullable=True),
        sa.Column("american_odds", sa.Float(), nullable=False),
        sa.Column("book_changed_at", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(
            ["pull_id"], ["prop_pulls.id"], name=op.f("fk_prop_quotes_pull_id_prop_pulls")
        ),
        sa.ForeignKeyConstraint(
            ["sportsbook_id"],
            ["sportsbooks.id"],
            name=op.f("fk_prop_quotes_sportsbook_id_sportsbooks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prop_quotes")),
    )
    op.create_index(op.f("ix_prop_quotes_pull_id"), "prop_quotes", ["pull_id"], unique=False)
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
    op.drop_index(op.f("ix_prop_quotes_pull_id"), table_name="prop_quotes")
    op.drop_table("prop_quotes")
    op.drop_index(op.f("ix_prop_pulls_game_id"), table_name="prop_pulls")
    op.drop_table("prop_pulls")
