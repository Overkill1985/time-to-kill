"""Forward tests: snapshot frozen models before kickoff, score them after.

Every model here is DEVELOPMENT: forward predictions are recorded and scored,
never qualified as bets. A snapshot is taken once per (model, game, horizon),
when a game is within ``horizon`` hours of kickoff (and, for the longer horizon,
not yet inside the shorter one). It records the main spread, the all-book
consensus no-vig probability and the best bettable prices *as we saw them*, so
the score is what a bettor at that moment could have done.

Scoring (``forward_report``) uses finished games only: log loss of the model and
of the market at the snapshot (paired), closing-line value against our own
closing prices at the same number, and results at the snapshot's best price.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ttk import betting_math as bm
from ttk.db.models import ForwardPrediction, ForwardScore, Game, ModelVersion, Team, utcnow
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.services.line_history import ClosingSnapshot, closing_snapshot
from ttk.services.market import SideMarket, main_lines, side_markets

HORIZONS_HOURS = (24, 1)
MIN_DECIDED_FOR_Z = 30
MAX_ODDS_AGE = timedelta(hours=2)
EDGE_THRESHOLDS = (0.0, 0.02, 0.04, 0.06)


@dataclass(frozen=True)
class ModelView:
    home_cover: float
    push: float
    expected_margin: float | None
    features: dict[str, float]


@dataclass(frozen=True)
class ForwardModel:
    """One model under forward test: its registry row and how it prices a game."""

    version: ModelVersion
    sport: Sport
    view: Callable[[int, float, float, datetime, int], ModelView | None]
    """(game_id, home line, market no-vig P(home covers), snapshot time, horizon
    hours) -> the model's view."""
    inputs_as_of: datetime
    """Newest finished game behind the model's state."""
    refresh: Callable[[Session], None] | None = None
    """Called at the start of each snapshot pass to reload fast-moving inputs
    (NBA injury reports) that the 6-hourly model build would leave stale."""
    market: Market = Market.SPREAD
    """MONEYLINE models get the same view call (main spread and its market) and
    return P(home wins | no tie) as ``home_cover`` and P(tie) as ``push``; the
    snapshot records the moneyline market and prices alongside."""


def register(
    session: Session,
    *,
    name: str,
    version: str,
    sport: Sport,
    algorithm: str,
    features: list[str],
    artifact: dict[str, Any] | None,
    training_window: str,
    validation_window: str,
    log_loss: float | None,
    brier: float | None,
) -> ModelVersion:
    """The registry row for a forward-tested model, created once (DEVELOPMENT)."""
    row = session.scalar(
        select(ModelVersion).where(ModelVersion.name == name, ModelVersion.version == version)
    )
    if row is not None:
        if artifact is not None and row.artifact != artifact:
            raise ValueError(f"{name} {version} is frozen with a different artifact; bump it")
        return row
    row = ModelVersion(
        name=name,
        version=version,
        sport=sport,
        market="SPREAD",
        algorithm=algorithm,
        features=features,
        training_window=training_window,
        validation_window=validation_window,
        calibration_method="fitted on TRAIN",
        brier_score=brier,
        log_loss=log_loss,
        status="DEVELOPMENT",
        artifact=artifact,
    )
    session.add(row)
    session.flush()
    return row


@dataclass
class SnapshotStats:
    written: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


def _main_spread(session: Session, game_id: int) -> tuple[SideMarket, SideMarket] | None:
    markets = side_markets(session, game_id)
    home = next(
        (
            m
            for m in main_lines(markets)
            if m.market is Market.SPREAD and m.selection is Selection.HOME
        ),
        None,
    )
    if home is None or home.line is None:
        return None
    away = next(
        (
            m
            for m in markets
            if m.market is Market.SPREAD and m.selection is Selection.AWAY and m.line == -home.line
        ),
        None,
    )
    return (home, away) if away is not None else None


