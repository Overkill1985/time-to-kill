"""forward tests: a market column (spread or moneyline) on forward_predictions

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-06

Added natively with a server default (no batch rebuild), so the table keeps its
append-only triggers and existing rows read as SPREAD.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "forward_predictions",
        sa.Column("market", sa.String(length=12), nullable=False, server_default="SPREAD"),
    )


def downgrade() -> None:
    op.drop_column("forward_predictions", "market")
