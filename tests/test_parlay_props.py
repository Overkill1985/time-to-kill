from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from test_player_props import KICK, seed
from ttk.db.models import Bet, Game, NflPlayerGameStat, PropPull, PropQuote, Sportsbook
from ttk.domain import BetResult, CorrelationRisk, GameStatus, Market, Selection
from ttk.services.parlay_lab import (
    LegInput,
    ParlayError,
    evaluate_parlay,
    save_parlay,
    settle_parlays,
)

AT = KICK - timedelta(minutes=30)
HURTS = "Jalen Hurts (PHI)"
BROWN = "A.J. Brown (PHI)"


def add_brown(sf: sessionmaker[Session], game_id: int) -> None:
    """A.J. Brown receptions 5.5 at two books, and his receiving yards at one."""
    with sf() as s:
        pull = s.scalars(select(PropPull).where(PropPull.game_id == game_id)).one()
        dk = s.scalars(select(Sportsbook).where(Sportsbook.key == "draftkings")).one()
        fd = s.scalars(select(Sportsbook).where(Sportsbook.key == "fanduel")).one()
        for book, over, under in ((dk, -120, -100), (fd, -125, +105)):
            for sel, odds in (("Over", over), ("Under", under)):
                s.add(
                    PropQuote(
                        pull_id=pull.id,
                        sportsbook_id=book.id,
                        market="player_receptions",
                        player=BROWN,
                        team_side="HOME",
                        selection=sel,
                        point=5.5,
                        american_odds=odds,
                    )
                )
        for sel, odds in (("Over", -110), ("Under", -110)):
            s.add(
                PropQuote(
                    pull_id=pull.id,
                    sportsbook_id=dk.id,
                    market="player_reception_yds",
                    player=BROWN,
                    team_side="HOME",
                    selection=sel,
                    point=70.5,
                    american_odds=odds,
                )
            )
        s.commit()


def legs(game_id: int) -> list[LegInput]:
    return [
        LegInput(
            game_id, Market.PLAYER_PROP, Selection.OVER, 245.5, player=HURTS, prop="player_pass_yds"
        ),
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.UNDER,
            5.5,
            player=BROWN,
            prop="player_receptions",
        ),
    ]


def test_prop_legs_are_priced_from_the_pull_at_the_market_chance(
    session_factory: sessionmaker[Session],
) -> None:
    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    with session_factory() as s:
        a = evaluate_parlay(s, legs(game_id), "draftkings", at=AT)
    hurts, brown = a.legs
    assert (hurts.american_odds, hurts.price_source, hurts.market_books) == (-110, "book", 3)
    assert (
        hurts.probability == pytest.approx(0.5, abs=0.01) and hurts.probability_source == "market"
    )
    assert brown.description == "A.J. Brown (PHI) Under 5.5 receptions"
    assert brown.american_odds == -100 and brown.probability < 0.5  # the books lean over
    # Two players in one game: linked by game script, not simulated.
    assert a.correlations[0].risk is CorrelationRisk.MODERATE and not a.simulated
    assert any("no tested model" in w for w in a.warnings)
    assert any("props pull 0.5 h ago" in w for w in a.warnings)


def test_same_player_props_are_high_risk_and_both_sides_are_refused(
    session_factory: sessionmaker[Session],
) -> None:
    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    two_props = [
        LegInput(
            game_id, Market.PLAYER_PROP, Selection.OVER, 5.5, player=BROWN, prop="player_receptions"
        ),
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.OVER,
            70.5,
            player=BROWN,
            prop="player_reception_yds",
        ),
    ]
    with session_factory() as s:
        a = evaluate_parlay(s, two_props, "draftkings", at=AT)
        assert a.correlation_risk is CorrelationRisk.HIGH
        both = [
            two_props[0],
            LegInput(
                game_id,
                Market.PLAYER_PROP,
                Selection.UNDER,
                5.5,
                player=BROWN,
                prop="player_receptions",
            ),
        ]
        with pytest.raises(ParlayError, match="both sides of one prop"):
            evaluate_parlay(s, both, "draftkings", at=AT)
        with pytest.raises(ParlayError, match="does not quote"):
            evaluate_parlay(s, two_props, "betmgm", at=AT)  # betmgm has no Brown props


