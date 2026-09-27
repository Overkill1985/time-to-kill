"""player box scores; injury report change-log fields

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27

injury_reports columns are added with SQLite's native ALTER TABLE ADD COLUMN
(op.add_column outside batch mode) so the table's append-only triggers survive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_game_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=20), nullable=False),
        sa.Column("player_name", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("starter", sa.Boolean(), nullable=False),
        sa.Column("played", sa.Boolean(), nullable=False),
        sa.Column("dnp_reason", sa.String(length=100), nullable=True),
        sa.Column("minutes", sa.Float(), nullable=True),
        *[
            sa.Column(name, sa.Integer(), nullable=True)
            for name in (
                "points",
                "fgm",
                "fga",
                "ftm",
                "fta",
                "oreb",
                "dreb",
                "ast",
                "stl",
                "blk",
                "tov",
                "pf",
                "plus_minus",
            )
        ],
        sa.Column("imported_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_player_game_stats_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_player_game_stats_team_id_teams")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_player_game_stats")),
        sa.UniqueConstraint(
            "game_id",
            "player_id",
            "provider",
            name=op.f("uq_player_game_stats_game_id_player_id_provider"),
        ),
    )
    op.create_index(
        op.f("ix_player_game_stats_game_id"), "player_game_stats", ["game_id"], unique=False
    )
    op.add_column("injury_reports", sa.Column("comment", sa.Text(), nullable=True))
    op.add_column("injury_reports", sa.Column("return_date", sa.String(length=10), nullable=True))
    op.add_column(
        "injury_reports",
        sa.Column("cleared", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_injury_reports_player",
        "injury_reports",
        ["provider", "sport", "player_source_identifier", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_injury_reports_player", table_name="injury_reports")
    op.drop_column("injury_reports", "cleared")
    op.drop_column("injury_reports", "return_date")
    op.drop_column("injury_reports", "comment")
    op.drop_index(op.f("ix_player_game_stats_game_id"), table_name="player_game_stats")
    op.drop_table("player_game_stats")
