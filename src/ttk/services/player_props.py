"""Player props for the Players page: each prop's market line next to how often the
player cleared it.

For every game in the window, the latest props pull (services/props: 24 h and 1 h
before kickoff) gives each (player, market) a main line: the number the most books
quote on both sides. At it: the median no-vig P(over) across those books, and the
best over and under prices. Anytime-TD Yes/No is treated as over/under 0.5.

Hit rate: the player's games before this one with the market's stat known
(NFL: nflverse player stats; NBA: ESPN box scores) - this season's, and his last
10 across seasons - counted over, under and push against today's line. The player
is matched by name on his team's roster in those stats (props.player_key and the
same-surname fallback), then followed by his own id across teams.

This is description, not a model: there is no validated prop model, so nothing
here is a recommendation (docs/MODEL-GOVERNANCE.md).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
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


def _main_lines(
    quotes: Sequence[tuple[PropQuote, str]],
) -> dict[tuple[str, str, str], tuple[float, dict[str, dict[str, float]]]]:
    """(player, team side, market) -> (main line, book -> {Over, Under: price})."""
    by: dict[tuple[str, str, str], dict[float, dict[str, dict[str, float]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for q, book in quotes:
        side = YES_NO.get(q.selection, q.selection)
        if side not in ("Over", "Under"):
            continue
        point = q.point if q.point is not None else (0.5 if q.selection in YES_NO else None)
        if point is None:
            continue
        book_sides = by[(q.player, q.team_side, q.market)][point].setdefault(book, {})
        # A book listing the same outcome twice: keep its better price.
        if side not in book_sides or q.american_odds > book_sides[side]:
            book_sides[side] = q.american_odds
    out = {}
    for key, points in by.items():

        def both(point: float, points: Mapping[float, dict[str, dict[str, float]]] = points) -> int:
            return sum(1 for sides in points[point].values() if len(sides) == 2)

        main = max(points, key=lambda pt: (both(pt), len(points[pt]), -abs(pt)))
        out[key] = (main, points[main])
    return out


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


def props_for_games(session: Session, games: Sequence[Game]) -> list[PropRow]:
    """Props of these games from each game's latest pull, with hit rates."""
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
        for (player, side, market), (line, book_prices) in _main_lines(quotes).items():
            team = home if side == "HOME" else away
            over_ps = [
                p
                for sides in book_prices.values()
                if len(sides) == 2 and (p := no_vig(sides["Over"], sides["Under"])) is not None
            ]
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
            )
            fn = stat_fns.get(market)
            player_id = _match(history, team.id, player) if fn is not None else None
            if fn is not None and player_id is not None:
                past = [
                    (season, v)
                    for kick, season, r in history.games.get(player_id, [])
                    if kick < game.commence_time and (v := fn(r)) is not None
                ]
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