def test_saved_prop_parlay_settles_from_player_stats(
    session_factory: sessionmaker[Session],
) -> None:
    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    with session_factory() as s:
        parlay = save_parlay(s, legs(game_id), "draftkings", 10.0, placed_at=AT)
        s.commit()
        parlay_id = parlay.id
        stored = s.scalars(select(Bet).where(Bet.parlay_id == parlay_id).order_by(Bet.id)).all()
        assert [(b.player, b.prop_market, b.market) for b in stored] == [
            (HURTS, "player_pass_yds", "PLAYER_PROP"),
            (BROWN, "player_receptions", "PLAYER_PROP"),
        ]
    with session_factory() as s:  # final, but no player stats yet: wait
        game = s.get_one(Game, game_id)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 24, 20
        s.commit()
        assert settle_parlays(s) == []
    with session_factory() as s:  # Hurts throws 268 (over 245.5); Brown has no stat line
        game = s.get_one(Game, game_id)
        hurts = s.scalars(
            select(NflPlayerGameStat).where(NflPlayerGameStat.player_name == "Jalen Hurts")
        ).first()
        assert hurts is not None
        s.add(
            NflPlayerGameStat(
                game_id=game.id,
                team_id=game.home_team_id,
                player_id=hurts.player_id,
                player_name="Jalen Hurts",
                position="QB",
                completions=22,
                attempts=31,
                passing_yards=268,
                passing_tds=2,
                interceptions=0,
                carries=5,
                rushing_yards=20,
                rushing_tds=0,
                targets=0,
                receptions=0,
                receiving_yards=0,
                receiving_tds=0,
            )
        )
        s.commit()
        # Brown has no NFL stat line: inactive, or played without one. Wait for a hand.
        assert settle_parlays(s) == []
        brown = s.scalars(select(Bet).where(Bet.parlay_id == parlay_id, Bet.player == BROWN)).one()
        brown.result = BetResult.VOID  # what PATCH /api/bets/{id} {"result": "VOID"} does
        s.commit()
        settled = settle_parlays(s)
        s.commit()
        assert [p.id for p in settled] == [parlay_id]
        results = {
            b.player: (b.result, b.actual_value)
            for b in s.scalars(select(Bet).where(Bet.parlay_id == parlay_id))
        }
        assert results == {HURTS: (BetResult.WIN, 268.0), BROWN: (BetResult.VOID, None)}
        assert settled[0].result == BetResult.WIN  # the void leg drops out
        assert settled[0].profit_loss == pytest.approx(10.0 * (1 / 1.1))  # -110 alone


def test_api_settles_a_prop_leg_by_hand_and_lists_props(
    database_url: str, session_factory: sessionmaker[Session]
) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    with session_factory() as s:
        parlay = save_parlay(s, legs(game_id), "draftkings", 10.0, placed_at=AT)
        s.commit()
        leg_ids = [b.id for b in s.scalars(select(Bet).where(Bet.parlay_id == parlay.id))]
        single = Bet(
            sport="NFL",
            game_id=game_id,
            market="SPREAD",
            selection="HOME",
            description="a single",
            line=-3.5,
            american_odds=-110,
            result="PENDING",
        )
        s.add(single)
        s.commit()
        single_id = single.id
    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    ok = client.patch(f"/api/bets/{leg_ids[1]}", json={"result": "LOSS", "actual_value": 0})
    assert ok.status_code == 200
    again = client.patch(f"/api/bets/{leg_ids[1]}", json={"result": "WIN"})
    assert again.status_code == 409  # already settled
    assert client.patch(f"/api/bets/{single_id}", json={"result": "WIN"}).status_code == 409
    listed = client.get("/api/parlays").json()[0]["legs"]
    assert [leg["market"] for leg in listed] == ["PLAYER_PROP", "PLAYER_PROP"]
    assert listed[1]["result"] == "LOSS" and listed[1]["game_final"] is False
    props = client.get("/api/props", params={"date": "2026-10-18"}).json()
    assert {p["player"] for p in props["props"]} == {"Jalen Hurts", "A.J. Brown"}


def test_a_prop_leg_matches_the_player_however_a_book_spells_him(
    session_factory: sessionmaker[Session],
) -> None:
    # The books in seed() list "Jalen Hurts (PHI)"; others write "Jalen Hurts" or
    # "Jalen Hurts Jr." - a leg added under any spelling prices against all of them.
    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    spelled = [
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.OVER,
            245.5,
            player="Jalen Hurts",
            prop="player_pass_yds",
        ),
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.UNDER,
            5.5,
            player="AJ Brown",
            prop="player_receptions",
        ),
    ]
    with session_factory() as s:
        a = evaluate_parlay(s, spelled, "draftkings", at=AT)
    assert [leg.american_odds for leg in a.legs] == [-110, -100]
    assert a.legs[0].market_books == 3 and a.legs[1].market_books == 2


def add_milestones(sf: sessionmaker[Session], game_id: int) -> None:
    """Hurts' 200+ and 250+ passing-yard rungs: one-sided ladders at three books, plus
    one book's two-sided 199.5 line (spelled without the team code)."""
    with sf() as s:
        pull = s.scalars(select(PropPull).where(PropPull.game_id == game_id)).one()
        books = {b.key: b for b in s.scalars(select(Sportsbook))}

        def q(
            book: str, sel: str, odds: float, point: float | None = None, name: str = HURTS
        ) -> PropQuote:
            return PropQuote(
                pull_id=pull.id,
                sportsbook_id=books[book].id,
                market="player_pass_yds",
                player=name,
                team_side="HOME",
                selection=sel,
                point=point,
                american_odds=odds,
            )

        s.add_all(
            [
                q("draftkings", "200+ Passing Yards", -400),
                q("fanduel", "200+ Passing Yards", -350),
                q("betmgm", "200+ Passing Yards", -500),
                q("draftkings", "250+ Passing Yards", +105),
                q("fanduel", "250+ Passing Yards", +110),
                q("betmgm", "300+ Passing Yards", +300),  # one book only: not listed
                q("fanduel", "Under", +250, 199.5, "Jalen Hurts"),
            ]
        )
        s.commit()


