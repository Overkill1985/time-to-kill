"""Player props: snapshots, not a stream. PropLine prices props per event (1
request each), so instead of polling every 15 minutes the collector takes one
pull per game at each horizon (24 h and 1 h before kickoff), NFL and NBA,
regular season only. A pull is 2 requests: the event's market list, then its
player markets' odds. Pulls stop while fewer than ``MIN_REMAINING`` requests are
left, so props never starve the game-odds polling.

Kept: real sportsbook prices only. Pick'em apps (flat payouts, ``dfs_odds_type``
or ``payout_multiplier`` set) and prediction markets are dropped, and so is any
prop whose player is on neither team's ESPN roster at the pull (the feed was
seen filing a Hornets player under a Pacers game, 2026-10-07). Every drop is
counted in the pull's ``stats``.

These are records for later research; no model prices props yet.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, PropPull, Sportsbook, Team, utcnow
from ttk.db.models import PropQuote as PropQuoteRow
from ttk.domain import GameStatus, Sport
from ttk.providers.base import ProviderError
from ttk.providers.propline import PropQuote
from ttk.services.forward_test import due_horizon

PROP_SPORTS = (Sport.NFL, Sport.NBA)
MAX_DROPPED_NAMES = 30
HORIZONS = (24, 1)
MIN_REMAINING = 100
NOT_SPORTSBOOKS = frozenset(
    {
        "prizepicks",
        "underdog",
        "dabble",
        "parlayplay",
        "pick6",
        "sleeper",
        "betr",
        "chalkboard",
        "kalshi",
        "polymarket",
        "polymarket_us",
        "novig",
        "prophetx",
        "sporttrade",
    }
)
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def player_key(name: str) -> str:
    """Lowercase ASCII words without punctuation, a name suffix, or a trailing team
    code ('Aaron Jones Sr.' and 'Aaron Jones' match; some books send 'Dak Prescott
    (DAL)')."""
    name = re.sub(r"\s*\([A-Za-z]{2,4}\)\s*$", "", name)
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9 ]", " ", s.replace("'", "").replace(".", "")).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def is_sportsbook(q: PropQuote) -> bool:
    return q.book not in NOT_SPORTSBOOKS and q.dfs_odds_type is None and q.payout_multiplier is None


class PropsProvider(Protocol):
    """What a pull needs from the odds provider (PropLineProvider)."""

    name: str
    daily_remaining: int | None

    def event_markets(self, sport: Sport, event_id: str) -> list[str]: ...
    def event_props(self, sport: Sport, event_id: str, markets: list[str]) -> list[PropQuote]: ...


@dataclass(frozen=True)
class DuePull:
    game_id: int
    sport: Sport
    horizon: int
    event_id: str


def due_pulls(session: Session, now: datetime) -> list[DuePull]:
    games = session.scalars(
        select(Game).where(
            Game.sport.in_([str(s) for s in PROP_SPORTS]),
            Game.commence_time > now,
            Game.commence_time <= now + timedelta(hours=max(HORIZONS)),
            Game.status != GameStatus.DUPLICATE,
            or_(Game.season_type.is_(None), Game.season_type != "PRE"),
        )
    ).all()
    out = []
    for g in games:
        h = due_horizon(g.commence_time, now, HORIZONS)
        if h is None:
            continue
        taken = session.scalar(
            select(PropPull.id).where(PropPull.game_id == g.id, PropPull.horizon_hours == h)
        )
        if taken is not None:
            continue
        event = session.scalar(
            select(func.min(GameSourceId.source_identifier)).where(
                GameSourceId.game_id == g.id, GameSourceId.provider == "propline"
            )
        )
        if event is not None:
            out.append(DuePull(g.id, Sport(g.sport), h, event))
    return out


def _book(session: Session, key: str) -> Sportsbook:
    book = session.scalar(select(Sportsbook).where(Sportsbook.key == key))
    if book is None:
        book = Sportsbook(key=key, name=key.replace("_", " ").title())
        session.add(book)
        session.flush()
    return book


