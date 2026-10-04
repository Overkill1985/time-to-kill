"""Persist normalized odds as an append-only change log, with an ingestion-run audit row."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import BookObservation, IngestionRun, OddsSnapshot, Sportsbook, utcnow
from ttk.domain import Selection, Sport
from ttk.providers.base import NormalizedOddsQuote, OddsFetch, OddsProvider
from ttk.services.identity import resolve_game
from ttk.services.odds_state import QuoteKey, latest_rows
from ttk.services.runs import RunResult, audited_run

_FLIP = {Selection.HOME: Selection.AWAY, Selection.AWAY: Selection.HOME}

ALT_LINE_WINDOW = 3.0
"""Spread and total lines are stored only within this many points of each book's
main line in the same poll (chosen 2026-10-04: alternates were 93% of rows, ~1M a
day in football season, and nothing but line shopping uses far ones). A line that
leaves the window is recorded as withdrawn (no longer offered *or* stored)."""


def _home_equivalent(market: str, selection: str, line: float) -> float:
    """Spread lines in home terms (AWAY +3 is HOME -3); totals as they are."""
    return -line if market == "SPREAD" and selection == "AWAY" else line


def main_line(quotes: dict[QuoteKey, NormalizedOddsQuote], market: str) -> float | None:
    """The book's main line for ``market`` in this poll: of its lines priced on
    both sides, the one whose no-vig probability is closest to 50/50."""
    sides = ("HOME", "AWAY") if market == "SPREAD" else ("OVER", "UNDER")
    best: tuple[float, float] | None = None
    for (m, selection, line), q in quotes.items():
        if m != market or selection != sides[0] or line is None:
            continue
        other_line = -line if market == "SPREAD" else line
        other = quotes.get((m, sides[1], other_line))
        if other is None:
            continue
        p = bm.no_vig_probabilities(
            [bm.american_to_decimal(q.american_odds), bm.american_to_decimal(other.american_odds)]
        ).probabilities[0]
        if best is None or abs(p - 0.5) < best[0]:
            best = (abs(p - 0.5), line)
    return None if best is None else best[1]


def within_window(
    quotes: dict[QuoteKey, NormalizedOddsQuote],
    stats: Counter[str],
    window: float = ALT_LINE_WINDOW,
) -> dict[QuoteKey, NormalizedOddsQuote]:
    """Drop spread/total lines more than ``window`` points from the book's main line.
    Moneylines are kept; a market with no two-sided line is kept whole (counted)."""
    mains = {m: main_line(quotes, m) for m in ("SPREAD", "TOTAL")}
    kept: dict[QuoteKey, NormalizedOddsQuote] = {}
    for key, q in quotes.items():
        market, selection, line = key
        if market not in mains or line is None:
            kept[key] = q
            continue
        main = mains[market]
        if main is None:
            stats["no_main_line"] += 1
            kept[key] = q
        elif abs(_home_equivalent(market, selection, line) - main) <= window:
            kept[key] = q
        else:
            stats["outside_line_window"] += 1
    return kept


def _sportsbook(session: Session, cache: dict[str, Sportsbook], key: str, name: str) -> Sportsbook:
    if key not in cache:
        book = session.scalar(select(Sportsbook).where(Sportsbook.key == key))
        if book is None:
            book = Sportsbook(key=key, name=name)
            session.add(book)
            session.flush()
        cache[key] = book
    return cache[key]


def store_odds(
    session: Session,
    fetch: OddsFetch,
    *,
    run: IngestionRun,
    observed_at: datetime,
    stats: Counter[str],
) -> int:
    """Append only what changed since the provider's last poll (see OddsSnapshot).

    For every game in this fetch: each book present gets a BookObservation; a quote
    is written if it is new or its price changed; a quote on file but absent now -
    including every quote of a book that no longer lists the game - is written as
    withdrawn. Games absent from the fetch are left as they were (e.g. finished).
    Returns the number of change rows written."""
    provider = run.provider
    games = {g.source_identifier: resolve_game(session, g, stats) for g in fetch.games}
    books: dict[str, Sportsbook] = {}

    # This poll's quotes: (game, book) -> key -> quote (last one wins on duplicates).
    polled: dict[tuple[int, int], dict[QuoteKey, NormalizedOddsQuote]] = defaultdict(dict)
    for q in fetch.quotes:
        resolved = games.get(q.game_source_identifier)
        if resolved is None:
            continue
        # A spread line is from the selection's own perspective, so flipping the
        # side (provider's HOME is our AWAY) leaves the line unchanged.
        selection = _FLIP.get(q.selection, q.selection) if resolved.swapped else q.selection
        book = _sportsbook(session, books, q.sportsbook_key, q.sportsbook_name)
        key = (str(q.market), str(selection), q.line)
        if key in polled[(resolved.game.id, book.id)]:
            stats["duplicate_quote"] += 1
        polled[(resolved.game.id, book.id)][key] = replace(q, selection=selection)

    for book_key, quotes in polled.items():
        polled[book_key] = within_window(quotes, stats)

    game_ids = {r.game.id for r in games.values()}
    on_file = {
        k: row for k, row in latest_rows(session, provider, game_ids).items() if not row.withdrawn
    }
    written = 0

    def append(
        game_id: int, book_id: int, q: NormalizedOddsQuote | OddsSnapshot, *, withdrawn: bool
    ) -> None:
        nonlocal written
        decimal_odds = bm.american_to_decimal(q.american_odds)
        session.add(
            OddsSnapshot(
                game_id=game_id,
                sportsbook_id=book_id,
                market=str(q.market),
                selection=str(q.selection),
                line=q.line,
                american_odds=q.american_odds,
                decimal_odds=decimal_odds,
                implied_probability=bm.decimal_implied_probability(decimal_odds),
                provider=provider,
                source_timestamp=q.source_timestamp,
                observed_at=observed_at,
                ingestion_run_id=run.id,
                withdrawn=withdrawn,
            )
        )
        written += 1

    for (game_id, book_id), quotes in polled.items():
        session.add(
            BookObservation(
                game_id=game_id,
                sportsbook_id=book_id,
                provider=provider,
                observed_at=observed_at,
                ingestion_run_id=run.id,
                quotes=len(quotes),
            )
        )
        for key, q in quotes.items():
            previous = on_file.get((game_id, book_id, *key))
            if previous is not None and previous.american_odds == q.american_odds:
                stats["unchanged"] += 1
                continue
            stats["changed" if previous is not None else "new"] += 1
            append(game_id, book_id, q, withdrawn=False)

    for (game_id, book_id, key_market, key_selection, key_line), row in on_file.items():
        if (key_market, key_selection, key_line) not in polled.get((game_id, book_id), {}):
            stats["withdrawn"] += 1
            append(game_id, book_id, row, withdrawn=True)
    return written


def run_odds_ingestion(
    session_factory: sessionmaker[Session], provider: OddsProvider, sport: Sport
) -> IngestionRun:
    """Fetch and store one sport's odds. A provider failure is recorded, not raised."""

    def work(session: Session, run: IngestionRun) -> RunResult:
        fetch = provider.fetch_odds(sport)
        stats: Counter[str] = Counter()
        written = store_odds(session, fetch, run=run, observed_at=utcnow(), stats=stats)
        return RunResult(written, fetch.skipped, stats)

    return audited_run(session_factory, provider=provider.name, kind="odds", sport=sport, work=work)
