"""Daily report: how the forward-tested models' picks did, how often each team's
games went over the closing total, and how often player props went over.

Built once a morning (``ttk daily-report``, a scheduled task at 9:00 Eastern) for
"yesterday" (an Eastern calendar day of game starts) and the season so far, and
saved as JSON under ``data/reports/daily`` for the app's Daily report page.

- **Models:** each model's record on the side it favored against the market (a
  game model at its last snapshot before the game, a prop model on the over or
  under), and whether it beats the market in log loss (forward reports' z).
  DEVELOPMENT models: records, never recommendations.
- **Teams:** over/under against the closing total. The close is the stored provider
  close where there is one (ESPN/DraftKings for college football, nflverse for the
  NFL), else the median main total across books at kickoff from the odds history,
  cached in ``closing_totals.json`` (a finished game's close never changes).
- **Player props:** main lines with 2+ books quoting both sides, as recorded by each
  sport's first prop model at its last pull before the game (1 h, else 24 h); a
  push is left out, as is a player who didn't play.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, ModelVersion, ReportedLine, Team, utcnow
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.services.forward_test import forward_report, scored_rows
from ttk.services.line_history import closing_snapshot
from ttk.services.prop_forward import ScoredProp, prop_report, prop_versions, scored

EASTERN = ZoneInfo("America/New_York")
REPORT_DIR = Path("reports") / "daily"
"""Under the data directory."""
CLOSE_PROVIDERS = {Sport.CFB: "espn:draftkings", Sport.NFL: "nflverse"}
LAST_N = 10
MIN_PLAYER_PROPS = 8
"""A player is listed among the most-over/most-under from this many decided props."""
TOP_PLAYERS = 10


def eastern_day(t: datetime) -> date:
    return t.astimezone(EASTERN).date()


@dataclass
class Record:
    wins: int = 0
    losses: int = 0
    pushes: int = 0

    def add(self, won: bool | None) -> None:
        if won is None:
            self.pushes += 1
        elif won:
            self.wins += 1
        else:
            self.losses += 1

    @property
    def hit_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None


@dataclass
class ModelLine:
    model: str
    sport: str
    market: str
    horizon: str
    yesterday: Record
    season: Record
    log_loss_z: float | None
    """Model minus market log loss, paired z (negative = the model is better)."""
    verdict: str


@dataclass
class TeamLine:
    team: str
    games: int
    overs: int
    unders: int
    pushes: int
    over_rate: float | None
    last10: str
    """Most recent last: O, U or P per game."""
    last10_overs: int


@dataclass
class PropMarketLine:
    market: str
    yesterday_props: int
    yesterday_over_rate: float | None
    season_props: int
    season_over_rate: float | None


@dataclass
class PlayerLine:
    player: str
    props: int
    overs: int
    over_rate: float


@dataclass
class SportProps:
    sport: str
    markets: list[PropMarketLine] = field(default_factory=list)
    most_over: list[PlayerLine] = field(default_factory=list)
    most_under: list[PlayerLine] = field(default_factory=list)


@dataclass
class SportTotals:
    sport: str
    season: int
    games: int
    over_rate: float | None
    yesterday_games: int
    yesterday_over_rate: float | None
    teams: list[TeamLine] = field(default_factory=list)


@dataclass
class DailyReport:
    day: str
    generated_at: str
    models: list[ModelLine]
    totals: list[SportTotals]
    props: list[SportProps]
    headline: str
    note: str = (
        "Every model is DEVELOPMENT (in testing): a record here is not a recommendation. "
        "A hit rate needs about 52.4% to beat a -110 price, and a few dozen picks prove nothing."
    )


MIN_GAMES = 30
"""As the app's model cards: a game model is judged from 30 decided games..."""
MIN_PROPS = 100
"""... a prop model from 100 decided props."""


def _verdict(z: float | None, decided: int, needed: int) -> str:
    if z is None or decided < needed:
        return f"too few to judge ({decided} of {needed})"
    if z <= -2:
        return "beating the market"
    if z >= 2:
        return "worse than the market"
    return "level with the market"


