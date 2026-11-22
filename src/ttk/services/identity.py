"""Resolve provider teams and games to canonical rows.

ESPN is the schedule authority. Other providers' games link to ESPN's rows so
that odds, predictions and results for one real game share one ``games.id``.

Team resolution order (first hit wins; never guesses):
1. ESPN team id, when the provider supplies one.
2. This provider's exact name, seen before (team_aliases).
3. Curated alias table (``ttk.teams.CURATED_ALIASES``), verified ESPN ids.
4. Normalized name matching exactly one team's known names.
5. Otherwise a new, unmatched team (espn_id NULL), counted as ``unmatched_team``.

Game resolution order:
1. This provider's event id, seen before. (An ESPN event first listed as
   TBD v TBD is settled once ESPN names its teams: ``_settle_placeholder``.)
2. ESPN event id, when the provider supplies one.
3. Same two teams, either orientation, commence time within ``MATCH_WINDOW``.
4. Otherwise a new game.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, Team, TeamAlias
from ttk.domain import GameStatus, Sport
from ttk.providers.base import NormalizedGame, TeamRef
from ttk.teams import CURATED_ALIASES, normalize_team_name

SCHEDULE_AUTHORITY = "espn"
# Providers allowed to write schedule/result fields, highest rank first. A provider
# only writes if no higher-ranked authority is linked to the game: nflverse fills
# NFL history that ESPN was never asked for, but never overrides ESPN.
AUTHORITY_RANK = {"espn": 2, "nflverse": 1}
MATCH_WINDOW = timedelta(hours=24)
BUSY_WINDOW = timedelta(hours=12)
SAME_START = timedelta(hours=3)  # picks one of several games in MATCH_WINDOW


def _as_utc(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


"""A team plays one game at a time: an odds event that would create a game for a
team already scheduled within this window is a phantom (on 2026-10-17 two offshore
books listed eleven NBA teams "@ San Antonio Spurs" at the Spurs' real tip-off).
Shorter than a back-to-back (about 24 h apart)."""


def _add_alias(session: Session, provider: str, sport: Sport, name: str, team: Team) -> None:
    exists = session.scalar(
        select(TeamAlias.id).where(
            TeamAlias.provider == provider,
            TeamAlias.sport == sport,
            TeamAlias.alias == name,
            TeamAlias.team_id == team.id,
        )
    )
    if exists is None:
        session.add(
            TeamAlias(
                provider=provider,
                sport=sport,
                alias=name,
                normalized=normalize_team_name(name),
                team_id=team.id,
            )
        )


def _team_by_espn_id(session: Session, sport: Sport, espn_id: str) -> Team | None:
    return session.scalar(select(Team).where(Team.sport == sport, Team.espn_id == espn_id))


def _new_team(session: Session, sport: Sport, name: str, espn_id: str | None) -> Team:
    """Create a team; team names are unique per sport, so a clash gets a suffix
    rather than silently merging two different teams."""
    taken = session.scalar(select(Team.id).where(Team.sport == sport, Team.name == name))
    team = Team(
        sport=sport,
        name=name if taken is None else f"{name} [{espn_id or 'unmatched'}]",
        espn_id=espn_id,
    )
    session.add(team)
    session.flush()
    return team


def _only_team(session: Session, team_ids: set[int]) -> Team | None:
    """The team if exactly one matched; ambiguity is never resolved by guessing."""
    return session.get(Team, team_ids.pop()) if len(team_ids) == 1 else None


def _by_normalized_name(session: Session, sport: Sport, names: list[str]) -> Team | None:
    normalized = {normalize_team_name(n) for n in names}
    team_ids = set(
        session.scalars(
            select(TeamAlias.team_id).where(
                TeamAlias.sport == sport, TeamAlias.normalized.in_(normalized)
            )
        )
    )
    return _only_team(session, team_ids)


def _use_espn_name(session: Session, sport: Sport, team: Team, ref: TeamRef) -> None:
    """ESPN names teams for display: a team first seen via another provider
    ("Penn St.") takes ESPN's name, unless another row already holds it. Old
    names keep resolving through their aliases."""
    if team.name == ref.name:
        return
    taken = session.scalar(select(Team.id).where(Team.sport == sport, Team.name == ref.name))
    if taken is None:
        team.name = ref.name


def resolve_team(
    session: Session, *, provider: str, sport: Sport, ref: TeamRef, stats: Counter[str]
) -> Team:
    names = [ref.name, *ref.other_names]
    team: Team | None = None

    if ref.espn_id:
        team = _team_by_espn_id(session, sport, ref.espn_id)
        if team is None:
            # An ESPN-identified team may exist unmatched (created from another
            # provider's name before ESPN reported it): adopt it, by normalized
            # name or by a curated alias that points at this ESPN id.
            curated_names = [
                alias
                for alias, espn_id in CURATED_ALIASES.get(sport, {}).items()
                if espn_id == ref.espn_id
            ]
            candidate = _by_normalized_name(session, sport, names + curated_names)
            if candidate is not None and candidate.espn_id is None:
                candidate.espn_id = ref.espn_id
                team = candidate
            else:
                team = _new_team(session, sport, ref.name, ref.espn_id)
        if provider == SCHEDULE_AUTHORITY:
            _use_espn_name(session, sport, team, ref)
    else:
        exact = set(
            session.scalars(
                select(TeamAlias.team_id).where(
                    TeamAlias.provider == provider,
                    TeamAlias.sport == sport,
                    TeamAlias.alias == ref.name,
                )
            )
        )
        team = _only_team(session, exact)
        if team is None:
            curated = CURATED_ALIASES.get(sport, {}).get(normalize_team_name(ref.name))
            if curated is not None:
                team = _team_by_espn_id(session, sport, curated)
        if team is None:
            team = _by_normalized_name(session, sport, names)
        if team is None:
            stats["unmatched_team"] += 1
            # Reuse only an earlier unmatched team of this name, never an ESPN team.
            team = session.scalar(
                select(Team).where(
                    Team.sport == sport, Team.name == ref.name, Team.espn_id.is_(None)
                )
            ) or _new_team(session, sport, ref.name, None)

    for name in names:
        _add_alias(session, provider, sport, name, team)
    session.flush()
    return team


@dataclass(frozen=True)
class ResolvedGame:
    game: Game
    swapped: bool
    """Provider's home team is our away team: flip HOME/AWAY from this provider."""
    sides_unclear: bool = False
    """Neither of the provider's teams resolves to a team of this game, or they
    disagree: ``swapped`` cannot be trusted, so HOME/AWAY quotes must not be stored."""


def _link(session: Session, g: NormalizedGame, game: Game, swapped: bool) -> ResolvedGame:
    session.add(
        GameSourceId(
            provider=g.provider,
            source_identifier=g.source_identifier,
            game_id=game.id,
            swapped=swapped,
        )
    )
    return ResolvedGame(game, swapped)


def _reorient(
    session: Session, g: NormalizedGame, game: Game, link: GameSourceId, stats: Counter[str]
) -> bool:
    """A provider can reverse an event's home and away after we linked it (PropLine
    merging two listings, 2026-10: Texas Tech @ Cincinnati was stored with each
    side's spread on the other team). Check the orientation on every fetch, and
    correct the link when the teams now resolve the other way round.

    One team is enough when the other no longer resolves: 2026-11 PropLine reversed
    Nicholls @ Ole Miss and renamed "Nicholls St." to "Nicholls State Colonels" in
    the same change. The new name became an unmatched team, the check gave up, and
    a day of correct quotes was stored on the wrong side. Home resolving to our
    home team settles the orientation whatever the other name is.

    Returns False when the orientation cannot be told (neither team belongs to the
    game, or the two disagree): the link is left alone, never guessed, and the
    caller must not store sided quotes."""
    home = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.home, stats=stats)
    away = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.away, stats=stats)
    votes: set[bool] = set()
    if home.id == game.home_team_id or away.id == game.away_team_id:
        votes.add(False)
    if home.id == game.away_team_id or away.id == game.home_team_id:
        votes.add(True)
    if len(votes) != 1:
        stats["link_teams_unclear"] += 1
        return False
    if {home.id, away.id} != {game.home_team_id, game.away_team_id}:
        stats["link_oriented_by_one_team"] += 1
    (swapped,) = votes
    if swapped != link.swapped:
        link.swapped = swapped
        stats["link_orientation_fixed"] += 1
    return True