def pull_props(
    session: Session,
    provider: PropsProvider,
    roster: Callable[[Sport, str], list[str]],
    due: DuePull,
    *,
    now: datetime,
) -> PropPull:
    """Take and store one pull. Raises ProviderError (nothing stored) when the
    provider or a roster cannot be read; the next pass retries."""
    game = session.get_one(Game, due.game_id)
    teams = {}
    for side, team_id in (("HOME", game.home_team_id), ("AWAY", game.away_team_id)):
        espn_id = session.get_one(Team, team_id).espn_id
        if espn_id is None:
            raise ProviderError(f"game {game.id}: a team has no ESPN id (no roster)")
        teams[side] = {player_key(n) for n in roster(due.sport, espn_id)}
    markets = [
        m for m in provider.event_markets(due.sport, due.event_id) if m.startswith("player_")
    ]
    stats: Counter[str] = Counter({"player_markets": len(markets)})
    quotes = provider.event_props(due.sport, due.event_id, markets) if markets else []
    kept: list[tuple[PropQuote, str]] = []
    off_roster: Counter[str] = Counter()
    for q in quotes:
        if not is_sportsbook(q):
            stats["dropped_not_a_sportsbook"] += 1
            continue
        key = player_key(q.player)
        sides = [side for side, names in teams.items() if key in names]
        if len(sides) != 1:
            stats["dropped_not_on_one_roster"] += 1
            off_roster[q.player] += 1
            continue
        kept.append((q, sides[0]))
    books = {q.book for q, _ in kept}
    stats["kept"] = len(kept)
    for book in sorted(books):
        stats[f"book:{book}"] = sum(1 for q, _ in kept if q.book == book)
    pull = PropPull(
        game_id=game.id,
        provider=provider.name,
        event_id=due.event_id,
        horizon_hours=due.horizon,
        pulled_at=now,
        books=len(books),
        quotes=len(kept),
        stats={
            **stats,
            # The most-quoted names dropped as off-roster, to tell feed errors
            # from name mismatches.
            "off_roster_names": dict(off_roster.most_common(MAX_DROPPED_NAMES)),
        },
    )
    session.add(pull)
    session.flush()
    book_ids = {key: _book(session, key).id for key in books}
    session.add_all(
        PropQuoteRow(
            pull_id=pull.id,
            sportsbook_id=book_ids[q.book],
            market=q.market,
            player=q.player,
            team_side=side,
            selection=q.selection,
            point=q.point,
            american_odds=q.american_odds,
            book_changed_at=q.last_change_at,
        )
        for q, side in kept
    )
    session.flush()
    return pull


def collect_props(
    session_factory: sessionmaker[Session],
    provider: PropsProvider,
    roster: Callable[[Sport, str], list[str]],
    *,
    now: datetime | None = None,
    min_remaining: int = MIN_REMAINING,
) -> list[str]:
    """Every due pull this pass. Returns log lines."""
    now = now or utcnow()
    lines = []
    cache: dict[tuple[Sport, str], list[str]] = {}

    def cached(sport: Sport, espn_id: str) -> list[str]:
        if (sport, espn_id) not in cache:
            cache[(sport, espn_id)] = roster(sport, espn_id)
        return cache[(sport, espn_id)]

    with session_factory() as session:
        due = due_pulls(session, now)
    for d in due:
        if provider.daily_remaining is not None and provider.daily_remaining < min_remaining:
            lines.append(f"props: paused, {provider.daily_remaining} requests left")
            break
        with session_factory() as session:
            try:
                pull = pull_props(session, provider, cached, d, now=now)
            except ProviderError as exc:
                session.rollback()
                lines.append(f"props {d.sport} game {d.game_id} {d.horizon} h: failed ({exc})")
                continue
            session.commit()
            st = pull.stats or {}
            lines.append(
                f"props {d.sport} game {d.game_id} {d.horizon} h: {pull.quotes} quotes from "
                f"{pull.books} books (dropped {st.get('dropped_not_a_sportsbook', 0)} pick'em or "
                f"exchange, {st.get('dropped_not_on_one_roster', 0)} off-roster)"
            )
    return lines


@dataclass(frozen=True)
class Coverage:
    sport: str
    horizon: int
    pulls: int
    with_quotes: int
    quotes: int
    books: dict[str, tuple[int, int]]
    """book -> (pulls it appeared in, quotes kept)."""
    markets: dict[str, int]
    dropped_not_a_sportsbook: int
    dropped_off_roster: int
    off_roster_names: dict[str, int]
    """The most-quoted dropped names across these pulls (from each pull's sample)."""


def _merged_names(pulls: list[PropPull]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for p in pulls:
        names = (p.stats or {}).get("off_roster_names") or {}
        if isinstance(names, dict):
            total.update({str(k): int(v) for k, v in names.items()})
    return dict(total.most_common(MAX_DROPPED_NAMES))


def coverage(session: Session) -> list[Coverage]:
    """Which books carried kept props, at each sport and horizon."""
    rows = session.execute(
        select(PropPull, Game.sport).join(Game, Game.id == PropPull.game_id)
    ).all()
    groups: dict[tuple[str, int], list[PropPull]] = {}
    for pull, sport in rows:
        groups.setdefault((sport, pull.horizon_hours), []).append(pull)
    out = []
    for (sport, h), pulls in sorted(groups.items()):
        ids = [p.id for p in pulls]
        per_book = session.execute(
            select(Sportsbook.key, func.count(func.distinct(PropQuoteRow.pull_id)), func.count())
            .join(Sportsbook, Sportsbook.id == PropQuoteRow.sportsbook_id)
            .where(PropQuoteRow.pull_id.in_(ids))
            .group_by(Sportsbook.key)
        ).all()
        markets = session.execute(
            select(PropQuoteRow.market, func.count())
            .where(PropQuoteRow.pull_id.in_(ids))
            .group_by(PropQuoteRow.market)
        ).all()
        out.append(
            Coverage(
                sport,
                h,
                len(pulls),
                sum(1 for p in pulls if p.quotes),
                sum(p.quotes for p in pulls),
                {k: (int(n), int(q)) for k, n, q in sorted(per_book, key=lambda r: -r[2])},
                {m: int(n) for m, n in sorted(markets, key=lambda r: -r[1])},
                sum((p.stats or {}).get("dropped_not_a_sportsbook", 0) for p in pulls),
                sum((p.stats or {}).get("dropped_not_on_one_roster", 0) for p in pulls),
                _merged_names(pulls),
            )
        )
    return out
