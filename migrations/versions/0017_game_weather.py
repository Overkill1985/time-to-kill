"""game weather: reanalysis weather at past games' kickoff (research input)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_weather",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("venue_source", sa.String(length=50), nullable=False),
        sa.Column("venue_id", sa.String(length=20), nullable=False),
        sa.Column("venue_name", sa.String(length=120), nullable=False),
        sa.Column("indoor", sa.Boolean(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("valid_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("wind_mph", sa.Float(), nullable=True),
        sa.Column("gust_mph", sa.Float(), nullable=True),
        sa.Column("temperature_f", sa.Float(), nullable=True),
        sa.Column("precipitation_mm", sa.Float(), nullable=True),
        sa.Column("fetched_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_game_weather_game_id_games")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_weather")),
        sa.UniqueConstraint("game_id", "provider", name=op.f("uq_game_weather_game_id_provider")),
    )
    op.create_index(op.f("ix_game_weather_game_id"), "game_weather", ["game_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_game_weather_game_id"), table_name="game_weather")
    op.drop_table("game_weather")
