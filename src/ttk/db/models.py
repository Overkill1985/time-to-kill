"""ORM schema. Change it only through an Alembic migration (migrations/versions).

Append-only tables (odds_snapshots, injury_reports, predictions) are protected
by database triggers created in the migration: rows can be inserted, never
updated or deleted. A changed price or prediction is a new row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
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


# Named constraints so migrations can drop/alter them (SQLite batch mode needs names).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime()}


# ----------------------------------------------------------------------- reference data


class Team(Base):
    """One real team. espn_id is the canonical identity; a team without one was
    seen only by a non-ESPN provider and could not be matched (see ttk.teams)."""

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("sport", "name"),
        UniqueConstraint("sport", "espn_id", name="uq_teams_sport_espn_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    abbreviation: Mapped[str | None] = mapped_column(String(10))
    espn_id: Mapped[str | None] = mapped_column(String(20))


class TeamAlias(Base):
    """How each provider names a team. Resolves provider names to one Team."""

    __tablename__ = "team_aliases"
    __table_args__ = (
        # A name may belong to several teams; resolution treats that as ambiguous.
        UniqueConstraint("provider", "sport", "alias", "team_id"),
        Index("ix_team_aliases_normalized", "sport", "normalized"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    sport: Mapped[str] = mapped_column(String(10))
    alias: Mapped[str] = mapped_column(String(100))
    normalized: Mapped[str] = mapped_column(String(100))
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
    season_type: Mapped[str | None] = mapped_column(String(4))
    week: Mapped[int | None] = mapped_column(Integer)
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
    swapped: Mapped[bool] = mapped_column(Boolean, default=False)
    """The provider lists home/away the other way round (e.g. a neutral-site game).
    Its HOME/AWAY selections are flipped on ingestion."""


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
    """Payload items dropped during normalization, by reason."""
    stats: Mapped[dict[str, int] | None] = mapped_column(JSON)
    """Identity resolution outcomes (linked_by_espn_event_id, unmatched_team, ...)."""
    error: Mapped[str | None] = mapped_column(Text)


class OddsSnapshot(Base):
    """A change to one quote: (game, book, provider, market, selection, line).

    Change log, append-only: a row is written when a quote first appears, when its
    price changes, and when it disappears (``withdrawn``). A quote's state at time
    T is its latest row at or before T; it is on the board iff that row is not
    withdrawn. When a book was *seen* (freshness, closing time) comes from
    ``book_observations``, not from these rows."""

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        Index("ix_odds_lookup", "game_id", "market", "selection", "sportsbook_id", "observed_at"),
        Index(
            "ix_odds_quote_key",
            "provider",
            "game_id",
            "sportsbook_id",
            "market",
            "selection",
            "line",
        ),
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
    """When this app first saw this state (the ingestion_timestamp)."""
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"))
    data_version: Mapped[int] = mapped_column(Integer, default=1)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)
    """The quote left the board; price fields repeat its last price."""


class BookObservation(Base):
    """A book's quotes for a game were present in a poll. Append-only. The latest
    observation at or before T is when that book's state was last confirmed."""

    __tablename__ = "book_observations"
    __table_args__ = (
        Index("ix_book_observations_game", "game_id", "sportsbook_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    sportsbook_id: Mapped[int] = mapped_column(ForeignKey("sportsbooks.id"))
    provider: Mapped[str] = mapped_column(String(50))
    observed_at: Mapped[datetime]
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"))
    quotes: Mapped[int] = mapped_column(Integer)
    """How many quotes the book showed for the game in this poll."""


class TeamSeasonFeature(Base):
    """One preseason fact about a team-season (e.g. returning production, roster
    talent, recruiting, a new head coach, the preseason poll). ``known_at`` is when
    it became public; a model may use it only for games after that time.
    Re-imports replace rows (providers revise)."""

    __tablename__ = "team_season_features"
    __table_args__ = (UniqueConstraint("sport", "season", "team_id", "provider", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(10))
    season: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    provider: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column(Float)
    known_at: Mapped[datetime | None]
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class TeamGameStat(Base):
    """One team's offensive play-by-play aggregates for one game (its defense is the
    opponent's row). Offensive plays = pass or run plays with an EPA value,
    excluding two-point attempts. Re-imports replace rows (upstream revises EPA)."""

    __tablename__ = "team_game_stats"
    __table_args__ = (UniqueConstraint("game_id", "team_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    provider: Mapped[str] = mapped_column(String(50))
    plays: Mapped[int] = mapped_column(Integer)
    epa_total: Mapped[float] = mapped_column(Float)
    successes: Mapped[int] = mapped_column(Integer)
    dropbacks: Mapped[int] = mapped_column(Integer)
    dropback_epa_total: Mapped[float] = mapped_column(Float)
    rushes: Mapped[int] = mapped_column(Integer)
    rush_epa_total: Mapped[float] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class QbGameStat(Base):
    """A quarterback's dropback EPA in one game (passes, sacks, scrambles)."""

    __tablename__ = "qb_game_stats"
    __table_args__ = (UniqueConstraint("game_id", "player_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[str] = mapped_column(String(20))
    """nflverse / NFL GSIS id, e.g. 00-0035228."""
    player_name: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    dropbacks: Mapped[int] = mapped_column(Integer)
    qb_epa_total: Mapped[float] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class GameStarter(Base):
    """Who started (or is listed to start) at a position. Known at kickoff; a model
    using it assumes the bet is placed once starters are known."""

    __tablename__ = "game_starters"
    __table_args__ = (UniqueConstraint("game_id", "team_id", "position", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    position: Mapped[str] = mapped_column(String(5))
    player_id: Mapped[str] = mapped_column(String(20))
    player_name: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class TeamGameBox(Base):
    """One team's box-score totals in one game (ESPN), for possession-based
    efficiency. Points = 2 x FGM + 3PM + FTM; possessions are estimated from these."""

    __tablename__ = "team_game_boxes"
    __table_args__ = (UniqueConstraint("game_id", "team_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    provider: Mapped[str] = mapped_column(String(50))
    fgm: Mapped[int] = mapped_column(Integer)
    fga: Mapped[int] = mapped_column(Integer)
    fg3m: Mapped[int] = mapped_column(Integer)
    fg3a: Mapped[int] = mapped_column(Integer)
    ftm: Mapped[int] = mapped_column(Integer)
    fta: Mapped[int] = mapped_column(Integer)
    oreb: Mapped[int] = mapped_column(Integer)
    dreb: Mapped[int] = mapped_column(Integer)
    tov: Mapped[int] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlayerGameStat(Base):
    """One player's box-score line in one game (ESPN). Players who were injured or
    inactive are absent; healthy scratches appear with ``played`` False and a
    ``dnp_reason``. Known at tip-off: a model using who played assumes the bet is
    placed once lineups are known (never at an earlier price)."""

    __tablename__ = "player_game_stats"
    __table_args__ = (UniqueConstraint("game_id", "player_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[str] = mapped_column(String(20))
    """ESPN athlete id."""
    player_name: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    starter: Mapped[bool] = mapped_column(Boolean)
    played: Mapped[bool] = mapped_column(Boolean)
    dnp_reason: Mapped[str | None] = mapped_column(String(100))
    minutes: Mapped[float | None] = mapped_column(Float)
    points: Mapped[int | None] = mapped_column(Integer)
    fgm: Mapped[int | None] = mapped_column(Integer)
    fga: Mapped[int | None] = mapped_column(Integer)
    ftm: Mapped[int | None] = mapped_column(Integer)
    fta: Mapped[int | None] = mapped_column(Integer)
    oreb: Mapped[int | None] = mapped_column(Integer)
    dreb: Mapped[int | None] = mapped_column(Integer)
    ast: Mapped[int | None] = mapped_column(Integer)
    stl: Mapped[int | None] = mapped_column(Integer)
    blk: Mapped[int | None] = mapped_column(Integer)
    tov: Mapped[int | None] = mapped_column(Integer)
    pf: Mapped[int | None] = mapped_column(Integer)
    plus_minus: Mapped[int | None] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class ReportedLine(Base):
    """A historical line as reported by a data provider (e.g. nflverse), with no
    documented timestamp. Benchmark data. A model may take it as an input only when
    the simulated bet is placed at this same line; never for a bet at an earlier
    price, never as a CLV closing line. One row per (game, provider); re-imports
    replace it, since upstream revises history."""

    __tablename__ = "reported_lines"
    __table_args__ = (UniqueConstraint("game_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    provider: Mapped[str] = mapped_column(String(50))
    home_spread: Mapped[float | None] = mapped_column(Float)
    """Bookmaker-style home line: -3.5 = home favored by 3.5."""
    home_spread_odds: Mapped[float | None] = mapped_column(Float)
    away_spread_odds: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)
    over_odds: Mapped[float | None] = mapped_column(Float)
    under_odds: Mapped[float | None] = mapped_column(Float)
    home_moneyline: Mapped[float | None] = mapped_column(Float)
    away_moneyline: Mapped[float | None] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)


class InjuryReport(Base):
    """Append-only injury observations; a prediction references the state it used."""

    __tablename__ = "injury_reports"
    __table_args__ = (
        Index(
            "ix_injury_reports_player",
            "provider",
            "sport",
            "player_source_identifier",
            "observed_at",
        ),
    )

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
    """When the provider last updated the entry (ESPN's ``date``)."""
    observed_at: Mapped[datetime] = mapped_column(default=utcnow)
    comment: Mapped[str | None] = mapped_column(Text)
    return_date: Mapped[str | None] = mapped_column(String(10))
    """The provider's expected return date (YYYY-MM-DD), as reported."""
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    """True: the player left the provider's injury list at ``observed_at``."""


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
    artifact: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    """Frozen fitted parameters (forward tests use exactly these; see
    services/forward_test.py). A changed model is a new version."""


class ForwardPrediction(Base):
    """A forward-test snapshot: one model's view of one game at a stated horizon
    before kickoff, with the market we saw at that moment. Append-only."""

    __tablename__ = "forward_predictions"
    __table_args__ = (UniqueConstraint("model_version_id", "game_id", "horizon_hours"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    snapshot_at: Mapped[datetime]
    home_line: Mapped[float] = mapped_column(Float)
    """The main home spread at the snapshot (bookmaker sign: -3.5 = home favoured)."""
    home_cover_probability: Mapped[float] = mapped_column(Float)
    """The model's P(home covers | no push) at ``home_line``."""
    push_probability: Mapped[float] = mapped_column(Float)
    market_home_cover: Mapped[float] = mapped_column(Float)
    """All-book consensus no-vig P(home covers) at ``home_line``, at the snapshot."""
    books: Mapped[int] = mapped_column(Integer)
    best_home_odds: Mapped[float | None] = mapped_column(Float)
    best_home_book: Mapped[str | None] = mapped_column(String(50))
    best_away_odds: Mapped[float | None] = mapped_column(Float)
    best_away_book: Mapped[str | None] = mapped_column(String(50))
    """Best bettable American prices at the snapshot (bettable books only)."""
    expected_margin: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict[str, float] | None] = mapped_column(JSON)
    inputs_as_of: Mapped[datetime]
    """Latest input used: the newest finished game, odds or injury observation."""
    market: Mapped[str] = mapped_column(String(12), default="SPREAD", server_default="SPREAD")
    """SPREAD, or MONEYLINE: then ``home_cover_probability`` is P(home wins | no
    tie), ``market_home_cover`` and the best prices are the moneyline's, and
    ``home_line`` is the main spread the model was centered on."""


class ForwardScore(Base):
    """Closing-line value of a forward snapshot's side, computed once its game is
    final: the closing state as of kickoff never changes, so it is stored rather
    than replayed from the odds history on every report. Append-only."""

    __tablename__ = "forward_scores"

    forward_prediction_id: Mapped[int] = mapped_column(
        ForeignKey("forward_predictions.id"), primary_key=True
    )
    price_clv: Mapped[float | None] = mapped_column(Float)
    points_vs_close: Mapped[float | None] = mapped_column(Float)
    scored_at: Mapped[datetime] = mapped_column(default=utcnow)


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
    """A placed parlay at one book. Its legs are Bet rows with parlay_id set; each
    leg keeps its own beliefs as of placed_at and its own graded result."""

    __tablename__ = "parlays"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    placed_at: Mapped[datetime | None]
    sportsbook_id: Mapped[int | None] = mapped_column(ForeignKey("sportsbooks.id"))
    american_odds: Mapped[float | None] = mapped_column(Float)
    """The price actually taken (a book's same-game parlay price may differ from the
    product of leg prices)."""
    stake: Mapped[float | None] = mapped_column(Float)
    model_joint_probability: Mapped[float | None] = mapped_column(Float)
    """Product of leg probabilities: an independence assumption, flagged when legs
    share a game."""
    market_joint_probability: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    correlation_risk: Mapped[str | None] = mapped_column(String(20))
    result: Mapped[str] = mapped_column(String(10), default="PENDING")
    profit_loss: Mapped[float | None] = mapped_column(Float)
    settled_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    bankroll_at_bet: Mapped[float | None] = mapped_column(Float)
    limit_override: Mapped[str | None] = mapped_column(Text)


class Bet(Base):
    """A real wager, or a leg of a parlay (parlay_id set). Model and market beliefs
    are captured as of placed_at; settlement adds result, P/L and CLV."""

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
    """Consensus no-vig probability at the bet's own line at kickoff."""
    clv: Mapped[float | None] = mapped_column(Float)
    """Price CLV: bet decimal x closing no-vig - 1 (None if no book closed at the line)."""
    closing_points_gained: Mapped[float | None] = mapped_column(Float)
    """Points gained versus the consensus closing main line (spreads, totals)."""
    model_push_probability: Mapped[float | None] = mapped_column(Float)
    settled_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    bankroll_at_bet: Mapped[float | None] = mapped_column(Float)
    """Bankroll when the bet was placed (None if no bankroll was set up)."""
    limit_override: Mapped[str | None] = mapped_column(Text)
    """The breached bankroll limits and the user's reason, when placed over a limit."""


class BankrollEntry(Base):
    """A deposit to or withdrawal from the betting bankroll. Append-only: a mistake
    is corrected by an ADJUSTMENT entry, never by editing history."""

    __tablename__ = "bankroll_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(12))
    amount: Mapped[float] = mapped_column(Float)
    """Signed: deposits positive, withdrawals negative."""
    note: Mapped[str | None] = mapped_column(Text)


class BankrollPolicy(Base):
    """Staking limits. Append-only: the newest row is in force, older rows are the
    history of what the limits were when each bet was placed."""

    __tablename__ = "bankroll_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    kelly_multiplier: Mapped[float] = mapped_column(Float)
    max_stake_fraction: Mapped[float] = mapped_column(Float)
    max_daily_fraction: Mapped[float] = mapped_column(Float)
    max_open_fraction: Mapped[float] = mapped_column(Float)
    stop_drawdown_fraction: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