def _main_moneyline(session: Session, game_id: int) -> tuple[SideMarket, SideMarket] | None:
    sides = {
        m.selection: m
        for m in main_lines(side_markets(session, game_id))
        if m.market is Market.MONEYLINE
    }
    home, away = sides.get(Selection.HOME), sides.get(Selection.AWAY)
    return (home, away) if home is not None and away is not None else None


def _best(sm: SideMarket, bettable: frozenset[str] | None) -> tuple[float | None, str | None]:
    prices = [b for b in sm.consensus.books if bettable is None or b.sportsbook in bettable]
    if not prices:
        return None, None
    best = max(prices, key=lambda b: b.decimal_odds)
    return bm.decimal_to_american(best.decimal_odds), best.sportsbook


def due_horizon(
    kickoff: datetime, now: datetime, horizons: Sequence[int] = HORIZONS_HOURS
) -> int | None:
    """The horizon a game is inside now: the smallest h with kickoff - now <= h."""
    lead = kickoff - now
    if lead <= timedelta(0):
        return None
    inside = [h for h in horizons if lead <= timedelta(hours=h)]
    return min(inside) if inside else None


def snapshot(
    session: Session,
    models: Sequence[ForwardModel],
    *,
    now: datetime | None = None,
    horizons: Sequence[int] = HORIZONS_HOURS,
    bettable_books: frozenset[str] | None = None,
) -> SnapshotStats:
    now = now or utcnow()
    stats = SnapshotStats()
    longest = max(horizons)
    for model in models:
        if model.refresh is not None:
            model.refresh(session)
        games = session.scalars(
            select(Game).where(
                Game.sport == model.sport,
                Game.commence_time > now,
                Game.commence_time <= now + timedelta(hours=longest),
                # Preseason games aren't modelled (no competitive results to learn).
                or_(Game.season_type.is_(None), Game.season_type != "PRE"),
            )
        ).all()
        for game in games:
            h = due_horizon(game.commence_time, now, horizons)
            if h is None:
                continue
            exists = session.scalar(
                select(ForwardPrediction.id).where(
                    ForwardPrediction.model_version_id == model.version.id,
                    ForwardPrediction.game_id == game.id,
                    ForwardPrediction.horizon_hours == h,
                )
            )
            if exists is not None:
                continue
            spread = _main_spread(session, game.id)
            if spread is None:
                stats.skipped["no two-sided spread"] += 1
                continue
            home, away = spread
            if now - min(home.oldest_observation, away.oldest_observation) > MAX_ODDS_AGE:
                stats.skipped["odds older than 2 hours"] += 1
                continue
            assert home.line is not None
            market = home.consensus.consensus_no_vig_probability
            priced_home, priced_away = home, away
            if model.market is Market.MONEYLINE:
                moneyline = _main_moneyline(session, game.id)
                if moneyline is None:
                    stats.skipped["no two-sided moneyline"] += 1
                    continue
                priced_home, priced_away = moneyline
                if (
                    now - min(priced_home.oldest_observation, priced_away.oldest_observation)
                    > MAX_ODDS_AGE
                ):
                    stats.skipped["odds older than 2 hours"] += 1
                    continue
            view = model.view(game.id, home.line, market, now, h)
            if view is None:
                stats.skipped["no model inputs"] += 1
                continue
            home_odds, home_book = _best(priced_home, bettable_books)
            away_odds, away_book = _best(priced_away, bettable_books)
            session.add(
                ForwardPrediction(
                    model_version_id=model.version.id,
                    game_id=game.id,
                    horizon_hours=h,
                    snapshot_at=now,
                    home_line=home.line,
                    home_cover_probability=view.home_cover,
                    push_probability=view.push,
                    market_home_cover=priced_home.consensus.consensus_no_vig_probability,
                    books=priced_home.consensus.books_reporting,
                    best_home_odds=home_odds,
                    best_home_book=home_book,
                    best_away_odds=away_odds,
                    best_away_book=away_book,
                    expected_margin=view.expected_margin,
                    features=view.features,
                    # Everything used (odds, results, injuries) was observed by now.
                    inputs_as_of=now,
                    market=model.market,
                )
            )
            stats.written += 1
    session.flush()
    return stats


