"""The daily Time-to-Kill card: every game on a date, what the model and the market
believe, and whether anything qualifies - with the Why-Not for everything else.

- The card day runs 06:00 to 06:00 US Eastern, so late West Coast kickoffs belong
  to the day they are played.
- Only games that have not started are evaluated; a started game's pregame
  prices are gone.
- Where no validated model exists (every market except NFL spreads today), the
  game is listed as unmodeled rather than given an invented probability.
- Every evaluated side is written as an immutable prediction snapshot.
- Sorting uses classification then EV. No composite score is shown.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Game, GameSourceId, Prediction, Team, utcnow
from ttk.domain import (
    BetClassification,
    Market,
    ModelHealth,
    ModelStatus,
    Selection,
    Sport,
)
from ttk.qualification import Check, Opportunity, QualificationRules, qualify
from ttk.services.data_quality import QualityInputs, assess
from ttk.services.line_history import line_history
from ttk.services.market import SideMarket, main_lines, side_markets
from ttk.services.nfl_spread_predictor import NflSpreadPredictor

EASTERN = ZoneInfo("America/New_York")
_RANK = {
    BetClassification.QUALIFIED: 0,
    BetClassification.LEAN: 1,
    BetClassification.PASS: 2,
    BetClassification.NO_BET: 3,
}


def card_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(6, 0), tzinfo=EASTERN)
    return start, start + timedelta(days=1)


@dataclass(frozen=True)
class CardEntry:
    sport: Sport
    game_id: int
    matchup: str
    commence_time: datetime
    market: Market
    selection: Selection
    bet: str
    line: float | None
    sportsbook: str
    american_odds: float
    model_probability: float
    """P(win | no push)."""
    push_probability: float
    market_probability: float
    edge: float
    fair_american_odds: float
    ev_percent: float
    uncertainty: str
    data_quality: str
    model_version: str
    odds_age_minutes: float
    line_opening: float | None
    line_current: float | None
    classification: BetClassification
    checks: tuple[Check, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class UnmodeledGame:
    sport: Sport
    game_id: int
    matchup: str
    commence_time: datetime
    reason: str


@dataclass
class SportSummary:
    games: int = 0
    with_market: int = 0
    modeled: int = 0
    qualified: int = 0
    lean: int = 0


@dataclass
class DailyCard:
    day: date
    generated_at: datetime
    entries: list[CardEntry] = field(default_factory=list)
    unmodeled: list[UnmodeledGame] = field(default_factory=list)
    unbettable: list[str] = field(default_factory=list)
    """Sides no bettable book quotes at the consensus line."""
    bettable_books: frozenset[str] | None = None
    by_sport: dict[Sport, SportSummary] = field(default_factory=dict)

    @property
    def qualified(self) -> list[CardEntry]:
        return [e for e in self.entries if e.classification is BetClassification.QUALIFIED]

    @property
    def headline(self) -> str:
        n = len(self.qualified)
        return "NO QUALIFIED BETS TODAY" if n == 0 else f"{n} QUALIFIED OPPORTUNITIES"


def _team_names(session: Session, game: Game) -> tuple[str, str]:
    home = session.get_one(Team, game.home_team_id).name
    away = session.get_one(Team, game.away_team_id).name
    return home, away


def _find(markets: list[SideMarket], selection: Selection, line: float | None) -> SideMarket | None:
    return next(
        (
            m
            for m in markets
            if m.market is Market.SPREAD and m.selection is selection and m.line == line
        ),
        None,
    )


def _median_lines(session: Session, game_id: int) -> tuple[float | None, float | None]:
    """Median opening and current home spread across books."""
    hist = line_history(session, game_id, Market.SPREAD, Selection.HOME)
    opens = [h.opening.line for h in hist if h.opening.line is not None]
    currents = [h.current.line for h in hist if h.current and h.current.line is not None]
    return (
        statistics.median(opens) if opens else None,
        statistics.median(currents) if currents else None,
    )


def build_card(
    session: Session,
    day: date,
    rules: QualificationRules,
    *,
    predictor: NflSpreadPredictor | None,
    now: datetime | None = None,
    persist: bool = True,
    bettable_books: frozenset[str] | None = None,
) -> DailyCard:
    """``bettable_books``: book keys whose prices can be bet (None = all). The
    market probability is the all-book consensus either way."""
    now = now or utcnow()
    start, end = card_window(day)
    card = DailyCard(day=day, generated_at=now, bettable_books=bettable_books)
    games = session.scalars(
        select(Game)
        .where(Game.commence_time >= start, Game.commence_time < end)
        .order_by(Game.commence_time)
    ).all()
    model_version = predictor.ensure_registered(session) if predictor else None

    for game in games:
        sport = Sport(game.sport)
        summary = card.by_sport.setdefault(sport, SportSummary())
        summary.games += 1
        home_name, away_name = _team_names(session, game)
        matchup = f"{away_name} @ {home_name}"
        if game.commence_time <= now:
            continue  # started: pregame prices are gone
        all_markets = side_markets(session, game.id)
        if not all_markets:
            continue
        summary.with_market += 1
        mains = main_lines(all_markets)
        home_main = next(
            (m for m in mains if m.market is Market.SPREAD and m.selection is Selection.HOME),
            None,
        )
        if sport is not Sport.NFL or predictor is None or model_version is None:
            card.unmodeled.append(
                UnmodeledGame(
                    sport,
                    game.id,
                    matchup,
                    game.commence_time,
                    "no validated model for this sport yet",
                )
            )
            continue
        if home_main is None or home_main.line is None:
            card.unmodeled.append(
                UnmodeledGame(
                    sport, game.id, matchup, game.commence_time, "no two-sided spread market"
                )
            )
            continue
        home_line = home_main.line
        view = predictor.spread(
            game.id, home_line, home_main.consensus.consensus_no_vig_probability
        )
        if view is None:
            card.unmodeled.append(
                UnmodeledGame(
                    sport, game.id, matchup, game.commence_time, "no model inputs for this game"
                )
            )
            continue
        summary.modeled += 1
        linked = (
            session.scalar(
                select(GameSourceId.id).where(
                    GameSourceId.game_id == game.id, GameSourceId.provider == "espn"
                )
            )
            is not None
        )
        opening, current = _median_lines(session, game.id)
        f = view.features
        disagreement = view.expected_margin + home_line

        sides = (
            (Selection.HOME, home_name, home_line, view.home_cover, home_main),
            (
                Selection.AWAY,
                away_name,
                -home_line,
                1.0 - view.home_cover,
                _find(all_markets, Selection.AWAY, -home_line),
            ),
        )
        for selection, team, line, p_model, sm in sides:
            if sm is None:
                continue
            c = sm.consensus
            prices = [
                b for b in c.books if bettable_books is None or b.sportsbook in bettable_books
            ]
            if not prices:
                card.unbettable.append(f"{team} {line:+g} ({matchup})")
                continue
            best = max(prices, key=lambda b: b.decimal_odds)
            odds_age = now - sm.oldest_observation
            assessment = assess(
                QualityInputs(
                    odds_age=odds_age,
                    books=c.books_reporting,
                    schedule_linked=linked,
                    home_team_games=f.home_team_games,
                    away_team_games=f.away_team_games,
                    home_starter_known=(game.id, game.home_team_id) in predictor.starters,
                    away_starter_known=(game.id, game.away_team_id) in predictor.starters,
                    home_starter_seen=f.home_qb_history > 0,
                    away_starter_seen=f.away_qb_history > 0,
                    disagreement_points=disagreement,
                ),
                max_odds_age=rules.max_odds_age,
            )
            result = qualify(
                Opportunity(
                    model_probability=p_model,
                    no_vig_probability=c.consensus_no_vig_probability,
                    decimal_odds=best.decimal_odds,
                    odds_timestamp=sm.oldest_observation,
                    data_quality=assessment.data_quality,
                    uncertainty=assessment.uncertainty,
                    model_status=ModelStatus(model_version.status),
                    model_health=ModelHealth(model_version.health),
                    push_probability=view.push,
                ),
                rules,
                now=now,
            )
            if persist:
                session.add(
                    Prediction(
                        game_id=game.id,
                        model_version_id=model_version.id,
                        market=Market.SPREAD,
                        selection=selection,
                        line=line,
                        probability=p_model,
                        push_probability=view.push,
                        uncertainty=assessment.uncertainty,
                        data_quality=assessment.data_quality,
                        features={
                            "elo_diff": view.elo_diff,
                            "epa_net_diff_pts": f.epa_net_diff_pts,
                            "qb_change_diff_pts": f.qb_change_diff_pts,
                            "expected_margin": view.expected_margin,
                            "market_no_vig": c.consensus_no_vig_probability,
                        },
                        inputs_as_of=now,
                    )
                )
            sign = 1 if selection is Selection.HOME else -1
            summary.qualified += result.classification is BetClassification.QUALIFIED
            summary.lean += result.classification is BetClassification.LEAN
            card.entries.append(
                CardEntry(
                    sport=sport,
                    game_id=game.id,
                    matchup=matchup,
                    commence_time=game.commence_time,
                    market=Market.SPREAD,
                    selection=selection,
                    bet=f"{team} {'PK' if line == 0 else f'{line:+g}'}",
                    line=line,
                    sportsbook=best.sportsbook,
                    american_odds=bm.decimal_to_american(best.decimal_odds),
                    model_probability=p_model,
                    push_probability=view.push,
                    market_probability=c.consensus_no_vig_probability,
                    edge=result.edge,
                    fair_american_odds=bm.fair_american_odds(min(max(p_model, 1e-6), 1 - 1e-6)),
                    ev_percent=result.ev_per_unit * 100,
                    uncertainty=assessment.uncertainty.value,
                    data_quality=assessment.data_quality.value,
                    model_version=f"{model_version.name} {model_version.version} "
                    f"({model_version.status})",
                    odds_age_minutes=odds_age.total_seconds() / 60,
                    line_opening=None if opening is None else sign * opening,
                    line_current=None if current is None else sign * current,
                    classification=result.classification,
                    checks=result.checks,
                    notes=assessment.reasons + result.reasons,
                )
            )
    if persist:
        session.commit()
    card.entries.sort(key=lambda e: (_RANK[e.classification], -e.ev_percent))
    return card
