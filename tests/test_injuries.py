import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun, InjuryReport, Team
from ttk.domain import Sport
from ttk.providers.espn_injuries import EspnInjuries, InjuryEntry, parse_injuries
from ttk.services.collector import refresh_injuries
from ttk.services.injury_ingest import injuries_at, store_injuries

# Real ESPN injury lists (trimmed), captured 2026-09-27.
FIX = json.loads((Path(__file__).parent / "fixtures" / "espn_injuries.json").read_text("utf-8"))
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_parse_injuries() -> None:
    nba = parse_injuries(FIX["NBA"])
    veesaar = next(e for e in nba if e.player_name == "Henri Veesaar")
    assert (veesaar.team_espn_id, veesaar.player_id, veesaar.status) == ("1", "5105571", "Out")
    assert veesaar.injury_type == "Knee" and veesaar.return_date == "2027-07-01"
    assert veesaar.updated_at == datetime(2026, 9, 21, 19, 50, tzinfo=UTC)
    nfl = parse_injuries(FIX["NFL"])
    assert {e.status for e in nfl} == {"Questionable", "Active"}
    active = next(e for e in nfl if e.status == "Active")
    assert active.injury_type is None and active.return_date is None
    # Never guess who a report is about: no athlete link, no row.
    broken = {"injuries": [{"id": "1", "injuries": [{"status": "Out", "athlete": {}}]}]}
    assert parse_injuries(broken) == []


def entry(pid: str, status: str = "Out", team: str = "1", **kw: object) -> InjuryEntry:
    base = InjuryEntry(team, pid, f"Player {pid}", status, "Knee", "note", None, T0)
    return replace(base, **kw)  # type: ignore[arg-type]


def rows(session: Session) -> list[tuple[str | None, str, bool]]:
    return [
        (r.player_source_identifier, r.status, r.cleared)
        for r in session.scalars(select(InjuryReport).order_by(InjuryReport.id))
    ]


def test_change_log(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(Team(sport=Sport.NBA, name="Atlanta Hawks", espn_id="1"))
        session.flush()
        s1 = store_injuries(session, Sport.NBA, [entry("a"), entry("b", "Day-To-Day")], T0)
        assert (s1["new"], s1["unmatched_team"]) == (2, 0)
        t1 = T0 + timedelta(minutes=15)
        s2 = store_injuries(session, Sport.NBA, [entry("a"), entry("b", "Day-To-Day")], t1)
        assert s2["unchanged"] == 2 and s2["new"] == s2["changed"] == 0
        t2 = T0 + timedelta(minutes=30)
        s3 = store_injuries(session, Sport.NBA, [entry("b", "Out", team="999")], t2)
        assert (s3["changed"], s3["cleared"], s3["unmatched_team"]) == (1, 1, 1)
        t3 = T0 + timedelta(minutes=45)
        s4 = store_injuries(session, Sport.NBA, [], t3)
        assert s4["empty_feed_ignored"] == 1  # a blank feed is a glitch, not mass healing
        session.commit()
        assert rows(session) == [
            ("a", "Out", False),
            ("b", "Day-To-Day", False),
            ("b", "Out", False),
            ("a", "Out", True),
        ]
        # What was known when: no leakage from later observations.
        assert set(injuries_at(session, Sport.NBA, T0 - timedelta(seconds=1))) == set()
        at_t1 = injuries_at(session, Sport.NBA, t1)
        assert {p: i.status for p, i in at_t1.items()} == {"a": "Out", "b": "Day-To-Day"}
        at_t2 = injuries_at(session, Sport.NBA, t2)
        assert {p: i.status for p, i in at_t2.items()} == {"b": "Out"}
        assert at_t2["b"].team_id is None  # team 999 is unknown: counted, not guessed
        a_back = store_injuries(session, Sport.NBA, [entry("a"), entry("b", "Out", team="999")], t3)
        assert a_back["new"] == 1  # returning to the list after a clear is a new report
        session.commit()


def test_injury_reports_are_append_only(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        store_injuries(session, Sport.NBA, [entry("a")], T0)
        session.commit()
        row = session.scalars(select(InjuryReport)).one()
        row.status = "Available"
        with pytest.raises(DatabaseError):
            session.commit()


def test_refresh_injuries_cadence(session_factory: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=FIX["NBA"] if "nba" in request.url.path else FIX["NFL"])

    source = EspnInjuries(client=httpx.Client(transport=httpx.MockTransport(handle)), backoff=0)
    every = {Sport.NBA: timedelta(minutes=10), Sport.NFL: timedelta(minutes=55)}
    first = refresh_injuries(session_factory, source, now=T0, every=every)
    assert [(r.sport, r.status) for r in first] == [("NBA", "SUCCESS"), ("NFL", "SUCCESS")]
    with session_factory() as session:
        last = session.scalar(select(func.max(IngestionRun.finished_at)))
    assert last is not None
    # finished_at is wall-clock; step "now" past it so cadence is measured from there.
    later = refresh_injuries(session_factory, source, now=last + timedelta(minutes=20), every=every)
    assert [r.sport for r in later] == ["NBA"]  # NFL waits for its hour
    assert len(calls) == 3
    with session_factory() as session:
        expected = len(parse_injuries(FIX["NBA"])) + len(parse_injuries(FIX["NFL"]))
        # the second NBA pass saw no changes, so it wrote nothing
        assert session.scalar(select(func.count()).select_from(InjuryReport)) == expected == 10
