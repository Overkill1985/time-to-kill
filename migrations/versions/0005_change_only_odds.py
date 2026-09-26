"""change-only odds storage: withdrawn flag, book observations

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26

odds_snapshots becomes a change log. The new column is added with SQLite's
native ALTER TABLE ADD COLUMN (op.add_column outside batch mode): batch mode
would rebuild the table and silently drop its append-only triggers.

Existing rows were written one full poll at a time, so each (game, book,
provider, observed_at) group is backfilled as one book observation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "odds_snapshots",
        sa.Column("withdrawn", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_odds_quote_key",
        "odds_snapshots",
        ["provider", "game_id", "sportsbook_id", "market", "selection", "line"],
    )
    op.create_table(
        "book_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("sportsbook_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("observed_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("ingestion_run_id", sa.Integer(), nullable=False),
        sa.Column("quotes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_book_observations_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_book_observations_ingestion_run_id_ingestion_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["sportsbook_id"],
            ["sportsbooks.id"],
            name=op.f("fk_book_observations_sportsbook_id_sportsbooks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_observations")),
    )
    op.create_index(
        "ix_book_observations_game",
        "book_observations",
        ["game_id", "sportsbook_id", "observed_at"],
    )
    op.execute(
        "INSERT INTO book_observations "
        "(game_id, sportsbook_id, provider, observed_at, ingestion_run_id, quotes) "
        "SELECT game_id, sportsbook_id, provider, observed_at, MIN(ingestion_run_id), COUNT(*) "
        "FROM odds_snapshots GROUP BY game_id, sportsbook_id, provider, observed_at"
    )
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("Add immutability triggers for this database dialect")
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER book_observations_no_{action.lower()} BEFORE {action} "
            "ON book_observations BEGIN "
            "SELECT RAISE(ABORT, 'book_observations is append-only'); END"
        )


def downgrade() -> None:
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS book_observations_no_{action}")
    op.drop_index("ix_book_observations_game", table_name="book_observations")
    op.drop_table("book_observations")
    op.drop_index("ix_odds_quote_key", table_name="odds_snapshots")
    # SQLite 3.35+ drops a column natively, keeping odds_snapshots' triggers intact.
    op.execute("ALTER TABLE odds_snapshots DROP COLUMN withdrawn")