def _apply_results(session: Session, game: Game, g: NormalizedGame, swapped: bool) -> None:
    """Only schedule authorities write schedule and result fields, and a lower-ranked
    authority never overrides a higher-ranked one linked to the same game."""
    rank = AUTHORITY_RANK.get(g.provider)
    if rank is None:
        return
    linked = session.scalars(
        select(GameSourceId.provider).where(GameSourceId.game_id == game.id)
    ).all()
    if any(AUTHORITY_RANK.get(p, 0) > rank for p in linked):
        return
    game.commence_time = g.commence_time
    if g.season_type is not None:
        game.season_type = g.season_type
    if g.week is not None:
        game.week = g.week
    if g.season is not None:
        game.season = g.season
    if g.neutral_site is not None:
        game.neutral_site = g.neutral_site
    if g.status is not None:
        game.status = g.status
    home, away = (g.away_score, g.home_score) if swapped else (g.home_score, g.away_score)
    if g.home_score is not None:
        game.home_score, game.away_score = home, away


def resolve_game(session: Session, g: NormalizedGame, stats: Counter[str]) -> ResolvedGame:
    resolved = _find_game(session, g, stats)
    assert resolved is not None
    _apply_results(session, resolved.game, g, resolved.swapped)
    session.flush()
    return resolved


