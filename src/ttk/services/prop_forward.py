"""Forward test of the prop models (NFL: research/prop_model, prop_matchup; NBA:
research/nba_props): record each one's chance for every prop main line in each props
pull of its sport before kickoff, and score it once the players' stats are in.

A record per (model version, pull, player, prop): the main line (most books on both
sides), the model's P(over) and mean, the market's median no-vig P(over) (two or more
books quoting both sides) and the best prices. Pulls are taken 24 h and 1 h before
kickoff, so a prop is recorded twice; the 1 h line is the nearest thing to a close,
and the report says whether the line moved toward the model in between.

Scoring: the player's stat once his game is final and its stats are imported. A
player with no stat line is left out (inactive, or played without one), as is a
push. Log loss of the model and of the market, paired; a z is shown once
``MIN_FOR_Z`` props are decided. DEVELOPMENT: never a recommendation.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import (
    Game,
    ModelVersion,
    NflPlayerGameStat,
    PlayerGameStat,
    PropPrediction,
    PropPull,
    PropQuote,
    Sportsbook,
    Team,
    utcnow,
)
from ttk.domain import GameStatus, Sport
from ttk.research import nba_props as nb
from ttk.research import prop_matchup as mu
from ttk.research import prop_model as pm
from ttk.research.moneyline import no_vig
from ttk.services import player_props as pp
from ttk.services.forward_test import register

MODEL_NAME = "nfl-prop-usage"
MODEL_VERSION = "0.1.0"
MATCHUP_NAME = "nfl-prop-matchup"
MATCHUP_VERSION = "0.2.0"
NBA_NAME = "nba-prop-usage"
NBA_VERSION = "0.1.0"
ANCHORED_NAME = "nba-prop-anchored"
ANCHORED_VERSION = "0.1.0"
SHRUNK_VERSION = "0.3.0"
KINDS = frozenset({"nfl_prop_usage", "nfl_prop_matchup", nb.KIND, nb.ANCHORED_KIND})
MIN_BOOKS = 2
MIN_FOR_Z = 100

PropModel = (
    pm.FrozenPropModel
    | mu.FrozenMatchupModel
    | nb.FrozenNbaPropModel
    | nb.FrozenAnchoredNbaPropModel
)


def sport_of(artifact: Mapping[str, Any]) -> Sport:
    return Sport(artifact.get("sport") or "NFL")


def load_model(artifact: Mapping[str, Any], games: Sequence[Any]) -> PropModel:
    """``games``: the sport's player games (``load_games``)."""
    if artifact.get("kind") == nb.ANCHORED_KIND:
        return nb.FrozenAnchoredNbaPropModel(artifact, games)
    if artifact.get("kind") == nb.KIND:
        return nb.FrozenNbaPropModel(artifact, games)
    if artifact.get("kind") == "nfl_prop_matchup":
        return mu.FrozenMatchupModel(artifact, games)
    return pm.FrozenPropModel(artifact, games)


def load_games(session: Session, sport: Sport) -> Sequence[Any]:
    if sport == Sport.NBA:
        return nb.load_player_games(session, since_season=nb.VALIDATE[0] - 1)
    return pm.load_player_games(session)


def model_view(
    model: PropModel,
    player_id: str,
    market: str,
    line: float,
    game: Game,
    opponent_id: int,
    market_over: float | None = None,
) -> pm.PropView | mu.MatchupView | nb.NbaView | None:
    """``market_over``: the market's no-vig P(over) (an anchored model needs it)."""
    if isinstance(model, nb.FrozenAnchoredNbaPropModel):
        return model.view(
            player_id,
            market,
            line,
            game.commence_time,
            game.season or 0,
            opponent_id,
            market_over,
        )
    if isinstance(model, (mu.FrozenMatchupModel, nb.FrozenNbaPropModel)):
        return model.view(
            player_id, market, line, game.commence_time, game.season or 0, opponent_id
        )
    return model.view(player_id, market, line, game.commence_time, game.season or 0)


