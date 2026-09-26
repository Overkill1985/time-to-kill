"""ORM schema. Change it only through an Alembic migration (migrations/versions).

Append-only tables (odds_snapshots, injury_reports, predictions) are protected
by database triggers created in the migration: rows can be inserted, never
updated or deleted. A changed price or prediction is a new row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC; always returns timezone-aware UTC. SQLite otherwise drops the offset.
    Naive datetimes are rejected rather than guessed."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime; pass a timezone-aware value")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


# ----------------------------------------------------------------------- reference data


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("sport", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    abbreviation: Mapped[str | None] = mapped_column(String(10))


class TeamAlias(Base):
    """How each provider names a team. Resolves provider names to one Team."""

    __tablename__ = "team_aliases"
    __table_args__ = (UniqueConstraint("provider", "sport", "alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    sport: Mapped[str] = mapped_column(String(10))
    alias: Mapped[str] = mapped_column(String(100))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))


class Sportsbook(Base):
    __tablename__ = "sportsbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (Index("ix_games_sport_commence", "sport", "commence_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(10))
    season: Mapped[int | None] = mapped_column(Integer)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    commence_time: Mapped[datetime]
    neutral_site: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])


class GameSourceId(Base):
    """Provider event id -> Game. One game can be known to many providers."""

    __tablename__ = "game_source_ids"
    __table_args__ = (UniqueConstraint("provider", "source_identifier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    source_identifier: Mapped[str] = mapped_column(String(100))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))


# ----------------------------------------------------------------------- ingestion


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(30))
    sport: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[dict[str, int] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class OddsSnapshot(Base):
    """One observed price. Immutable: every poll appends; nothing is overwritten.
    Opening/closing lines are the first/last snapshots before commence_time."""

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        Index("ix_odds_lookup", "game_id", "market", "selection", "sportsbook_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    sportsbook_id: Mapped[int] = mapped_column(ForeignKey("sportsbooks.id"))
    market: Mapped[str] = mapped_column(String(20))
    selection: Mapped[str] = mapped_column(String(10))
    line: Mapped[float | None] = mapped_column(Float)
    american_odds: Mapped[float] = mapped_column(Float)
    decimal_odds: Mapped[float] = mapped_column(Float)
    implied_probability: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(50))
    source_timestamp: Mapped[datetime | None]
    observed_at: Mapped[datetime]
    """When this app ingested the price (the ingestion_timestamp)."""
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"))
    data_version: Mapped[int] = mapped_column(Integer, default=1)


class InjuryReport(Base):
    """Append-only injury observations; a prediction references the state it used."""

    __tablename__ = "injury_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(10))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    player_name: Mapped[str] = mapped_column(String(100))
    player_source_identifier: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    injury_type: Mapped[str | None] = mapped_column(String(100))
    expected_minutes: Mapped[float | None] = mapped_column(Float)
    estimated_impact: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(50))
    source_timestamp: Mapped[datetime | None]
    observed_at: Mapped[datetime] = mapped_column(default=utcnow)


# ----------------------------------------------------------------------- models


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(30))
    sport: Mapped[str] = mapped_column(String(10))
    market: Mapped[str] = mapped_column(String(20))
    algorithm: Mapped[str] = mapped_column(String(50))
    features: Mapped[list[str] | None] = mapped_column(JSON)
    training_window: Mapped[str | None] = mapped_column(String(50))
    validation_window: Mapped[str | None] = mapped_column(String(50))
    calibration_method: Mapped[str | None] = mapped_column(String(30))
    brier_score: Mapped[float | None] = mapped_column(Float)
    log_loss: Mapped[float | None] = mapped_column(Float)
    historical_roi: Mapped[float | None] = mapped_column(Float)
    historical_clv: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="DEVELOPMENT")
    health: Mapped[str] = mapped_column(String(20), default="HEALTHY")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deployed_at: Mapped[datetime | None]


class Prediction(Base):
    """Immutable prediction snapshot. A re-run writes a new row."""

    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_game", "game_id", "market", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    market: Mapped[str] = mapped_column(String(20))
    selection: Mapped[str] = mapped_column(String(10))
    line: Mapped[float | None] = mapped_column(Float)
    probability: Mapped[float] = mapped_column(Float)
    push_probability: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[str] = mapped_column(String(20))
    data_quality: Mapped[str] = mapped_column(String(20))
    features: Mapped[dict[str, float] | None] = mapped_column(JSON)
    """Feature values used, so the prediction is traceable and reproducible."""
    inputs_as_of: Mapped[datetime]
    """Latest timestamp of any input used (no-leakage audit)."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# ----------------------------------------------------------------------- bets


class Parlay(Base):
    __tablename__ = "parlays"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sportsbook_id: Mapped[int | None] = mapped_column(ForeignKey("sportsbooks.id"))
    american_odds: Mapped[float | None] = mapped_column(Float)
    stake: Mapped[float | None] = mapped_column(Float)
    model_joint_probability: Mapped[float | None] = mapped_column(Float)
    correlation_risk: Mapped[str | None] = mapped_column(String(20))
    result: Mapped[str] = mapped_column(String(10), default="PENDING")
    profit_loss: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)


class Bet(Base):
    """A real wager, or a leg of a parlay (parlay_id set)."""

    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True)
    placed_at: Mapped[datetime] = mapped_column(default=utcnow)
    sport: Mapped[str] = mapped_column(String(10))
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"))
    parlay_id: Mapped[int | None] = mapped_column(ForeignKey("parlays.id"))
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    sportsbook_id: Mapped[int | None] = mapped_column(ForeignKey("sportsbooks.id"))
    market: Mapped[str] = mapped_column(String(20))
    selection: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(String(200))
    line: Mapped[float | None] = mapped_column(Float)
    american_odds: Mapped[float] = mapped_column(Float)
    model_probability: Mapped[float | None] = mapped_column(Float)
    market_probability: Mapped[float | None] = mapped_column(Float)
    edge: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    stake: Mapped[float | None] = mapped_column(Float)
    result: Mapped[str] = mapped_column(String(10), default="PENDING")
    profit_loss: Mapped[float | None] = mapped_column(Float)
    closing_line: Mapped[float | None] = mapped_column(Float)
    closing_american_odds: Mapped[float | None] = mapped_column(Float)
    closing_no_vig_probability: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
