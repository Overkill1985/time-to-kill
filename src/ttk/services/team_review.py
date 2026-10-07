"""Unmatched teams: review them, and link one to its ESPN team.

A provider name that resolves to no ESPN team becomes an unmatched team
(services/identity, step 5), and its games become separate rows from ESPN's
games, so their odds never reach the card or the forward tests. Nothing here
guesses: ``unmatched_teams`` lists each one with its games and suggestions;
``link_team`` acts only on an explicit (unmatched team, ESPN team) pair, and
only when asked to apply.

Linking:
- the unmatched team's aliases move to the ESPN team, so the provider's name
  resolves there from now on;
- a game that has an ESPN counterpart (the ESPN team and the same opponent,
  within ``MATCH_WINDOW``): its provider event links move to the ESPN game (with
  the home/away orientation recomputed), and the old game is marked DUPLICATE,
  which the card and forward tests skip. Odds already stored stay on the old
  game: they are append-only;
- a game with no ESPN counterpart yet takes the ESPN team in place of the
  unmatched one (ESPN may adopt it later, by teams and time).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, OddsSnapshot, Team, TeamAlias
from ttk.domain import GameStatus
from ttk.services.identity import MATCH_WINDOW, SCHEDULE_AUTHORITY
from ttk.teams import normalize_team_name


@dataclass(frozen=True)
class GameRef:
    game_id: int
    commence_time: str
    status: str
    opponent: str
    odds_rows: int
    espn_counterpart: int | None


@dataclass(frozen=True)
class UnmatchedTeam:
    team_id: int
    sport: str
    name: str
    aliases: list[str]
    games: list[GameRef]
    suggestions: list[tuple[int, str, float]]
    """(team id, name, similarity) of the closest ESPN teams; never applied."""


def _counterpart(session: Session, game: Game, espn_team: int, opponent: int) -> Game | None:
    """The ESPN game between ``espn_team`` and ``opponent`` near this game's time."""
    candidates = session.scalars(
        select(Game).where(
            Game.sport == game.sport,
            Game.id != game.id,
            or_(
                (Game.home_team_id == espn_team) & (Game.away_team_id == opponent),
                (Game.home_team_id == opponent) & (Game.away_team_id == espn_team),
            ),
            Game.commence_time.between(
                game.commence_time - MATCH_WINDOW, game.commence_time + MATCH_WINDOW
            ),
            select(GameSourceId.id)
            .where(GameSourceId.game_id == Game.id, GameSourceId.provider == SCHEDULE_AUTHORITY)
            .exists(),
        )
    ).all()
    return candidates[0] if len(candidates) == 1 else None


def _counterpart_of(session: Session, game_id: int, team_id: int, espn_team: int) -> Game | None:
    g = session.get_one(Game, game_id)
    opponent = g.away_team_id if g.home_team_id == team_id else g.home_team_id
    return _counterpart(session, g, espn_team, opponent)


def _suggest(session: Session, team: Team, names: list[str]) -> list[tuple[int, str, float]]:
    espn = session.execute(
        select(Team.id, Team.name).where(Team.sport == team.sport, Team.espn_id.is_not(None))
    ).all()
    scored = []
    for tid, name in espn:
        target = normalize_team_name(name)
        best = max(
            difflib.SequenceMatcher(None, normalize_team_name(n), target).ratio() for n in names
        )
        scored.append((tid, name, round(best, 3)))
    return sorted(scored, key=lambda x: -x[2])[:3]