def freeze_and_register(session: Session) -> tuple[ModelVersion, pm.PropBacktest]:
    result = pm.backtest(pm.load_player_games(session))
    row = register(
        session,
        name=MODEL_NAME,
        version=MODEL_VERSION,
        sport=Sport.NFL,
        algorithm="weighted recent usage; nbinom / gamma",
        features=list(pm.STATS),
        artifact=pm.freeze(result),
        training_window=f"{pm.TRAIN[0]}-{pm.TRAIN[1]}",
        validation_window=f"{pm.VALIDATE[0]}-{pm.VALIDATE[1]}",
        log_loss=None,
        brier=None,
    )
    return row, result


def freeze_matchup_and_register(session: Session) -> tuple[ModelVersion, mu.MatchupBacktest]:
    """v0.2: v0.1's usage settings (TRAIN), plus the opposing defense (TRAIN)."""
    games = pm.load_player_games(session)
    usage = pm.backtest(games).params
    result = mu.backtest(games, usage)
    row = register(
        session,
        name=MATCHUP_NAME,
        version=MATCHUP_VERSION,
        sport=Sport.NFL,
        algorithm="recent usage x opposing defense; nbinom / gamma",
        features=[*pm.STATS, "defense_allowed_by_position"],
        artifact=mu.freeze(result),
        training_window=f"{pm.TRAIN[0]}-{pm.TRAIN[1]}",
        validation_window=f"{pm.VALIDATE[0]}-{pm.VALIDATE[1]}",
        log_loss=None,
        brier=None,
    )
    return row, result


def freeze_shrunk_and_register(session: Session) -> tuple[ModelVersion, mu.MatchupBacktest]:
    """v0.3: the registered v0.2, plus each player's usage shrunk toward his position's
    average (shrink and beta per stat chosen on TRAIN; validated against v0.2)."""
    previous = session.scalars(
        select(ModelVersion).where(
            ModelVersion.name == MATCHUP_NAME, ModelVersion.version == MATCHUP_VERSION
        )
    ).first()
    if previous is None or not previous.artifact:
        raise ValueError(f"register {MATCHUP_NAME} {MATCHUP_VERSION} first (--model matchup)")
    result = mu.backtest_v03(pm.load_player_games(session), mu.from_artifact(previous.artifact))
    row = register(
        session,
        name=MATCHUP_NAME,
        version=SHRUNK_VERSION,
        sport=Sport.NFL,
        algorithm="recent usage shrunk to position x opposing defense; nbinom / gamma",
        features=[*pm.STATS, "defense_allowed_by_position", "position_prior"],
        artifact=mu.freeze(result),
        training_window=f"{pm.TRAIN[0]}-{pm.TRAIN[1]}",
        validation_window=f"{pm.VALIDATE[0]}-{pm.VALIDATE[1]}",
        log_loss=None,
        brier=None,
    )
    return row, result


def freeze_nba_and_register(session: Session) -> tuple[ModelVersion, nb.NbaBacktest]:
    """NBA v0.1: usage x the opponent, shrunk to a minutes-based role (TRAIN)."""
    result = nb.backtest(nb.load_player_games(session))
    row = register(
        session,
        name=NBA_NAME,
        version=NBA_VERSION,
        sport=Sport.NBA,
        algorithm="usage shrunk to role x opponent allowed; negative binomial (var = phi x mean)",
        features=[*nb.STATS, "minutes", "opponent_allowed"],
        artifact=nb.freeze(result),
        training_window=f"{nb.TRAIN[0]}-{nb.TRAIN[1]}",
        validation_window=f"{nb.VALIDATE[0]}-{nb.VALIDATE[1]}",
        log_loss=None,
        brier=None,
    )
    return row, result


@dataclass
class AnchoredFit:
    weight: float
    weight_se: float
    props: int
    window: tuple[str, str]
    check: dict[str, float]
    """Time split inside the window: weight from the first three quarters of the
    days, log loss on the last quarter for the anchored model, v0.1 and the market."""


