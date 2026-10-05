"""forward scores: stored closing-line value of finished forward snapshots

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forward_scores",
        sa.Column("forward_prediction_id", sa.Integer(), nullable=False),
        sa.Column("price_clv", sa.Float(), nullable=True),
        sa.Column("points_vs_close", sa.Float(), nullable=True),
        sa.Column("scored_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["forward_prediction_id"],
            ["forward_predictions.id"],
            name=op.f("fk_forward_scores_forward_prediction_id_forward_predictions"),
        ),
        sa.PrimaryKeyConstraint("forward_prediction_id", name=op.f("pk_forward_scores")),
    )
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("Add immutability triggers for this database dialect")
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER forward_scores_no_{action.lower()} BEFORE {action} "
            "ON forward_scores BEGIN "
            "SELECT RAISE(ABORT, 'forward_scores is append-only'); END"
        )


def downgrade() -> None:
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS forward_scores_no_{action}")
    op.drop_table("forward_scores")