# --------------------------------------------------------------------------- scoring


@dataclass
class Bets:
    min_edge: float
    bets: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    units: float = 0.0

    @property
    def roi(self) -> float | None:
        return self.units / self.bets if self.bets else None


@dataclass
class HorizonScore:
    model: str
    horizon_hours: int
    games: int = 0
    decided: int = 0
    model_log_loss: float | None = None
    market_log_loss: float | None = None
    paired_diff: float | None = None
    paired_se: float | None = None
    price_clv: list[float] = field(default_factory=list)
    points_vs_close: list[float] = field(default_factory=list)
    bets: list[Bets] = field(default_factory=list)

    @property
    def z(self) -> float | None:
        if self.paired_diff is None or not self.paired_se:
            return None
        return self.paired_diff / self.paired_se


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


_CLV_CACHE: dict[tuple[int, str, float | None, float], tuple[float | None, float | None]] = {}
"""(game, side, line, price) -> (price CLV, points vs close). Only finished games are
scored, and a finished game's closing state (as of kickoff) never changes."""


@dataclass(frozen=True)
class ScoredRow:
    """One forward snapshot on a finished game, scored. The side is the one the
    model favors against the market (edge >= 0: home)."""

    model: str
    horizon_hours: int
    game_id: int
    sport: str
    market: Market
    commence_time: datetime
    home_line: float
    model_home_cover: float
    market_home_cover: float
    cover_margin: float
    """Home margin plus the home line (a moneyline: the margin): > 0 home won
    the bet, 0 push."""
    side: Selection
    side_odds: float | None
    """Best bettable price for the side at the snapshot (None: no bettable book)."""
    price_clv: float | None
    points_vs_close: float | None

    @property
    def edge(self) -> float:
        return self.model_home_cover - self.market_home_cover

    @property
    def side_probability(self) -> float:
        """The model's P(side covers | no push)."""
        p = self.model_home_cover
        return p if self.side is Selection.HOME else 1 - p

    @property
    def side_market_probability(self) -> float:
        q = self.market_home_cover
        return q if self.side is Selection.HOME else 1 - q

    @property
    def won(self) -> bool | None:
        """None for a push."""
        if self.cover_margin == 0:
            return None
        return (self.cover_margin > 0) == (self.side is Selection.HOME)


def _side(fp: ForwardPrediction) -> tuple[Selection, float | None]:
    """The side the model favors against the market, and its best bettable price."""
    side = Selection.HOME if fp.home_cover_probability >= fp.market_home_cover else Selection.AWAY
    return side, fp.best_home_odds if side is Selection.HOME else fp.best_away_odds


def _clv(
    session: Session, fp: ForwardPrediction, closes: dict[tuple[int, Market], ClosingSnapshot]
) -> tuple[float | None, float | None]:
    """(price CLV, points vs close) of the snapshot's side, replayed from the odds
    history (about a second per game; ``closes`` shares one replay per game)."""
    side, odds = _side(fp)
    if odds is None:
        return None, None
    market = Market(fp.market)
    line: float | None = None
    if market is Market.SPREAD:
        line = fp.home_line if side is Selection.HOME else -fp.home_line
    key = (fp.game_id, f"{market}:{side}", line, odds)
    if key not in _CLV_CACHE:
        close = closes.get((fp.game_id, market))
        if close is None:
            close = closes[(fp.game_id, market)] = closing_snapshot(session, fp.game_id, market)
        _CLV_CACHE[key] = (close.price_clv(side, line, odds), close.points_gained(side, line))
    return _CLV_CACHE[key]