def resolve_odds_game(
    session: Session, g: NormalizedGame, stats: Counter[str]
) -> ResolvedGame | None:
    """``resolve_game`` for live odds: None (its quotes are skipped) for an event
    that would create a game for a team already playing within ``BUSY_WINDOW``,
    or one linked to a game set aside as DUPLICATE."""
    resolved = _find_game(session, g, stats, refuse_busy=True)
    if resolved is None:
        return None
    _apply_results(session, resolved.game, g, resolved.swapped)
    session.flush()
    return resolved


def _busy(session: Session, sport: str, team_ids: tuple[int, int], at: datetime) -> bool:
    return (
        session.scalar(
            select(Game.id)
            .where(
                Game.sport == sport,
                Game.status != GameStatus.DUPLICATE,
                or_(Game.home_team_id.in_(team_ids), Game.away_team_id.in_(team_ids)),
                Game.commence_time.between(at - BUSY_WINDOW, at + BUSY_WINDOW),
            )
            .limit(1)
        )
        is not None
    )


def _is_placeholder(name: str) -> bool:
    """ESPN lists a tournament game before its teams are known as 'TBD' v 'TBD'."""
    return normalize_team_name(name).split()[:1] in (["tbd"], ["tba"])


def _settle_placeholder(
    session: Session, g: NormalizedGame, game: Game, link: GameSourceId, stats: Counter[str]
) -> ResolvedGame | None:
    """ESPN creates a tournament game as TBD v TBD and names the teams later, under
    the same event id. Following the event id alone left the game with its TBD
    teams for good, so the sportsbooks' listing of the real game stayed a separate
    row with no schedule, no model inputs and no result (2026-11: 17 such games
    in four days).

    Once the event names both teams: if another provider already created the game
    (same teams, within ``MATCH_WINDOW``, no ESPN event), the event moves to that
    game, which holds the odds history, and the placeholder is set aside as
    DUPLICATE. Otherwise the placeholder takes the teams. None when there is
    nothing to settle."""
    held = (session.get_one(Team, game.home_team_id), session.get_one(Team, game.away_team_id))
    if not any(_is_placeholder(t.name) for t in held):
        return None
    if _is_placeholder(g.home.name) or _is_placeholder(g.away.name):
        return None
    home = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.home, stats=stats)
    away = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.away, stats=stats)
    others = session.scalars(
        select(Game)
        .where(
            Game.sport == g.sport,
            Game.id != game.id,
            Game.status != GameStatus.DUPLICATE,
            or_(
                (Game.home_team_id == home.id) & (Game.away_team_id == away.id),
                (Game.home_team_id == away.id) & (Game.away_team_id == home.id),
            ),
            Game.commence_time.between(
                g.commence_time - MATCH_WINDOW, g.commence_time + MATCH_WINDOW
            ),
            ~select(GameSourceId.id)
            .where(GameSourceId.game_id == Game.id, GameSourceId.provider == SCHEDULE_AUTHORITY)
            .exists(),
        )
        .order_by(Game.id)
    ).all()
    if not others:
        game.home_team_id, game.away_team_id = home.id, away.id
        link.swapped = False
        stats["placeholder_took_teams"] += 1
        return ResolvedGame(game, False)

    start = _as_utc(g.commence_time)
    keep = min(others, key=lambda o: (abs(_as_utc(o.commence_time) - start), o.id))
    for extra in others:
        if extra.id == keep.id:
            continue
        # The same game listed twice by a provider: one row carries on. Odds
        # already stored on the other stay there; they are append-only.
        flipped = extra.home_team_id != keep.home_team_id
        for moved in session.scalars(select(GameSourceId).where(GameSourceId.game_id == extra.id)):
            moved.game_id = keep.id
            moved.swapped = moved.swapped != flipped
        extra.status = GameStatus.DUPLICATE
        stats["placeholder_merged_duplicate"] += 1
    link.game_id = keep.id
    link.swapped = keep.home_team_id != home.id
    game.status = GameStatus.DUPLICATE
    stats["placeholder_adopted_game"] += 1
    session.flush()
    return ResolvedGame(keep, link.swapped)