def test_milestone_rows_have_rungs_hit_rates_and_implied_chance(
    session_factory: sessionmaker[Session],
) -> None:
    from ttk.services.player_props import props_for_games, upcoming_games

    game_id = seed(session_factory)
    add_milestones(session_factory, game_id)
    with session_factory() as s:
        games = upcoming_games(s, KICK - timedelta(days=1), KICK + timedelta(days=1))
        rows = props_for_games(s, games, milestones=True)
    by = {(r.player, r.milestone): r for r in rows}
    assert set(by) == {("Jalen Hurts", 200), ("Jalen Hurts", 250)}
    two_hundred = by[("Jalen Hurts", 200)]
    assert two_hundred.line == 199.5 and two_hundred.books == 3
    assert two_hundred.best_over is not None and two_hundred.best_over.american == -350
    assert two_hundred.implied_over == pytest.approx(0.8, abs=0.001)  # median of 80/78/83%
    assert two_hundred.over_probability is not None  # fanduel posts both sides at 199.5
    # His last 10 (310 down to 220) all cleared 200; 7 of 10 cleared 250.
    assert two_hundred.last10 is not None and two_hundred.last10.over == 10
    assert by[("Jalen Hurts", 250)].last10.over == 7  # type: ignore[union-attr]
    assert by[("Jalen Hurts", 250)].over_probability is None  # one-sided: implied only


def test_a_milestone_leg_prices_off_the_ladder(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    add_milestones(session_factory, game_id)
    legs_ = [
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.OVER,
            249.5,
            player="Jalen Hurts",
            prop="player_pass_yds",
        ),
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.UNDER,
            5.5,
            player=BROWN,
            prop="player_receptions",
        ),
    ]
    with session_factory() as s:
        a = evaluate_parlay(s, legs_, "draftkings", at=AT)
        hurts = a.legs[0]
        assert (hurts.american_odds, hurts.probability_source) == (105, "implied")
        assert hurts.probability == pytest.approx((100 / 205 + 100 / 210) / 2, abs=0.001)
        assert any("one-sided" in w for w in a.warnings)
        with pytest.raises(ParlayError, match="does not quote"):  # no under on a milestone
            evaluate_parlay(
                s,
                [
                    LegInput(
                        game_id,
                        Market.PLAYER_PROP,
                        Selection.UNDER,
                        249.5,
                        player="Jalen Hurts",
                        prop="player_pass_yds",
                    ),
                    legs_[1],
                ],
                "draftkings",
                at=AT,
            )


def test_touchdown_props_are_milestones_and_price_as_legs(
    session_factory: sessionmaker[Session],
) -> None:
    from ttk.services.player_props import props_for_games, upcoming_games

    game_id = seed(session_factory)
    add_brown(session_factory, game_id)
    with session_factory() as s:
        pull = s.scalars(select(PropPull).where(PropPull.game_id == game_id)).one()
        books = {b.key: b for b in s.scalars(select(Sportsbook))}

        def q(
            book: str, market: str, sel: str, odds: float, point: float | None = None
        ) -> PropQuote:
            return PropQuote(
                pull_id=pull.id,
                sportsbook_id=books[book].id,
                market=market,
                player=HURTS,
                team_side="HOME",
                selection=sel,
                point=point,
                american_odds=odds,
            )

        s.add_all(
            [
                q("draftkings", "player_anytime_td", "Yes", +110),
                q("draftkings", "player_anytime_td", "No", -140),
                q("fanduel", "player_anytime_td", HURTS, +120),  # theScore-style "yes"
                q("betmgm", "player_anytime_td", "Over", +115, 0.5),
                q("draftkings", "player_2plus_td", "Yes", +450),
                q("fanduel", "player_2plus_td", HURTS, +500),
            ]
        )
        s.commit()
        games = upcoming_games(s, KICK - timedelta(days=1), KICK + timedelta(days=1))
        rungs = {
            (r.market, r.milestone): r
            for r in props_for_games(s, games, milestones=True)
            if r.market == "player_anytime_td"
        }
        main = next(r for r in props_for_games(s, games) if r.market == "player_anytime_td")
    assert set(rungs) == {("player_anytime_td", 1), ("player_anytime_td", 2)}
    assert rungs[("player_anytime_td", 1)].books == 3  # all three spellings of "yes"
    assert rungs[("player_anytime_td", 2)].best_over.american == 500  # type: ignore[union-attr]
    assert main.line == 0.5 and main.over_probability is not None  # draftkings yes/no
    two_tds = [
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.OVER,
            1.5,
            player="Jalen Hurts",
            prop="player_anytime_td",
        ),
        LegInput(
            game_id,
            Market.PLAYER_PROP,
            Selection.UNDER,
            5.5,
            player=BROWN,
            prop="player_receptions",
        ),
    ]
    with session_factory() as s:
        a = evaluate_parlay(s, two_tds, "draftkings", at=AT)
    assert a.legs[0].american_odds == 450 and a.legs[0].probability_source == "implied"
