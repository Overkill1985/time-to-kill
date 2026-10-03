"""team season features (preseason data)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team_season_features",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=10), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("known_at", ttk.db.models.UTCDateTime(), nullable=True),
        sa.Column("imported_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_team_season_features_team_id_teams")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_season_features")),
        sa.UniqueConstraint(
            "sport",
            "season",
            "team_id",
            "provider",
            "name",
            name=op.f("uq_team_season_features_sport_season_team_id_provider_name"),
        ),
    )


def downgrade() -> None:
    op.drop_table("team_season_features")
