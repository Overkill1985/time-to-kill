"""espn team identity and swapped game links

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 14:42:47.520074

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ttk.teams import normalize_team_name

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingestion_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stats", sa.JSON(), nullable=True))

    with op.batch_alter_table("game_source_ids", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("swapped", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("team_aliases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("normalized", sa.String(length=100), nullable=False, server_default="")
        )

    # Backfill with the same function the application uses.
    conn = op.get_bind()
    for alias_id, alias in conn.execute(sa.text("SELECT id, alias FROM team_aliases")).all():
        conn.execute(
            sa.text("UPDATE team_aliases SET normalized = :n WHERE id = :id"),
            {"n": normalize_team_name(alias), "id": alias_id},
        )

    # Databases created before the naming convention have this constraint unnamed;
    # newer ones name it uq_team_aliases_provider_sport_alias. The convention below
    # gives a reflected unnamed constraint that same name, so one drop covers both.
    # A name may now belong to several teams (resolution treats that as ambiguous).
    with op.batch_alter_table(
        "team_aliases", naming_convention={"uq": "uq_%(table_name)s_%(column_0_N_name)s"}
    ) as batch_op:
        batch_op.drop_constraint("uq_team_aliases_provider_sport_alias", type_="unique")
        batch_op.create_unique_constraint(
            "uq_team_aliases_provider_sport_alias_team_id",
            ["provider", "sport", "alias", "team_id"],
        )
        batch_op.create_index("ix_team_aliases_normalized", ["sport", "normalized"], unique=False)

    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.add_column(sa.Column("espn_id", sa.String(length=20), nullable=True))
        batch_op.create_unique_constraint("uq_teams_sport_espn_id", ["sport", "espn_id"])


def downgrade() -> None:
    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.drop_constraint("uq_teams_sport_espn_id", type_="unique")
        batch_op.drop_column("espn_id")

    with op.batch_alter_table("team_aliases", schema=None) as batch_op:
        batch_op.drop_index("ix_team_aliases_normalized")
        batch_op.drop_constraint("uq_team_aliases_provider_sport_alias_team_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_team_aliases_provider_sport_alias", ["provider", "sport", "alias"]
        )
        batch_op.drop_column("normalized")

    with op.batch_alter_table("game_source_ids", schema=None) as batch_op:
        batch_op.drop_column("swapped")

    with op.batch_alter_table("ingestion_runs", schema=None) as batch_op:
        batch_op.drop_column("stats")
