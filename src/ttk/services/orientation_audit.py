"""Audit: forward-test records taken on a line stored with the teams reversed.

Until 2026-10-24 a provider event whose home and away were reversed after we
linked it kept its old orientation, so each side's spread was stored on the other
team (services/identity._reorient now re-checks every fetch). Records snapshotted
then carry the mirror of the real line. Judged against an independent close (ESPN's
DraftKings line; nflverse for the NFL), a spread or moneyline record is flipped when
both lines are at least ``MIN_POINTS`` from pick'em and the record is nearer to
minus the reference than to the reference. Flipped records are marked in
``forward_exclusions`` (never changed or deleted) and left out of scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import ForwardExclusion, ForwardPrediction, Game, ModelVersion, ReportedLine
from ttk.domain import GameStatus, Market, Sport
from ttk.providers.espn_odds import EspnCoreOdds

REFERENCES = ("espn:draftkings", "nflverse")
MIN_POINTS = 2.0
REASON = "line stored with the teams reversed (the mirror of the reference close)"


@dataclass(frozen=True)
class Flipped:
    forward_prediction_id: int
    model: str
    game_id: int
    horizon_hours: int
    home_line: float
    reference_line: float
    reference_provider: str


def is_mirrored(line: float, reference: float) -> bool:
    if abs(line) < MIN_POINTS or abs(reference) < MIN_POINTS:
        return False
    return abs(line + reference) < abs(line - reference)


def find_flipped(session: Session) -> list[Flipped]:
    """Flipped records on finished games not yet excluded."""
    refs: dict[int, tuple[float, str]] = {}
    for game_id, spread, provider in session.execute(
        select(ReportedLine.game_id, ReportedLine.home_spread, ReportedLine.provider).where(
            ReportedLine.provider.in_(REFERENCES), ReportedLine.home_spread.is_not(None)
        )
    ):
        if spread is not None:
            refs.setdefault(game_id, (float(spread), provider))
    excluded = select(ForwardExclusion.forward_prediction_id)
    out = []
    for fp, mv in session.execute(
        select(ForwardPrediction, ModelVersion)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .join(ModelVersion, ModelVersion.id == ForwardPrediction.model_version_id)
        .where(
            Game.status == GameStatus.FINAL,
            ForwardPrediction.market.in_([Market.SPREAD, Market.MONEYLINE]),
            ForwardPrediction.id.not_in(excluded),
        )
        .order_by(ForwardPrediction.id)
    ):
        ref = refs.get(fp.game_id)
        if ref is not None and is_mirrored(fp.home_line, ref[0]):
            out.append(
                Flipped(
                    fp.id,
                    f"{mv.name} {mv.version}",
                    fp.game_id,
                    fp.horizon_hours,
                    fp.home_line,
                    ref[0],
                    ref[1],
                )
            )
    return out


def exclude_flipped(session: Session, *, apply: bool = False) -> list[Flipped]:
    flipped = find_flipped(session)
    if apply:
        session.add_all(
            ForwardExclusion(
                forward_prediction_id=f.forward_prediction_id,
                reason=REASON,
                reference_line=f.reference_line,
                reference_provider=f.reference_provider,
            )
            for f in flipped
        )
        session.flush()
    return flipped


IN_SEASON_DAYS = 30


def current_seasons(session: Session, now: datetime) -> list[tuple[Sport, int]]:
    """(sport, season) with a finished game in the last ``IN_SEASON_DAYS`` days."""
    out = []
    for sport in (Sport.NFL, Sport.NBA, Sport.CFB, Sport.NCAAB):
        season = session.scalar(
            select(Game.season)
            .where(
                Game.sport == sport,
                Game.status == GameStatus.FINAL,
                Game.commence_time >= now - timedelta(days=IN_SEASON_DAYS),
                Game.season.is_not(None),
            )
            .order_by(Game.commence_time.desc())
            .limit(1)
        )
        if season is not None:
            out.append((sport, int(season)))
    return out


def daily_audit(
    session_factory: sessionmaker[Session], now: datetime, odds: EspnCoreOdds | None = None
) -> list[str]:
    """ESPN's closes for newly finished games (NFL has nflverse), then the exclusions.
    Returns log lines; a failed ESPN import is reported, not raised."""
    from ttk.services.espn_history_import import import_season_odds

    lines = []
    with session_factory() as session:
        seasons = current_seasons(session, now)
    odds = odds or EspnCoreOdds()
    for sport, season in seasons:
        if sport == Sport.NFL:
            continue
        run = import_season_odds(session_factory, sport, season, odds=odds)
        lines.append(
            f"ESPN closes {sport} {season}: {run.status} {run.records_written} lines"
            + (f", error={run.error}" if run.error else "")
        )
    with session_factory() as session:
        flipped = exclude_flipped(session, apply=True)
        session.commit()
    lines.append(f"orientation audit: {len(flipped)} forward records excluded")
    return lines
