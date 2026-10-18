"""prop predictions: append-only forward-test records of prop models

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("prop_predictions",)


def upgrade() -> None:
    op.create_table(
        "prop_predictions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("pull_id", sa.Integer(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("created_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.Column("player", sa.String(length=100), nullable=False),
        sa.Column("player_id", sa.String(length=20), nullable=False),
        sa.Column("market", sa.String(length=60), nullable=False),
        sa.Column("line", sa.Float(), nullable=False),
        sa.Column("p_over", sa.Float(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("market_over", sa.Float(), nullable=False),
        sa.Column("books", sa.Integer(), nullable=False),
        sa.Column("best_over", sa.Float(), nullable=True),
        sa.Column("best_over_book", sa.String(length=50), nullable=True),
        sa.Column("best_under", sa.Float(), nullable=True),
        sa.Column("best_under_book", sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_prop_predictions_model_version_id_model_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_prop_predictions_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["pull_id"], ["prop_pulls.id"], name=op.f("fk_prop_predictions_pull_id_prop_pulls")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prop_predictions")),
        sa.UniqueConstraint(
            "model_version_id",
            "pull_id",
            "player_id",
            "market",
            name=op.f("uq_prop_predictions_model_version_id_pull_id_player_id_market"),
        ),
    )
    op.create_index(
        op.f("ix_prop_predictions_model_version_id"),
        "prop_predictions",
        ["model_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_prop_predictions_game_id"), "prop_predictions", ["game_id"], unique=False
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
    op.drop_index(op.f("ix_prop_predictions_game_id"), table_name="prop_predictions")
    op.drop_index(op.f("ix_prop_predictions_model_version_id"), table_name="prop_predictions")
    op.drop_table("prop_predictions")
