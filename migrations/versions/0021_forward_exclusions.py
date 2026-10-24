"""forward exclusions: append-only marks on forward-test records left out of scoring

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ttk.db.models

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("forward_exclusions",)


def upgrade() -> None:
    op.create_table(
        "forward_exclusions",
        sa.Column("forward_prediction_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("reference_line", sa.Float(), nullable=True),
        sa.Column("reference_provider", sa.String(length=50), nullable=True),
        sa.Column("created_at", ttk.db.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["forward_prediction_id"],
            ["forward_predictions.id"],
            name=op.f("fk_forward_exclusions_forward_prediction_id_forward_predictions"),
        ),
        sa.PrimaryKeyConstraint("forward_prediction_id", name=op.f("pk_forward_exclusions")),
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
    op.drop_table("forward_exclusions")
