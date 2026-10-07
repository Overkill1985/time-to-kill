from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, Team, TeamAlias
from ttk.domain import GameStatus, Sport
from ttk.providers.base import TeamRef
from ttk.services.identity import resolve_team
from ttk.services.team_review import LinkError, link_team, unmatched_teams

T = datetime(2026, 10, 22, 2, 40, tzinfo=UTC)


def seed(sf: sessionmaker[Session]) -> dict[str, int]:
    with sf() as s:
        espn = Team(sport="NBA", name="LA Clippers", espn_id="12")
        kings = Team(sport="NBA", name="Sacramento Kings", espn_id="23")
        lakers = Team(sport="NBA", name="Los Angeles Lakers", espn_id="13")
        stray = Team(sport="NBA", name="Los Angeles Clippers")
        s.add_all([espn, kings, lakers, stray])
        s.flush()
        s.add(
            TeamAlias(
                provider="propline",
                sport="NBA",
                alias="Los Angeles Clippers",
                normalized="los angeles clippers",
                team_id=stray.id,
            )
        )
        real = Game(sport="NBA", home_team_id=kings.id, away_team_id=espn.id, commence_time=T)
        dup = Game(
            sport="NBA",
            home_team_id=stray.id,
            away_team_id=kings.id,
            commence_time=T + timedelta(minutes=10),
        )
        lone = Game(
            sport="NBA",
            home_team_id=stray.id,
            away_team_id=lakers.id,
            commence_time=T + timedelta(days=9),
        )
        s.add_all([real, dup, lone])
        s.flush()
        s.add_all(
            [
                GameSourceId(provider="espn", source_identifier="401", game_id=real.id),
                GameSourceId(provider="propline", source_identifier="330733", game_id=dup.id),
                GameSourceId(provider="propline", source_identifier="291887", game_id=lone.id),
            ]
        )
        s.commit()
        return {
            "espn": espn.id,
            "stray": stray.id,
            "real": real.id,
            "dup": dup.id,
            "lone": lone.id,
            "lakers": lakers.id,
        }


def test_review_lists_games_and_suggestions_without_applying(
    session_factory: sessionmaker[Session],
) -> None:
    ids = seed(session_factory)
    with session_factory() as s:
        (t,) = unmatched_teams(s)
        assert t.team_id == ids["stray"] and t.aliases == ["propline: Los Angeles Clippers"]
        assert {g.game_id for g in t.games} == {ids["dup"], ids["lone"]}
        assert {tid for tid, _, _ in t.suggestions} >= {ids["espn"]}
        assert s.get_one(TeamAlias, 1).team_id == ids["stray"]  # nothing changed


def test_link_moves_links_marks_duplicates_and_needs_apply(
    session_factory: sessionmaker[Session],
) -> None:
    ids = seed(session_factory)
    with session_factory() as s:
        plan = link_team(s, ids["stray"], ids["espn"])
        assert not plan.applied and any("swapped" in step for step in plan.steps)
        s.rollback()
        assert s.get_one(Game, ids["dup"]).status == GameStatus.SCHEDULED  # dry run

        link_team(s, ids["stray"], ids["espn"], apply=True)
        s.commit()
        link = s.scalars(
            select(GameSourceId).where(GameSourceId.source_identifier == "330733")
        ).one()
        # PropLine had the Clippers at home; ESPN has the Kings: flipped on ingestion.
        assert link.game_id == ids["real"] and link.swapped
        assert s.get_one(Game, ids["dup"]).status == GameStatus.DUPLICATE
        lone = s.get_one(Game, ids["lone"])
        assert lone.home_team_id == ids["espn"] and lone.status == GameStatus.SCHEDULED
        team = resolve_team(
            s,
            provider="propline",
            sport=Sport.NBA,
            ref=TeamRef("Los Angeles Clippers"),
            stats=Counter(),
        )
        assert team.id == ids["espn"]
        assert unmatched_teams(s) == []


def test_link_refuses_wrong_pairs(session_factory: sessionmaker[Session]) -> None:
    ids = seed(session_factory)
    with session_factory() as s:
        with pytest.raises(LinkError, match="not unmatched"):
            link_team(s, ids["espn"], ids["lakers"])
        with pytest.raises(LinkError, match="no ESPN id"):
            link_team(s, ids["stray"], ids["stray"])


def test_curated_alias_resolves_a_new_name(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        s.add(Team(sport="NFL", name="New Orleans Saints", espn_id="18"))
        s.commit()
        team = resolve_team(
            s, provider="newfeed", sport=Sport.NFL, ref=TeamRef("NOLA Saints"), stats=Counter()
        )
        assert team.espn_id == "18"
