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
1. This provider's event id, seen before.
2. ESPN event id, when the provider supplies one.
3. Same two teams, either orientation, commence time within ``MATCH_WINDOW``.
4. Otherwise a new game.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, Team, TeamAlias
from ttk.domain import Sport
from ttk.providers.base import NormalizedGame, TeamRef
from ttk.teams import CURATED_ALIASES, normalize_team_name

SCHEDULE_AUTHORITY = "espn"
MATCH_WINDOW = timedelta(hours=24)


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


def resolve_team(
    session: Session, *, provider: str, sport: Sport, ref: TeamRef, stats: Counter[str]
) -> Team:
    names = [ref.name, *ref.other_names]
    team: Team | None = None

    if ref.espn_id:
        team = _team_by_espn_id(session, sport, ref.espn_id)
        if team is None:
            # An ESPN-identified team may exist unmatched (created from another
            # provider's name before ESPN reported it): adopt it.
            candidate = _by_normalized_name(session, sport, names)
            if candidate is not None and candidate.espn_id is None:
                candidate.espn_id = ref.espn_id
                team = candidate
            else:
                team = _new_team(session, sport, ref.name, ref.espn_id)
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


def _apply_results(game: Game, g: NormalizedGame, swapped: bool) -> None:
    """Only the schedule authority writes schedule and result fields."""
    if g.provider != SCHEDULE_AUTHORITY:
        return
    game.commence_time = g.commence_time
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
    _apply_results(resolved.game, g, resolved.swapped)
    session.flush()
    return resolved


def _find_game(session: Session, g: NormalizedGame, stats: Counter[str]) -> ResolvedGame:
    link = session.scalar(
        select(GameSourceId).where(
            GameSourceId.provider == g.provider,
            GameSourceId.source_identifier == g.source_identifier,
        )
    )
    if link is not None:
        return ResolvedGame(session.get_one(Game, link.game_id), link.swapped)

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

    candidates = session.scalars(
        select(Game).where(
            Game.sport == g.sport,
            or_(
                (Game.home_team_id == home.id) & (Game.away_team_id == away.id),
                (Game.home_team_id == away.id) & (Game.away_team_id == home.id),
            ),
            Game.commence_time.between(
                g.commence_time - MATCH_WINDOW, g.commence_time + MATCH_WINDOW
            ),
        )
    ).all()
    if len(candidates) == 1:
        game = candidates[0]
        stats["linked_by_teams_and_time"] += 1
        return _link(session, g, game, swapped=game.home_team_id != home.id)
    if len(candidates) > 1:
        stats["ambiguous_game_match"] += 1  # never guess: fall through to a new game

    game = Game(
        sport=g.sport,
        home_team_id=home.id,
        away_team_id=away.id,
        commence_time=g.commence_time,
    )
    session.add(game)
    session.flush()
    return _link(session, g, game, swapped=False)