# ------------------------------------------------------------------ models


def _game_models(session: Session, day: date) -> list[ModelLine]:
    rows = scored_rows(session)
    z = {(h.model, h.horizon_hours): h.z for h in forward_report(session)}
    # A model's pick is its last snapshot before the game: the shortest horizon.
    shortest: dict[str, int] = {}
    for r in rows:
        shortest[r.model] = min(shortest.get(r.model, r.horizon_hours), r.horizon_hours)
    out: dict[str, ModelLine] = {}
    for r in rows:
        if r.horizon_hours != shortest[r.model]:
            continue
        line = out.get(r.model)
        if line is None:
            line = out[r.model] = ModelLine(
                r.model,
                r.sport,
                str(r.market),
                f"{r.horizon_hours} h",
                Record(),
                Record(),
                z.get((r.model, r.horizon_hours)),
                "",
            )
        line.season.add(r.won)
        if eastern_day(r.commence_time) == day:
            line.yesterday.add(r.won)
    for line in out.values():
        decided = line.season.wins + line.season.losses
        line.verdict = _verdict(line.log_loss_z, decided, MIN_GAMES)
    return list(out.values())


def _prop_models(session: Session, day: date) -> list[ModelLine]:
    out = []
    for version in prop_versions(session):
        report = prop_report(session, version)
        zz = report.scores[0].z if report.scores else None
        rows = last_pull(scored(session, version.id))
        line = ModelLine(
            f"{version.name} {version.version}",
            version.sport,
            "PLAYER_PROP",
            "last pull (1 h, else 24 h)",
            Record(),
            Record(),
            zz,
            _verdict(zz, report.decided, MIN_PROPS),
        )
        kickoffs = _kickoffs(session, {s.prediction.game_id for s in rows})
        for s in rows:
            pick_over = s.prediction.p_over >= s.prediction.market_over
            won = s.over == pick_over
            line.season.add(won)
            if eastern_day(kickoffs[s.prediction.game_id]) == day:
                line.yesterday.add(won)
        out.append(line)
    return out


def last_pull(rows: Iterable[ScoredProp]) -> list[ScoredProp]:
    """One record per prop: the last pull before the game (1 h over 24 h)."""
    latest: dict[tuple[int, str, str], ScoredProp] = {}
    for s in rows:
        p = s.prediction
        key = (p.game_id, p.player_id or p.player, p.market)
        if key not in latest or p.horizon_hours < latest[key].prediction.horizon_hours:
            latest[key] = s
    return list(latest.values())


def _kickoffs(session: Session, game_ids: Iterable[int]) -> dict[int, datetime]:
    ids = list(game_ids)
    if not ids:
        return {}
    return dict(session.execute(select(Game.id, Game.commence_time).where(Game.id.in_(ids))).all())


# ------------------------------------------------------------------ teams


def closing_totals(
    session: Session, games: Sequence[Game], cache_path: Path | None
) -> dict[int, float]:
    """Closing total per game id (games without one are absent)."""
    cache: dict[str, float] = {}
    if cache_path is not None and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    out: dict[int, float] = {}
    by_provider: dict[str, list[int]] = defaultdict(list)
    for g in games:
        provider = CLOSE_PROVIDERS.get(Sport(g.sport))
        if provider:
            by_provider[provider].append(g.id)
    for provider, ids in by_provider.items():
        for game_id, total in session.execute(
            select(ReportedLine.game_id, ReportedLine.total).where(
                ReportedLine.provider == provider,
                ReportedLine.game_id.in_(ids),
                ReportedLine.total.is_not(None),
            )
        ):
            if total is not None:
                out[game_id] = float(total)
    changed = False
    for g in games:
        if g.id in out:
            continue
        key = str(g.id)
        if key not in cache:
            close = closing_snapshot(session, g.id, Market.TOTAL).main_close(Selection.OVER)
            cache[key] = close if close is not None else float("nan")
            changed = True
        if cache[key] == cache[key]:  # not NaN
            out[g.id] = cache[key]
    if changed and cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return out