def _find_game(
    session: Session, g: NormalizedGame, stats: Counter[str], *, refuse_busy: bool = False
) -> ResolvedGame | None:
    link = session.scalar(
        select(GameSourceId).where(
            GameSourceId.provider == g.provider,
            GameSourceId.source_identifier == g.source_identifier,
        )
    )
    if link is not None:
        game = session.get_one(Game, link.game_id)
        if refuse_busy and game.status == GameStatus.DUPLICATE:
            stats["linked_to_set_aside_game"] += 1
            return None
        if g.provider == SCHEDULE_AUTHORITY:
            settled = _settle_placeholder(session, g, game, link, stats)
            if settled is not None:
                return settled
            # Keep display names current even for games already linked.
            home_ref, away_ref = (g.away, g.home) if link.swapped else (g.home, g.away)
            for team_id, ref in ((game.home_team_id, home_ref), (game.away_team_id, away_ref)):
                team = session.get_one(Team, team_id)
                if ref.espn_id and team.espn_id == ref.espn_id:
                    _use_espn_name(session, g.sport, team, ref)
        else:
            if not _reorient(session, g, game, link, stats):
                return ResolvedGame(game, link.swapped, sides_unclear=True)
        return ResolvedGame(game, link.swapped)

    home = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.home, stats=stats)
    away = resolve_team(session, provider=g.provider, sport=g.sport, ref=g.away, stats=stats)

    if g.espn_event_id and g.provider != SCHEDULE_AUTHORITY:
        espn_link = session.scalar(
            select(GameSourceId).where(
                GameSourceId.provider == SCHEDULE_AUTHORITY,
                GameSourceId.source_identifier == g.espn_event_id,
            )
        )
        if espn_link is not None:
            game = session.get_one(Game, espn_link.game_id)
            stats["linked_by_espn_event_id"] += 1
            return _link(session, g, game, swapped=game.home_team_id == away.id)

    query = select(Game).where(
        Game.sport == g.sport,
        or_(
            (Game.home_team_id == home.id) & (Game.away_team_id == away.id),
            (Game.home_team_id == away.id) & (Game.away_team_id == home.id),
        ),
        Game.commence_time.between(g.commence_time - MATCH_WINDOW, g.commence_time + MATCH_WINDOW),
    )
    if g.provider == SCHEDULE_AUTHORITY:
        # Each ESPN event is its own game: it may adopt a game another provider
        # created, never one that already belongs to a different ESPN event (NBA
        # teams can meet twice in two days with home and away reversed).
        query = query.where(
            ~select(GameSourceId.id)
            .where(
                GameSourceId.game_id == Game.id,
                GameSourceId.provider == SCHEDULE_AUTHORITY,
            )
            .exists()
        )
    candidates = session.scalars(query).all()
    if len(candidates) == 1:
        game = candidates[0]
        stats["linked_by_teams_and_time"] += 1
        return _link(session, g, game, swapped=game.home_team_id != home.id)
    if len(candidates) > 1:
        # The same teams on consecutive nights (an NBA back-to-back rematch): the
        # event is the one game starting at its own time, if exactly one does.
        near = [
            c
            for c in candidates
            if abs(_as_utc(c.commence_time) - _as_utc(g.commence_time)) <= SAME_START
        ]
        if len(near) == 1:
            stats["linked_by_start_time"] += 1
            return _link(session, g, near[0], swapped=near[0].home_team_id != home.id)
        stats["ambiguous_game_match"] += 1  # never guess: fall through to a new game
    if (
        refuse_busy
        and g.provider not in AUTHORITY_RANK  # a schedule authority's event is real
        and _busy(session, g.sport, (home.id, away.id), g.commence_time)
    ):
        stats["team_already_playing"] += 1
        return None

    game = Game(
        sport=g.sport,
        home_team_id=home.id,
        away_team_id=away.id,
        commence_time=g.commence_time,
    )
    session.add(game)
    session.flush()
    return _link(session, g, game, swapped=False)
