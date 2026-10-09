"""weather forecasts: append-only weather_forecasts

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("weather_forecasts",)


def upgrade() -> None:
    op.create_table(
        "weather_forecasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("stadium_id", sa.String(length=10), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("valid_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("fetched_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("wind_mph", sa.Float(), nullable=False),
        sa.Column("gust_mph", sa.Float(), nullable=True),
        sa.Column("temperature_f", sa.Float(), nullable=True),
        sa.Column("precipitation_mm", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_weather_forecasts_game_id_games")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weather_forecasts")),
        sa.UniqueConstraint(
            "game_id", "horizon_hours", name=op.f("uq_weather_forecasts_game_id_horizon_hours")
        ),
    )
    op.create_index(
        op.f("ix_weather_forecasts_game_id"), "weather_forecasts", ["game_id"], unique=False
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
    op.drop_index(op.f("ix_weather_forecasts_game_id"), table_name="weather_forecasts")
    op.drop_table("weather_forecasts")