def score_finished(session: Session) -> int:
    """Store the closing-line value of every forward snapshot whose game is final
    and not yet scored. Returns the number stored."""
    pending = session.scalars(
        select(ForwardPrediction)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .outerjoin(ForwardScore, ForwardScore.forward_prediction_id == ForwardPrediction.id)
        .where(Game.status == GameStatus.FINAL, ForwardScore.forward_prediction_id.is_(None))
        .order_by(ForwardPrediction.game_id)
    ).all()
    closes: dict[tuple[int, Market], ClosingSnapshot] = {}
    for fp in pending:
        price_clv, points = _clv(session, fp, closes)
        session.add(
            ForwardScore(forward_prediction_id=fp.id, price_clv=price_clv, points_vs_close=points)
        )
    session.flush()
    return len(pending)


def scored_rows(
    session: Session, sport: Sport | None = None, *, model: str | None = None
) -> list[ScoredRow]:
    """Every forward snapshot on a finished game, oldest kickoff first, with its
    result and closing-line value (stored by ``score_finished``; replayed, without
    writing, for any snapshot not stored yet). ``model`` is "name version"."""
    query = (
        select(ForwardPrediction, Game, ModelVersion, ForwardScore)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .join(ModelVersion, ModelVersion.id == ForwardPrediction.model_version_id)
        .outerjoin(ForwardScore, ForwardScore.forward_prediction_id == ForwardPrediction.id)
        .where(Game.status == GameStatus.FINAL)
        .order_by(ModelVersion.name, ForwardPrediction.horizon_hours, Game.commence_time)
    )
    if sport is not None:
        query = query.where(Game.sport == sport)
    closes: dict[tuple[int, Market], ClosingSnapshot] = {}
    out = []
    for fp, game, mv, stored in session.execute(query).all():
        name = f"{mv.name} {mv.version}"
        if model is not None and name != model:
            continue
        assert game.home_score is not None and game.away_score is not None
        side, odds = _side(fp)
        if stored is not None:
            price_clv, points = stored.price_clv, stored.points_vs_close
        else:
            price_clv, points = _clv(session, fp, closes)
        out.append(
            ScoredRow(
                model=name,
                horizon_hours=fp.horizon_hours,
                game_id=game.id,
                sport=game.sport,
                market=Market(fp.market),
                commence_time=game.commence_time,
                home_line=fp.home_line,
                model_home_cover=fp.home_cover_probability,
                market_home_cover=fp.market_home_cover,
                cover_margin=game.home_score
                - game.away_score
                + (fp.home_line if fp.market == Market.SPREAD else 0.0),
                side=side,
                side_odds=odds,
                price_clv=price_clv,
                points_vs_close=points,
            )
        )
    return out


def forward_report(session: Session, sport: Sport | None = None) -> list[HorizonScore]:
    groups: dict[tuple[str, int], list[ScoredRow]] = defaultdict(list)
    for row in scored_rows(session, sport):
        groups[(row.model, row.horizon_hours)].append(row)
    out = []
    for (name, h), rows in groups.items():
        score = HorizonScore(name, h, games=len(rows), bets=[Bets(e) for e in EDGE_THRESHOLDS])
        diffs, model_ll, market_ll = [], [], []
        for r in rows:
            if r.cover_margin != 0:
                y = int(r.cover_margin > 0)
                a, b = _ll(r.model_home_cover, y), _ll(r.market_home_cover, y)
                model_ll.append(a)
                market_ll.append(b)
                diffs.append(a - b)
            if r.side_odds is None:
                continue
            if r.price_clv is not None:
                score.price_clv.append(r.price_clv)
            if r.points_vs_close is not None:
                score.points_vs_close.append(r.points_vs_close)
            for bet in score.bets:
                if abs(r.edge) < bet.min_edge:
                    continue
                bet.bets += 1
                if r.won is None:
                    bet.pushes += 1
                elif r.won:
                    bet.wins += 1
                    bet.units += bm.american_to_decimal(r.side_odds) - 1.0
                else:
                    bet.losses += 1
                    bet.units -= 1.0
        score.decided = len(diffs)
        if diffs:
            n = len(diffs)
            mean = sum(diffs) / n
            score.model_log_loss = sum(model_ll) / n
            score.market_log_loss = sum(market_ll) / n
            score.paired_diff = mean
            if n > 1:
                var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
                score.paired_se = math.sqrt(var / n)
        out.append(score)
    return out


