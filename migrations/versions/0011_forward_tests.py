"""forward tests: frozen model artifacts and append-only forward predictions

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-03

model_versions.artifact is added natively (no batch rebuild).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("artifact", sa.JSON(), nullable=True))
    op.create_table(
        "forward_predictions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("home_line", sa.Float(), nullable=False),
        sa.Column("home_cover_probability", sa.Float(), nullable=False),
        sa.Column("push_probability", sa.Float(), nullable=False),
        sa.Column("market_home_cover", sa.Float(), nullable=False),
        sa.Column("books", sa.Integer(), nullable=False),
        sa.Column("best_home_odds", sa.Float(), nullable=True),
        sa.Column("best_home_book", sa.String(length=50), nullable=True),
        sa.Column("best_away_odds", sa.Float(), nullable=True),
        sa.Column("best_away_book", sa.String(length=50), nullable=True),
        sa.Column("expected_margin", sa.Float(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("inputs_as_of", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_forward_predictions_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_forward_predictions_model_version_id_model_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forward_predictions")),
        sa.UniqueConstraint(
            "model_version_id",
            "game_id",
            "horizon_hours",
            name=op.f("uq_forward_predictions_model_version_id_game_id_horizon_hours"),
        ),
    )
    op.create_index(
        op.f("ix_forward_predictions_game_id"), "forward_predictions", ["game_id"], unique=False
    )
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("Add immutability triggers for this database dialect")
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER forward_predictions_no_{action.lower()} BEFORE {action} "
            "ON forward_predictions BEGIN "
            "SELECT RAISE(ABORT, 'forward_predictions is append-only'); END"
        )


def downgrade() -> None:
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS forward_predictions_no_{action}")
    op.drop_index(op.f("ix_forward_predictions_game_id"), table_name="forward_predictions")
    op.drop_table("forward_predictions")
    op.drop_column("model_versions", "artifact")