IN_SEASON = timedelta(days=30)
"""A sport is reported while it has a finished game this recent (not last season's)."""


def _not_preseason() -> Any:
    return or_(Game.season_type.is_(None), Game.season_type != "PRE")


def _season_games(session: Session, sport: Sport, before: datetime) -> list[Game]:
    latest = session.scalar(
        select(Game.season)
        .where(
            Game.sport == sport,
            Game.status == GameStatus.FINAL,
            Game.commence_time < before,
            Game.commence_time >= before - IN_SEASON,
            _not_preseason(),
        )
        .order_by(Game.commence_time.desc())
        .limit(1)
    )
    if latest is None:
        return []
    return list(
        session.scalars(
            select(Game)
            .where(
                Game.sport == sport,
                Game.season == latest,
                Game.status == GameStatus.FINAL,
                Game.commence_time < before,
                Game.home_score.is_not(None),
                _not_preseason(),
            )
            .order_by(Game.commence_time)
        )
    )


def _totals(session: Session, day: date, before: datetime, cache: Path | None) -> list[SportTotals]:
    out = []
    names = {t.id: t.name for t in session.scalars(select(Team))}
    for sport in (Sport.NFL, Sport.NBA, Sport.CFB, Sport.NCAAB):
        games = _season_games(session, sport, before)
        if not games:
            continue
        closes = closing_totals(session, games, cache)
        results: dict[int, list[str]] = defaultdict(list)
        all_marks, day_marks = [], []
        for g in games:
            close = closes.get(g.id)
            if close is None:
                continue
            assert g.home_score is not None and g.away_score is not None
            points = g.home_score + g.away_score
            mark = "O" if points > close else "U" if points < close else "P"
            all_marks.append(mark)
            if eastern_day(g.commence_time) == day:
                day_marks.append(mark)
            for team in (g.home_team_id, g.away_team_id):
                results[team].append(mark)
        teams = []
        for team, marks in results.items():
            o, u, p = marks.count("O"), marks.count("U"), marks.count("P")
            last = marks[-LAST_N:]
            teams.append(
                TeamLine(
                    names.get(team, str(team)),
                    len(marks),
                    o,
                    u,
                    p,
                    o / (o + u) if o + u else None,
                    "".join(last),
                    last.count("O"),
                )
            )
        if not all_marks:
            continue  # no closing totals stored for this sport's games
        teams.sort(key=lambda t: (-(t.over_rate or 0), -t.games, t.team))
        out.append(
            SportTotals(
                str(sport),
                games[-1].season or 0,
                len(all_marks),
                _rate(all_marks),
                len(day_marks),
                _rate(day_marks),
                teams,
            )
        )
    return out


def _rate(marks: Sequence[str]) -> float | None:
    o, u = marks.count("O"), marks.count("U")
    return o / (o + u) if o + u else None


# ------------------------------------------------------------------ props