def forward_counts(session: Session) -> list[tuple[str, int, int]]:
    """(model, snapshots, of them on finished games)."""
    rows = session.execute(
        select(
            ModelVersion.name + " " + ModelVersion.version,
            func.count(ForwardPrediction.id),
            func.sum(func.iif(Game.status == GameStatus.FINAL, 1, 0)),
        )
        .join(ForwardPrediction, ForwardPrediction.model_version_id == ModelVersion.id)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .group_by(ModelVersion.id)
    ).all()
    return [(str(a), int(b), int(c or 0)) for a, b, c in rows]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def forward_dashboard(
    session: Session, sport: Sport | None = None, *, recent: int = 30
) -> dict[str, Any]:
    """Everything the forward-test view needs, as plain data."""
    counts = {name: (total, finished) for name, total, finished in forward_counts(session)}
    scores = []
    for sc in forward_report(session, sport):
        scores.append(
            {
                "model": sc.model,
                "horizon_hours": sc.horizon_hours,
                "games": sc.games,
                "decided": sc.decided,
                "model_log_loss": sc.model_log_loss,
                "market_log_loss": sc.market_log_loss,
                "paired_diff": sc.paired_diff,
                "paired_se": sc.paired_se,
                # A z from a handful of games is noise (its SE is tiny); held back until
                # MIN_DECIDED_FOR_Z games are decided.
                "z": sc.z if sc.decided >= MIN_DECIDED_FOR_Z else None,
                "min_decided_for_z": MIN_DECIDED_FOR_Z,
                "price_clv": _mean(sc.price_clv),
                "price_clv_n": len(sc.price_clv),
                "points_vs_close": _mean(sc.points_vs_close),
                "points_n": len(sc.points_vs_close),
                "bets": [
                    {
                        "min_edge": b.min_edge,
                        "bets": b.bets,
                        "wins": b.wins,
                        "losses": b.losses,
                        "pushes": b.pushes,
                        "units": b.units,
                        "roi": b.roi,
                    }
                    for b in sc.bets
                ],
            }
        )
    home, away = aliased(Team), aliased(Team)
    query = (
        select(ForwardPrediction, ModelVersion.name, Game, home.name, away.name)
        .join(ModelVersion, ModelVersion.id == ForwardPrediction.model_version_id)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .join(home, home.id == Game.home_team_id)
        .join(away, away.id == Game.away_team_id)
        .order_by(ForwardPrediction.snapshot_at.desc(), ForwardPrediction.id.desc())
        .limit(recent)
    )
    if sport is not None:
        query = query.where(Game.sport == sport)
    rows = []
    for fp, name, game, home_name, away_name in session.execute(query).all():
        covered = None
        if game.status == GameStatus.FINAL and game.home_score is not None:
            assert game.away_score is not None
            line = fp.home_line if fp.market == Market.SPREAD else 0.0
            margin = game.home_score - game.away_score + line
            covered = "push" if margin == 0 else ("home" if margin > 0 else "away")
        rows.append(
            {
                "model": name,
                "sport": game.sport,
                "matchup": f"{away_name} @ {home_name}",
                "commence_time": game.commence_time.isoformat(),
                "horizon_hours": fp.horizon_hours,
                "market": fp.market,
                "snapshot_at": fp.snapshot_at.isoformat(),
                "home_line": fp.home_line,
                "model_home_cover": fp.home_cover_probability,
                "market_home_cover": fp.market_home_cover,
                "edge": fp.home_cover_probability - fp.market_home_cover,
                "result": covered,
            }
        )
    return {
        "models": [
            {"model": name, "snapshots": total, "finished": finished}
            for name, (total, finished) in sorted(counts.items())
        ],
        "scores": scores,
        "recent": rows,
        "note": "Forward-tested models are DEVELOPMENT: these are records, never bets. "
        "A negative log-loss difference beats the market; |z| under about 2 is not an edge.",
    }
