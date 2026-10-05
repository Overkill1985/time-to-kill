from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from test_bets import KICK, bet, game_id, poll
from ttk import betting_math as bm
from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import Bet
from ttk.domain import BetResult
from ttk.services.bankroll import (
    EntryKind,
    Policy,
    add_entry,
    bankroll_state,
    check_limits,
    max_allowed,
    save_policy,
    stake_guidance,
)
from ttk.services.bets import BetError, record_bet

T0 = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def wager(
    s: Session,
    placed: datetime,
    stake: float,
    *,
    settled: datetime | None = None,
    profit: float | None = None,
    result: BetResult = BetResult.PENDING,
) -> None:
    s.add(
        Bet(
            placed_at=placed,
            sport="NFL",
            market="SPREAD",
            selection="HOME",
            description="x",
            american_odds=-110,
            stake=stake,
            result=result,
            settled_at=settled,
            profit_loss=profit,
        )
    )
    s.flush()


def test_no_bankroll_means_no_limits(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        state = bankroll_state(s)
        assert not state.configured and max_allowed(state) is None
        assert check_limits(state, 1e6) == []
        b = record_bet(s, bet(gid, stake=5000.0))
    assert b.bankroll_at_bet is None and b.limit_override is None


def test_balance_peak_and_drawdown_from_history(session_factory: sessionmaker[Session]) -> None:
    h = timedelta(hours=1)
    with session_factory() as s:
        add_entry(s, EntryKind.DEPOSIT, 1000, at=T0)
        wager(s, T0 + h, 100, settled=T0 + 2 * h, profit=90.9, result=BetResult.WIN)
        wager(s, T0 + h, 300, settled=T0 + 3 * h, profit=-300, result=BetResult.LOSS)
        add_entry(s, EntryKind.WITHDRAWAL, 200, at=T0 + 4 * h)
        wager(s, T0 + 4 * h, 50)  # still open
        wager(s, T0 + 4 * h, 70, settled=T0 + 4 * h, profit=0, result=BetResult.VOID)

        mid = bankroll_state(s, at=T0 + 2 * h)  # the loss isn't settled yet: still open
        assert mid.balance == pytest.approx(1090.9)
        assert mid.open_exposure == 300 and mid.open_wagers == 1

        now = bankroll_state(s, at=T0 + 5 * h)
    assert now.balance == pytest.approx(590.9)
    assert now.net_deposits == 800 and now.realized_profit == pytest.approx(-209.1)
    # The peak 1090.9 moves down with the withdrawal: only betting is a drawdown.
    assert now.peak == pytest.approx(890.9)
    assert now.drawdown == pytest.approx(590.9 / 890.9 - 1)
    assert now.open_exposure == 50 and now.available == pytest.approx(540.9)
    assert now.staked_today == pytest.approx(450)  # voided stakes don't count


def test_limits_and_override(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        add_entry(s, EntryKind.DEPOSIT, 1000, at=KICK - timedelta(days=2))
        with pytest.raises(BetError, match="over 3.0% of the bankroll"):
            record_bet(s, bet(gid, stake=100.0))
        ok = record_bet(s, bet(gid, stake=30.0))
        assert ok.bankroll_at_bet == 1000 and ok.limit_override is None
        forced = record_bet(s, bet(gid, stake=100.0, limit_override="testing a big one"))
    assert forced.limit_override is not None
    assert "over 3.0%" in forced.limit_override
    assert forced.limit_override.endswith("Reason: testing a big one")


def test_daily_open_and_stop_limits(session_factory: sessionmaker[Session]) -> None:
    at = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # 6 PM Eastern
    with session_factory() as s:
        add_entry(s, EntryKind.DEPOSIT, 1000, at=at - timedelta(days=3))
        save_policy(s, Policy(max_stake_fraction=0.5, max_daily_fraction=0.1))
        wager(s, at - timedelta(hours=23), 60)  # yesterday, Eastern: open, not today
        wager(s, at - timedelta(hours=2), 70)
        state = bankroll_state(s, at=at)
        assert state.staked_today == 70 and state.open_exposure == 130
        assert max_allowed(state) == pytest.approx(30)  # daily: 100 - 70
        assert [b.limit for b in check_limits(state, 40)] == ["daily"]
        assert [b.limit for b in check_limits(state, 80)] == ["daily", "open"]

        wager(
            s,
            at - timedelta(days=1),
            300,
            settled=at - timedelta(hours=1),
            profit=-300,
            result=BetResult.LOSS,
        )
        stopped = bankroll_state(s, at=at)
    assert stopped.drawdown == pytest.approx(-0.3)
    assert max_allowed(stopped) == 0
    assert "stop" in [b.limit for b in check_limits(stopped, 1)]


def test_guidance_only_for_qualified_and_within_limits(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        add_entry(s, EntryKind.DEPOSIT, 1000, at=T0)
        state = bankroll_state(s, at=T0 + timedelta(hours=1))
    d = bm.american_to_decimal(-110)
    small = stake_guidance(state, 0.55, d, qualified=True)
    assert small.full_kelly_fraction == pytest.approx(bm.kelly_fraction(0.55, d))
    assert small.recommended == pytest.approx(1000 * small.full_kelly_fraction * 0.25)
    capped = stake_guidance(state, 0.70, d, qualified=True)
    assert capped.kelly_stake > 30 and capped.recommended == pytest.approx(30)
    assert "capped" in capped.reason
    lean = stake_guidance(state, 0.70, d, qualified=False)
    assert lean.recommended == 0 and "QUALIFIED" in lean.reason
    # P(win | no push) with pushes: the push share is taken out before Kelly.
    pushy = stake_guidance(state, 0.55, d, push_probability=0.1, qualified=True)
    assert pushy.full_kelly_fraction == pytest.approx(
        bm.kelly_fraction(0.55 * 0.9, d, push_probability=0.1)
    )


def test_entries_and_policies_are_append_only(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        add_entry(s, EntryKind.DEPOSIT, 1000)
        save_policy(s, Policy())
        s.commit()
        for table in ("bankroll_entries", "bankroll_policies"):
            with pytest.raises(DatabaseError, match="append-only"):
                s.execute(text(f"DELETE FROM {table}"))
            s.rollback()


def test_entry_and_policy_validation(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        with pytest.raises(ValueError, match="positive"):
            add_entry(s, EntryKind.WITHDRAWAL, -5)
        with pytest.raises(ValueError, match="Kelly"):
            save_policy(s, Policy(kelly_multiplier=1.5))
        adj = add_entry(s, EntryKind.ADJUSTMENT, -12.5)
    assert adj.amount == -12.5


def test_bankroll_api(database_url: str) -> None:
    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    assert client.get("/api/bankroll").json()["configured"] is False
    r = client.post("/api/bankroll/entries", json={"kind": "DEPOSIT", "amount": 500})
    assert r.status_code == 201 and r.json()["balance"] == 500
    assert (
        client.post("/api/bankroll/entries", json={"kind": "DEPOSIT", "amount": 0}).status_code
        == 422
    )
    policy = {
        "kelly_multiplier": 0.5,
        "max_stake_fraction": 0.05,
        "max_daily_fraction": 0.2,
        "max_open_fraction": 0.3,
        "stop_drawdown_fraction": 0.2,
    }
    r = client.put("/api/bankroll/policy", json=policy)
    assert r.json()["max_allowed"] == pytest.approx(25)
    body = client.get("/api/bankroll").json()
    assert body["policy"]["kelly_multiplier"] == 0.5 and len(body["entries"]) == 1
    g = client.post(
        "/api/bankroll/guidance",
        json={"model_probability": 0.6, "american_odds": -110, "qualified": False},
    ).json()
    assert g["recommended"] == 0 and g["max_allowed"] == pytest.approx(25)