def _props(session: Session, day: date) -> list[SportProps]:
    first: dict[str, ModelVersion] = {}
    for v in prop_versions(session):  # oldest first: the most complete record
        first.setdefault(v.sport, v)
    out = []
    for sport, version in sorted(first.items()):
        latest = {
            (s.prediction.game_id, s.prediction.player_id or "", s.prediction.market): s
            for s in last_pull(scored(session, version.id))
        }
        kickoffs = _kickoffs(session, {k[0] for k in latest})
        by_market: dict[str, list[bool]] = defaultdict(list)
        by_market_day: dict[str, list[bool]] = defaultdict(list)
        by_player: dict[str, list[bool]] = defaultdict(list)
        for (game_id, _, market), s in latest.items():
            by_market[market].append(s.over)
            by_player[s.prediction.player].append(s.over)
            if eastern_day(kickoffs[game_id]) == day:
                by_market_day[market].append(s.over)
        sp = SportProps(sport)
        for market in sorted(by_market, key=lambda m: -len(by_market[m])):
            season, yday = by_market[market], by_market_day.get(market, [])
            sp.markets.append(
                PropMarketLine(
                    market.removeprefix("player_").replace("_", " "),
                    len(yday),
                    sum(yday) / len(yday) if yday else None,
                    len(season),
                    sum(season) / len(season),
                )
            )
        players = [
            PlayerLine(name, len(v), sum(v), sum(v) / len(v))
            for name, v in by_player.items()
            if len(v) >= MIN_PLAYER_PROPS
        ]
        sp.most_over = sorted(players, key=lambda p: (-p.over_rate, -p.props))[:TOP_PLAYERS]
        sp.most_under = sorted(players, key=lambda p: (p.over_rate, -p.props))[:TOP_PLAYERS]
        out.append(sp)
    return out


# ------------------------------------------------------------------ report


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def headline(
    models: Sequence[ModelLine], totals: Sequence[SportTotals], props: Sequence[SportProps]
) -> str:
    games, prop_picks = Record(), Record()
    for m in models:
        r = prop_picks if m.market == "PLAYER_PROP" else games
        r.wins += m.yesterday.wins
        r.losses += m.yesterday.losses
        r.pushes += m.yesterday.pushes
    parts = [f"Game-model picks yesterday {games.wins}-{games.losses} ({_pct(games.hit_rate)})"]
    if prop_picks.wins + prop_picks.losses:
        parts.append(
            f"prop-model picks {prop_picks.wins}-{prop_picks.losses} ({_pct(prop_picks.hit_rate)})"
        )
    overs = [
        f"{t.sport} {_pct(t.yesterday_over_rate)} of {t.yesterday_games}"
        for t in totals
        if t.yesterday_games
    ]
    if overs:
        parts.append("games over: " + ", ".join(overs))
    for sp in props:
        n = sum(m.yesterday_props for m in sp.markets)
        if n:
            o = sum((m.yesterday_over_rate or 0) * m.yesterday_props for m in sp.markets)
            parts.append(f"{sp.sport} props over {_pct(o / n)} of {n}")
    return "; ".join(parts) + "."


def build_report(
    session: Session,
    day: date | None = None,
    *,
    cache: Path | None = None,
    now: datetime | None = None,
) -> DailyReport:
    now = now or utcnow()
    day = day or (now.astimezone(EASTERN).date() - timedelta(days=1))
    before = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=EASTERN)
    models = sorted(
        _game_models(session, day) + _prop_models(session, day),
        key=lambda m: (m.sport, m.market, m.model),
    )
    totals = _totals(session, day, before, cache)
    props = _props(session, day)
    return DailyReport(
        day.isoformat(),
        now.isoformat(timespec="seconds"),
        models,
        totals,
        props,
        headline(models, totals, props),
    )


def save_report(report: DailyReport, data_dir: Path) -> Path:
    path = data_dir / REPORT_DIR / f"{report.day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(report), indent=1), encoding="utf-8")
    return path


def to_dict(report: DailyReport) -> dict[str, Any]:
    d = asdict(report)
    for m, src in zip(d["models"], report.models, strict=True):
        for k in ("yesterday", "season"):
            m[k]["hit_rate"] = getattr(src, k).hit_rate
    return d


def load_report(data_dir: Path, day: date | None = None) -> dict[str, Any] | None:
    folder = data_dir / REPORT_DIR
    if day is not None:
        path = folder / f"{day.isoformat()}.json"
    else:
        files = sorted(folder.glob("????-??-??.json")) if folder.exists() else []
        if not files:
            return None
        path = files[-1]
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def report_days(data_dir: Path) -> list[str]:
    folder = data_dir / REPORT_DIR
    return (
        sorted((p.stem for p in folder.glob("????-??-??.json")), reverse=True)
        if folder.exists()
        else []
    )
