"""team box-score totals per game

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov")


def upgrade() -> None:
    op.create_table(
        "team_game_boxes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in STATS],
        sa.Column("imported_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_team_game_boxes_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_team_game_boxes_team_id_teams")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_game_boxes")),
        sa.UniqueConstraint(
            "game_id",
            "team_id",
            "provider",
            name=op.f("uq_team_game_boxes_game_id_team_id_provider"),
        ),
    )
    op.create_index(
        op.f("ix_team_game_boxes_game_id"), "team_game_boxes", ["game_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_team_game_boxes_game_id"), table_name="team_game_boxes")
    op.drop_table("team_game_boxes")