def fit_anchored(session: Session, base: ModelVersion, cutoff: datetime) -> AnchoredFit:
    """The weight from ``base``'s forward-test props decided on games before
    ``cutoff``. Those props stay ``base``'s test; the anchored model is judged only
    on games after its freeze."""
    rows = sorted(
        (r for r in scored(session, base.id) if r.prediction.created_at < cutoff),
        key=lambda r: r.prediction.game_id,
    )
    if len(rows) < MIN_FOR_Z:
        raise ValueError(f"{len(rows)} decided props for {base.name}: need {MIN_FOR_Z}")
    games = {
        g.id: g.commence_time
        for g in session.scalars(
            select(Game).where(Game.id.in_({r.prediction.game_id for r in rows}))
        )
    }
    if any(t >= cutoff for t in games.values()):
        raise ValueError("a decided prop's game is after the cutoff")
    triples = [(r.prediction.market_over, r.prediction.p_over, r.over) for r in rows]
    weight, se = nb.fit_weight(triples)
    days = sorted({games[r.prediction.game_id].date() for r in rows})
    split = days[max(1, (3 * len(days)) // 4) - 1]
    early = [
        t for r, t in zip(rows, triples, strict=True) if games[r.prediction.game_id].date() <= split
    ]
    late = [
        t for r, t in zip(rows, triples, strict=True) if games[r.prediction.game_id].date() > split
    ]
    check: dict[str, float] = {}
    if early and late:
        w_early, _ = nb.fit_weight(early)
        check = {
            "weight_first_part": w_early,
            "props_last_part": float(len(late)),
            "anchored": statistics.fmean(_ll(nb.anchored(m, p, w_early), o) for m, p, o in late),
            "base": statistics.fmean(_ll(p, o) for _, p, o in late),
            "market": statistics.fmean(_ll(m, o) for m, _, o in late),
        }
    return AnchoredFit(weight, se, len(rows), (str(days[0]), str(days[-1])), check)


def freeze_anchored_and_register(
    session: Session, now: datetime | None = None
) -> tuple[ModelVersion, AnchoredFit]:
    now = now or utcnow()
    base = session.scalars(
        select(ModelVersion).where(
            ModelVersion.name == NBA_NAME, ModelVersion.version == NBA_VERSION
        )
    ).first()
    if base is None or not base.artifact:
        raise ValueError(f"register {NBA_NAME} {NBA_VERSION} first (--model nba)")
    fit = fit_anchored(session, base, now)
    row = register(
        session,
        name=ANCHORED_NAME,
        version=ANCHORED_VERSION,
        sport=Sport.NBA,
        algorithm="logit(market) + weight x (logit(nba-prop-usage) - logit(market))",
        features=[f"{NBA_NAME} {NBA_VERSION}", "market no-vig P(over)"],
        artifact={
            "artifact_version": nb.ARTIFACT_VERSION,
            "kind": nb.ANCHORED_KIND,
            "market": "PLAYER_PROP",
            "sport": "NBA",
            "base_version": f"{NBA_NAME} {NBA_VERSION}",
            "base": base.artifact,
            "weight": fit.weight,
            "weight_se": fit.weight_se,
            "fit": {"props": fit.props, "window": list(fit.window), "check": fit.check},
        },
        training_window=f"{NBA_NAME} forward props {fit.window[0]}..{fit.window[1]}",
        validation_window="forward test from the freeze",
        log_loss=None,
        brier=None,
    )
    return row, fit


def prop_versions(session: Session) -> list[ModelVersion]:
    return [
        row
        for row in session.scalars(
            select(ModelVersion).where(ModelVersion.artifact.is_not(None)).order_by(ModelVersion.id)
        )
        if (row.artifact or {}).get("kind") in KINDS
    ]


def _due(session: Session, version: ModelVersion, now: datetime) -> list[tuple[PropPull, Game]]:
    taken = select(PropPrediction.pull_id).where(PropPrediction.model_version_id == version.id)
    return list(
        session.execute(
            select(PropPull, Game)
            .join(Game, Game.id == PropPull.game_id)
            .where(
                Game.sport == sport_of(version.artifact or {}),
                Game.status != GameStatus.DUPLICATE,  # set aside (services/repair)
                Game.commence_time > now,
                PropPull.id.not_in(taken),
            )
            .order_by(PropPull.pulled_at)
        ).tuples()
    )


def snapshot_pulls(
    session: Session,
    *,
    versions: Sequence[ModelVersion] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Record every registered prop model on every pull of a game that hasn't started
    and has no records from it yet. Returns log lines."""
    now = now or utcnow()
    lines: list[str] = []
    for version in versions if versions is not None else prop_versions(session):
        due = _due(session, version, now)
        if not due:
            continue
        sport = sport_of(version.artifact or {})
        model = load_model(version.artifact or {}, load_games(session, sport))
        teams = {t.id: t for t in session.scalars(select(Team).where(Team.sport == sport))}
        history = pp._history(session, sport, list(teams), now + timedelta(days=1))
        books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
        for pull, game in due:
            # Written once per pull, not row by row (forward_test.snapshot).
            with session.no_autoflush:
                written = _snapshot(session, version, model, history, books, pull, game)
            lines.append(
                f"prop model {version.name} {version.version}: game {game.id} "
                f"{pull.horizon_hours} h: {written} props recorded"
            )
        session.flush()
    return lines


def _snapshot(
    session: Session,
    version: ModelVersion,
    model: PropModel,
    history: pp._History,
    books: dict[int, str],
    pull: PropPull,
    game: Game,
) -> int:
    quotes = [
        (q, books[q.sportsbook_id])
        for q in session.scalars(select(PropQuote).where(PropQuote.pull_id == pull.id))
    ]
    written = 0
    seen: set[tuple[str, str]] = set()
    for (_, side, market), prop in pp._props(quotes).items():
        if market not in model.dists:
            continue
        line = pp._main_line(prop.points)
        sides = prop.points[line]
        no_vigs = [
            p
            for b in sides.values()
            if len(b) == 2 and (p := no_vig(b["Over"], b["Under"])) is not None
        ]
        if len(no_vigs) < MIN_BOOKS:
            continue
        team_id = game.home_team_id if side == "HOME" else game.away_team_id
        opponent_id = game.away_team_id if side == "HOME" else game.home_team_id
        player_id = pp._match(history, team_id, prop.name)
        if player_id is None or (player_id, market) in seen:
            continue
        view = model_view(
            model, player_id, market, line, game, opponent_id, statistics.median(no_vigs)
        )
        if view is None:
            continue
        seen.add((player_id, market))
        over, under = pp._best(sides, "Over"), pp._best(sides, "Under")
        session.add(
            PropPrediction(
                model_version_id=version.id,
                game_id=game.id,
                pull_id=pull.id,
                horizon_hours=pull.horizon_hours,
                player=prop.name,
                player_id=player_id,
                market=market,
                line=line,
                p_over=view.p_over,
                mean=view.mean,
                games=view.games,
                market_over=statistics.median(no_vigs),
                books=len(no_vigs),
                best_over=over.american if over else None,
                best_over_book=over.book if over else None,
                best_under=under.american if under else None,
                best_under_book=under.book if under else None,
            )
        )
        written += 1
    return written


@dataclass(frozen=True)
class ScoredProp:
    prediction: PropPrediction
    actual: float
    over: bool


def scored(session: Session, version_id: int) -> list[ScoredProp]:
    rows = session.execute(
        select(PropPrediction, Game)
        .join(Game, Game.id == PropPrediction.game_id)
        .where(PropPrediction.model_version_id == version_id, Game.status == GameStatus.FINAL)
    ).all()
    if not rows:
        return []
    game_ids = {g.id for _, g in rows}
    nba = rows[0][1].sport == Sport.NBA
    fns: Mapping[str, Any] = pp.NBA_STATS if nba else pm.STATS
    if nba:
        # A player who didn't play: the prop is void (left out).
        stats: dict[tuple[int, str], Any] = {
            (r.game_id, r.player_id): r
            for r in session.scalars(
                select(PlayerGameStat).where(
                    PlayerGameStat.game_id.in_(game_ids), PlayerGameStat.played.is_(True)
                )
            )
        }
    else:
        stats = {
            (r.game_id, r.player_id): r
            for r in session.scalars(
                select(NflPlayerGameStat).where(NflPlayerGameStat.game_id.in_(game_ids))
            )
        }
    out = []
    for p, _ in rows:
        stat = stats.get((p.game_id, p.player_id))
        fn = fns.get(p.market)
        value = fn(stat) if stat is not None and fn is not None else None
        if value is None:
            continue  # stats not in yet, or no stat line (inactive / unrecorded)
        actual = float(value)
        if actual == p.line:
            continue
        out.append(ScoredProp(p, actual, actual > p.line))
    return out


def _ll(p: float, over: bool) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if over else 1 - p)


@dataclass
class PropScore:
    label: str
    decided: int
    model_log_loss: float | None
    market_log_loss: float | None
    paired_diff: float | None
    """Model minus market log loss (negative = the model is better)."""
    z: float | None
    """Shown from MIN_FOR_Z decided props."""


@dataclass
class PropReport:
    model: str
    recorded: int
    decided: int
    scores: list[PropScore] = field(default_factory=list)
    moved_toward: int = 0
    moved_away: int = 0
    """Props recorded at 24 h and 1 h whose line moved toward / away from the model's
    lean at 24 h (a model that knows something sees lines move its way)."""


def _score(label: str, rows: Sequence[ScoredProp]) -> PropScore:
    if not rows:
        return PropScore(label, 0, None, None, None, None)
    d = [_ll(r.prediction.p_over, r.over) - _ll(r.prediction.market_over, r.over) for r in rows]
    mean = statistics.fmean(d)
    z = None
    if len(d) >= MIN_FOR_Z:
        sd = statistics.stdev(d)
        z = mean / (sd / math.sqrt(len(d))) if sd > 0 else None
    return PropScore(
        label,
        len(rows),
        statistics.fmean(_ll(r.prediction.p_over, r.over) for r in rows),
        statistics.fmean(_ll(r.prediction.market_over, r.over) for r in rows),
        mean,
        z,
    )


def prop_report(session: Session, version: ModelVersion) -> PropReport:
    recorded = (
        session.scalar(select(func.count()).where(PropPrediction.model_version_id == version.id))
        or 0
    )
    rows = scored(session, version.id)
    report = PropReport(f"{version.name} {version.version}", int(recorded), len(rows))
    report.scores.append(_score("all", rows))
    for h in sorted({r.prediction.horizon_hours for r in rows}, reverse=True):
        report.scores.append(_score(f"{h} h", [r for r in rows if r.prediction.horizon_hours == h]))
    for market in sorted({r.prediction.market for r in rows}):
        report.scores.append(_score(market, [r for r in rows if r.prediction.market == market]))
    # Line movement, 24 h -> 1 h, against the model's lean at 24 h.
    by_prop: dict[tuple[int, str, str], dict[int, PropPrediction]] = {}
    for p in session.scalars(
        select(PropPrediction).where(PropPrediction.model_version_id == version.id)
    ):
        by_prop.setdefault((p.game_id, p.player_id, p.market), {})[p.horizon_hours] = p
    for snaps in by_prop.values():
        early, late = snaps.get(24), snaps.get(1)
        if early is None or late is None or late.line == early.line:
            continue
        lean = early.p_over - early.market_over
        if lean == 0:
            continue
        toward = (late.line > early.line) == (lean > 0)
        report.moved_toward += toward
        report.moved_away += not toward
    return report


def snapshot_due(session_factory: sessionmaker[Session]) -> list[str]:
    """For the collector: record prop models on new pulls, in their own transaction."""
    with session_factory() as session:
        lines = snapshot_pulls(session)
        session.commit()
    return lines
