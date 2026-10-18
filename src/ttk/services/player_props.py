"""Player props for the Players page: each prop's market line next to how often the
player cleared it.

For every game in the window, the latest props pull (services/props: 24 h and 1 h
before kickoff) gives each (player, market) a main line: the number the most books
quote on both sides. At it: the median no-vig P(over) across those books, and the
best over and under prices. Anytime-TD Yes/No is treated as over/under 0.5.

Milestones ("200+ passing yards", ``milestones=True``): every rung a ladder quote
lists ("N+ ..." with no line) is an over at N - 0.5. Books post most of them one-sided,
so their market chance is often only the books' implied one (their cut still in).

A player is grouped by props.player_key: books spell him differently ("C.J. Stroud",
"C.J Stroud (HOU)"), and each spelling would otherwise be its own row.

Hit rate: the player's games before this one with the market's stat known
(NFL: nflverse player stats; NBA: ESPN box scores) - this season's, and his last
10 across seasons - counted over, under and push against today's line. The player
is matched by name on his team's roster in those stats (props.player_key and the
same-surname fallback), then followed by his own id across teams.

This is description, not a model: there is no validated prop model, so nothing
here is a recommendation (docs/MODEL-GOVERNANCE.md).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import (
    Game,
    NflPlayerGameStat,
    PlayerGameStat,
    PropPull,
    PropQuote,
    Sportsbook,
    Team,
)
from ttk.domain import GameStatus, Sport
from ttk.research.moneyline import no_vig
from ttk.services.props import player_key, roster_sides

NFL_STATS: dict[str, Callable[[NflPlayerGameStat], float]] = {
    "player_pass_yds": lambda r: r.passing_yards,
    "player_pass_tds": lambda r: r.passing_tds,
    "player_pass_completions": lambda r: r.completions,
    "player_pass_attempts": lambda r: r.attempts,
    "player_pass_interceptions": lambda r: r.interceptions,
    "player_pass_rush_yds": lambda r: r.passing_yards + r.rushing_yards,
    "player_rush_yds": lambda r: r.rushing_yards,
    "player_rush_attempts": lambda r: r.carries,
    "player_rush_tds": lambda r: r.rushing_tds,
    "player_receptions": lambda r: r.receptions,
    "player_reception_yds": lambda r: r.receiving_yards,
    "player_reception_tds": lambda r: r.receiving_tds,
    "player_rush_reception_yds": lambda r: r.rushing_yards + r.receiving_yards,
    "player_anytime_td": lambda r: r.rushing_tds + r.receiving_tds,
}
"""Markets whose stat nflverse records (kicking, defense, longest and first/last
TD are not in the player stats, so they get no hit rate)."""


def _three(r: PlayerGameStat) -> float | None:
    return float(r.fg3m) if r.fg3m is not None else None


def _sum(*names: str) -> Callable[[PlayerGameStat], float | None]:
    def f(r: PlayerGameStat) -> float | None:
        values = [getattr(r, n) for n in names]
        return None if any(v is None for v in values) else float(sum(values))

    return f


def _reb(r: PlayerGameStat) -> float | None:
    return None if r.oreb is None or r.dreb is None else float(r.oreb + r.dreb)


def _pra(*parts: str) -> Callable[[PlayerGameStat], float | None]:
    def f(r: PlayerGameStat) -> float | None:
        total = 0.0
        for part in parts:
            v = _reb(r) if part == "reb" else getattr(r, part)
            if v is None:
                return None
            total += v
        return total

    return f


NBA_STATS: dict[str, Callable[[PlayerGameStat], float | None]] = {
    "player_points": _sum("points"),
    "player_rebounds": _reb,
    "player_assists": _sum("ast"),
    "player_threes": _three,
    "player_steals": _sum("stl"),
    "player_blocks": _sum("blk"),
    "player_turnovers": _sum("tov"),
    "player_points_rebounds_assists": _pra("points", "reb", "ast"),
    "player_points_rebounds": _pra("points", "reb"),
    "player_points_assists": _pra("points", "ast"),
    "player_rebounds_assists": _pra("reb", "ast"),
    "player_blocks_steals": _sum("blk", "stl"),
}
"""PropLine NBA market keys (named like the NFL ones); a market not listed here is
shown without a hit rate."""

YES_NO = {"Yes": "Over", "No": "Under"}
LAST_N = 10
LADDER = re.compile(r"^(\d+)\+ ")
"""A milestone rung: "200+ Passing Yards" = an over at 199.5."""
MIN_MILESTONE_BOOKS = 2


@dataclass(frozen=True)
class Price:
    american: float
    book: str


@dataclass(frozen=True)
class HitRate:
    games: int
    over: int
    under: int
    push: int
    average: float | None

    @property
    def over_share(self) -> float | None:
        decided = self.over + self.under
        return self.over / decided if decided else None


@dataclass
class PropRow:
    game_id: int
    sport: str
    matchup: str
    commence_time: datetime
    pulled_at: datetime
    hours_before: int
    player: str
    team: str
    market: str
    line: float
    books: int
    over_probability: float | None
    """Median no-vig P(over) at the line across books quoting both sides."""
    best_over: Price | None
    best_under: Price | None
    season: HitRate | None = None
    last10: HitRate | None = None
    recent: list[float] = field(default_factory=list)
    """The last 10 values, most recent first."""
    milestone: int | None = None
    """For a milestone row: N in "N+" (the line is N - 0.5)."""
    implied_over: float | None = None
    """Median of the books' implied P(over) at the line, their cut still in: the only
    market chance a one-sided milestone has (it overstates the real chance)."""


Points = dict[float, dict[str, dict[str, float]]]
"""line -> book -> {Over, Under: price}."""


@dataclass
class _Prop:
    names: Counter[str] = field(default_factory=Counter)
    points: Points = field(default_factory=lambda: defaultdict(dict))
    rungs: set[float] = field(default_factory=set)
    """Lines a ladder ("N+") quote lists."""

    @property
    def name(self) -> str:
        """The spelling most books use, without a team code."""
        return re.sub(r"\s*\([A-Za-z]{2,4}\)\s*$", "", self.names.most_common(1)[0][0])


def _props(quotes: Sequence[tuple[PropQuote, str]]) -> dict[tuple[str, str, str], _Prop]:
    """(player key, team side, market) -> every book's prices at every line."""
    by: dict[tuple[str, str, str], _Prop] = defaultdict(_Prop)
    for q, book in quotes:
        point: float | None
        rung = LADDER.match(q.selection) if q.point is None else None
        if rung:
            side, point = "Over", int(rung.group(1)) - 0.5
        else:
            side = YES_NO.get(q.selection, q.selection)
            point = q.point if q.point is not None else (0.5 if q.selection in YES_NO else None)
        if side not in ("Over", "Under") or point is None:
            continue
        prop = by[(player_key(q.player), q.team_side, q.market)]
        prop.names[q.player] += 1
        if rung:
            prop.rungs.add(point)
        book_sides = prop.points[point].setdefault(book, {})
        # A book listing the same outcome twice: keep its better price.
        if side not in book_sides or q.american_odds > book_sides[side]:
            book_sides[side] = q.american_odds
    return by


