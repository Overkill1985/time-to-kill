"""player stats: NBA 3-pointers; NFL player game stats (nflverse)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NFL_COUNTS = (
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "interceptions",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "targets",
    "receptions",
    "receiving_yards",
    "receiving_tds",
)


def upgrade() -> None:
    # Added natively (no batch rebuild of a 300k-row table).
    op.add_column("player_game_stats", sa.Column("fg3m", sa.Integer(), nullable=True))
    op.add_column("player_game_stats", sa.Column("fg3a", sa.Integer(), nullable=True))
    op.create_table(
        "nfl_player_game_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=20), nullable=False),
        sa.Column("player_name", sa.String(length=100), nullable=False),
        sa.Column("position", sa.String(length=10), nullable=True),
        *(sa.Column(name, sa.Integer(), nullable=False) for name in NFL_COUNTS),
        sa.Column("imported_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_nfl_player_game_stats_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_nfl_player_game_stats_team_id_teams")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nfl_player_game_stats")),
        sa.UniqueConstraint(
            "game_id", "player_id", name=op.f("uq_nfl_player_game_stats_game_id_player_id")
        ),
    )
    op.create_index(
        op.f("ix_nfl_player_game_stats_game_id"),
        "nfl_player_game_stats",
        ["game_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_nfl_player_game_stats_game_id"), table_name="nfl_player_game_stats")
    op.drop_table("nfl_player_game_stats")
    op.drop_column("player_game_stats", "fg3a")
    op.drop_column("player_game_stats", "fg3m")