def unmatched_teams(session: Session) -> list[UnmatchedTeam]:
    """Every unmatched team that still has aliases or games."""
    out = []
    for team in session.scalars(
        select(Team).where(Team.espn_id.is_(None)).order_by(Team.sport, Team.name)
    ):
        aliases = sorted(
            {
                f"{a.provider}: {a.alias}"
                for a in session.scalars(select(TeamAlias).where(TeamAlias.team_id == team.id))
            }
        )
        games = []
        for g in session.scalars(
            select(Game)
            .where(
                or_(Game.home_team_id == team.id, Game.away_team_id == team.id),
                Game.status != GameStatus.DUPLICATE,  # already linked
            )
            .order_by(Game.commence_time)
        ):
            other_id = g.away_team_id if g.home_team_id == team.id else g.home_team_id
            other = session.get_one(Team, other_id)
            rows = session.scalar(
                select(func.count()).select_from(OddsSnapshot).where(OddsSnapshot.game_id == g.id)
            )
            games.append(
                GameRef(
                    g.id, g.commence_time.isoformat(), g.status, other.name, int(rows or 0), None
                )
            )
        if not aliases and not games:
            continue
        names = [team.name, *(a.split(": ", 1)[1] for a in aliases)]
        suggestions = _suggest(session, team, names)
        if suggestions:
            # Where each game would go if linked to the top suggestion.
            top = suggestions[0][0]
            games = [
                GameRef(
                    r.game_id,
                    r.commence_time,
                    r.status,
                    r.opponent,
                    r.odds_rows,
                    (c.id if (c := _counterpart_of(session, r.game_id, team.id, top)) else None),
                )
                for r in games
            ]
        out.append(UnmatchedTeam(team.id, team.sport, team.name, aliases, games, suggestions))
    return out


@dataclass
class LinkPlan:
    unmatched: str
    espn_team: str
    steps: list[str] = field(default_factory=list)
    applied: bool = False


class LinkError(ValueError):
    """The pair cannot be linked. The message is safe to show the user."""


def link_team(
    session: Session, unmatched_id: int, espn_team_id: int, *, apply: bool = False
) -> LinkPlan:
    """Plan (and with ``apply``, perform) linking an unmatched team to its ESPN
    team. Flushes but never commits: the caller commits."""
    u, e = session.get(Team, unmatched_id), session.get(Team, espn_team_id)
    if u is None or e is None:
        raise LinkError("Team not found")
    if u.espn_id is not None:
        raise LinkError(f"{u.name} already has an ESPN id: it is not unmatched")
    if e.espn_id is None:
        raise LinkError(f"{e.name} has no ESPN id: link to an ESPN team")
    if u.sport != e.sport:
        raise LinkError(f"{u.name} is {u.sport}, {e.name} is {e.sport}")
    plan = LinkPlan(f"{u.name} ({u.sport}, id {u.id})", f"{e.name} (id {e.id}, ESPN {e.espn_id})")

    for alias in session.scalars(select(TeamAlias).where(TeamAlias.team_id == u.id)):
        duplicate = session.scalar(
            select(TeamAlias.id).where(
                TeamAlias.provider == alias.provider,
                TeamAlias.sport == alias.sport,
                TeamAlias.alias == alias.alias,
                TeamAlias.team_id == e.id,
            )
        )
        plan.steps.append(f"alias {alias.provider} '{alias.alias}' -> {e.name}")
        if apply:
            if duplicate is None:
                alias.team_id = e.id
            else:
                session.delete(alias)  # the ESPN team already has this exact alias

    for g in session.scalars(
        select(Game).where(or_(Game.home_team_id == u.id, Game.away_team_id == u.id))
    ):
        opponent = g.away_team_id if g.home_team_id == u.id else g.home_team_id
        provider_home = e.id if g.home_team_id == u.id else g.home_team_id
        target = _counterpart(session, g, e.id, opponent)
        when = f"{g.commence_time:%Y-%m-%d %H:%M} UTC"
        if target is None:
            plan.steps.append(f"game {g.id} ({when}): no ESPN game yet; takes {e.name}")
            if apply:
                if g.home_team_id == u.id:
                    g.home_team_id = e.id
                else:
                    g.away_team_id = e.id
            continue
        links = session.scalars(
            select(GameSourceId).where(
                GameSourceId.game_id == g.id, GameSourceId.provider != SCHEDULE_AUTHORITY
            )
        ).all()
        for link in links:
            swapped = target.home_team_id != provider_home
            plan.steps.append(
                f"game {g.id} ({when}): {link.provider} event {link.source_identifier} -> "
                f"ESPN game {target.id}{' (home/away swapped)' if swapped else ''}"
            )
            if apply:
                link.game_id = target.id
                link.swapped = swapped
        plan.steps.append(f"game {g.id}: marked {GameStatus.DUPLICATE} of {target.id}")
        if apply:
            g.status = GameStatus.DUPLICATE
    if apply:
        session.flush()
        plan.applied = True
    return plan