def _main_line(points: Points) -> float:
    def both(point: float) -> int:
        return sum(1 for sides in points[point].values() if len(sides) == 2)

    return max(points, key=lambda pt: (both(pt), len(points[pt]), -abs(pt)))


def _implied(american: float) -> float:
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def _best(books: Mapping[str, Mapping[str, float]], side: str) -> Price | None:
    offers = [(sides[side], book) for book, sides in books.items() if side in sides]
    if not offers:
        return None
    american, book = max(offers)
    return Price(american, book)


def _hit(values: Sequence[float], line: float) -> HitRate:
    return HitRate(
        len(values),
        sum(1 for v in values if v > line),
        sum(1 for v in values if v < line),
        sum(1 for v in values if v == line),
        statistics.fmean(values) if values else None,
    )


@dataclass
class _History:
    """A sport's player stat rows before ``before``, indexed for matching."""

    by_team_key: dict[int, dict[str, str]]
    """team id -> player key -> player id (from that team's recent games)."""
    games: dict[str, list[tuple[datetime, int, Any]]]
    """player id -> [(kickoff, season, row)], most recent first."""


def _history(session: Session, sport: str, team_ids: Iterable[int], before: datetime) -> _History:
    model: Any = NflPlayerGameStat if sport == Sport.NFL else PlayerGameStat
    since = before - timedelta(days=550)  # this season and last
    rows = session.execute(
        select(model, Game.commence_time, Game.season)
        .join(Game, Game.id == model.game_id)
        .where(
            Game.sport == sport,
            Game.status == GameStatus.FINAL,
            Game.commence_time < before,
            Game.commence_time >= since,
        )
        .order_by(Game.commence_time.desc())
    ).all()
    teams = set(team_ids)
    by_team_key: dict[int, dict[str, str]] = defaultdict(dict)
    games: dict[str, list[tuple[datetime, int, Any]]] = defaultdict(list)
    for row, kick, season in rows:
        if sport == Sport.NBA and not row.played:
            continue
        games[row.player_id].append((kick, season, row))
        if row.team_id in teams and row.player_name:
            # Most recent first: a player's latest team wins.
            by_team_key[row.team_id].setdefault(player_key(row.player_name), row.player_id)
    return _History(dict(by_team_key), dict(games))


def _match(history: _History, team_id: int, name: str) -> str | None:
    """The player's id on his team's roster: the exact name key, or else the one
    roster player with the same first initial and surname (props.roster_sides)."""
    roster = history.by_team_key.get(team_id, {})
    key = player_key(name)
    if key in roster:
        return roster[key]
    if " " not in key:
        return None
    fits = [k for k in roster if roster_sides(key, {"team": {k}})]
    return roster[fits[0]] if len(fits) == 1 else None


def props_for_games(
    session: Session, games: Sequence[Game], *, milestones: bool = False
) -> list[PropRow]:
    """Props of these games from each game's latest pull, with hit rates: each prop's
    main line, or with ``milestones`` every ladder rung (N+) at least
    ``MIN_MILESTONE_BOOKS`` books price."""
    books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
    teams = {t.id: t for t in session.scalars(select(Team))}
    out: list[PropRow] = []
    histories: dict[str, _History] = {}
    for game in games:
        pull = session.scalars(
            select(PropPull)
            .where(PropPull.game_id == game.id)
            .order_by(PropPull.pulled_at.desc())
            .limit(1)
        ).first()
        if pull is None:
            continue
        quotes = [
            (q, books[q.sportsbook_id])
            for q in session.scalars(select(PropQuote).where(PropQuote.pull_id == pull.id))
        ]
        sport = str(game.sport)
        stat_fns: Mapping[str, Callable[[Any], float | None]] = (
            NFL_STATS if sport == Sport.NFL else NBA_STATS
        )
        if sport not in histories:
            histories[sport] = _history(
                session,
                sport,
                [t.id for t in teams.values() if t.sport == sport],
                max(g.commence_time for g in games),
            )
        history = histories[sport]
        home, away = teams[game.home_team_id], teams[game.away_team_id]
        for (_, side, market), prop in _props(quotes).items():
            team = home if side == "HOME" else away
            player = prop.name
            if milestones:
                lines = sorted(
                    pt
                    for pt in prop.rungs
                    if sum(1 for s in prop.points[pt].values() if "Over" in s)
                    >= MIN_MILESTONE_BOOKS
                )
            else:
                lines = [_main_line(prop.points)]
            fn = stat_fns.get(market)
            player_id = _match(history, team.id, player) if fn is not None else None
            past: list[tuple[int, float]] = []
            if fn is not None and player_id is not None:
                past = [
                    (season, v)
                    for kick, season, r in history.games.get(player_id, [])
                    if kick < game.commence_time and (v := fn(r)) is not None
                ]
            for line in lines:
                book_prices = prop.points[line]
                over_ps = [
                    p
                    for sides in book_prices.values()
                    if len(sides) == 2 and (p := no_vig(sides["Over"], sides["Under"])) is not None
                ]
                implied = [_implied(s["Over"]) for s in book_prices.values() if "Over" in s]
                row = PropRow(
                    game.id,
                    sport,
                    f"{away.name} @ {home.name}",
                    game.commence_time,
                    pull.pulled_at,
                    pull.horizon_hours,
                    player,
                    team.abbreviation or team.name,
                    market,
                    line,
                    len(book_prices),
                    statistics.median(over_ps) if over_ps else None,
                    _best(book_prices, "Over"),
                    _best(book_prices, "Under"),
                    milestone=int(line + 0.5) if milestones else None,
                    implied_over=statistics.median(implied) if implied else None,
                )
                if player_id is not None:
                    this_season = [v for season, v in past if season == game.season]
                    last = [v for _, v in past[:LAST_N]]
                    row.season = _hit(this_season, line)
                    row.last10 = _hit(last, line)
                    row.recent = last
                out.append(row)
    return out


def upcoming_games(
    session: Session, start: datetime, end: datetime, sport: str | None = None
) -> list[Game]:
    stmt = select(Game).where(
        Game.commence_time >= start,
        Game.commence_time < end,
        Game.sport.in_([Sport.NFL, Sport.NBA] if sport is None else [sport]),
        Game.status != GameStatus.DUPLICATE,
        Game.id.in_(select(PropPull.game_id)),
    )
    return list(session.scalars(stmt.order_by(Game.commence_time)))
