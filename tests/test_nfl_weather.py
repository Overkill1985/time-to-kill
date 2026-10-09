from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from test_forward import KICK
from ttk.db.models import ForwardPrediction, GameSourceId, IngestionRun, WeatherForecast
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import (
    NormalizedGame,
    NormalizedOddsQuote,
    OddsFetch,
    ProviderError,
    TeamRef,
)
from ttk.providers.open_meteo import HourlyForecast, kickoff_hour, parse_hour
from ttk.research.nfl_weather import (
    STADIUMS,
    Venue,
    WindModel,
    WindRow,
    backtest,
    fit,
    freeze,
    outdoor_stadium,
    thaw,
    validate,
    wind_rows,
)
from ttk.services.forward_models import _nfl_wind_model
from ttk.services.forward_test import register, snapshot
from ttk.services.odds_ingest import store_odds
from ttk.services.weather import WeatherState


def test_kickoff_hour_and_parse() -> None:
    assert kickoff_hour(datetime(2026, 10, 11, 17, 25, tzinfo=UTC)) == datetime(
        2026, 10, 11, 17, tzinfo=UTC
    )
    assert kickoff_hour(datetime(2026, 10, 11, 17, 30, tzinfo=UTC)) == datetime(
        2026, 10, 11, 18, tzinfo=UTC
    )
    payload = {
        "hourly": {
            "time": ["2026-10-11T17:00", "2026-10-11T18:00"],
            "wind_speed_10m": [9.0, 14.5],
            "wind_gusts_10m": [20.0, None],
            "temperature_2m": [50.0, 49.0],
            "precipitation": [0.0, 0.2],
        }
    }
    f = parse_hour(payload, datetime(2026, 10, 11, 18, tzinfo=UTC))
    assert (f.wind_mph, f.gust_mph, f.temperature_f, f.precipitation_mm) == (14.5, None, 49.0, 0.2)
    with pytest.raises(ProviderError):
        parse_hour(payload, datetime(2026, 10, 11, 19, tzinfo=UTC))


def row(season: int, roof: str = "outdoors", wind: str = "10", total: str = "40") -> dict[str, str]:
    return {
        "game_id": f"{season}_01_A_B",
        "season": str(season),
        "game_type": "REG",
        "roof": roof,
        "wind": wind,
        "total": total,
        "total_line": "44",
        "over_odds": "-110",
        "under_odds": "-110",
        "stadium_id": "GNB00",
    }


def training_rows() -> list[dict[str, str]]:
    return [
        row(s, wind=str(w), total=str(44 - w // 5 + d))
        for s in (2010, 2019)
        for w in (0, 10, 20)
        for d in (-6, 6)
    ]


def test_wind_rows_keep_finished_outdoor_regular_season_games() -> None:
    rows = [
        row(2010),
        row(2010, roof="dome"),
        row(2010, roof="closed"),
        row(2010, wind="NA"),
        row(2026, total="NA"),  # not played yet
        {**row(2010), "game_type": "WC"},
    ]
    assert [r.season for r in wind_rows(rows)] == [2010]


def test_fit_recovers_the_wind_effect_and_centers_it() -> None:
    rows = [
        WindRow(2005, 44.0, 44.0 - 0.2 * (w - 8.0) + noise, float(w), -110, -110)
        for w in range(0, 17)
        for noise in (-7.0, 7.0)
    ]
    m = fit(rows)
    assert m.wind_coef == pytest.approx(-0.2)
    assert m.wind_ref == pytest.approx(8.0)
    assert m.adjustment(8.0) == pytest.approx(0.0)
    assert m.sigma == pytest.approx(7.0, rel=0.05)
    v = validate(m, rows)
    assert v.rmse_model < v.rmse_line and v.priced == len(rows)


def test_p_over_leans_under_in_wind_and_pushes_on_whole_lines() -> None:
    m = WindModel(-0.1, 8.0, 13.0)
    assert m.p_over(44.5, 8.0) == pytest.approx(0.5)
    assert m.p_over(44.5, 25.0) < 0.5 < m.p_over(44.5, 0.0)
    assert m.push(44.5, 25.0) == 0.0 and m.push(44.0, 25.0) > 0.0


def test_artifact_round_trip() -> None:
    rows = training_rows()
    result = backtest(rows)
    artifact = freeze(result)
    assert artifact["market"] == "TOTAL" and artifact["kind"] == "nfl_wind"
    assert thaw(artifact) == result.model
    with pytest.raises(ValueError):
        thaw({**artifact, "artifact_version": 99})


def test_only_outdoor_stadiums_with_coordinates_are_forecast() -> None:
    assert outdoor_stadium(Venue("GNB00", "Lambeau Field", "outdoors")) == (
        "GNB00",
        STADIUMS["GNB00"],
    )
    assert outdoor_stadium(Venue("DET00", "Ford Field", "dome")) is None
    # Retractable roof, not yet classified:
    assert outdoor_stadium(Venue("ATL97", "Mercedes-Benz Stadium", "")) is None
    assert outdoor_stadium(Venue("XXX00", "Somewhere Field", "outdoors")) is None
    assert outdoor_stadium(None) is None
    # nflverse filed this London game under Jacksonville's id; the name decides.
    london = outdoor_stadium(Venue("JAX00", "Tottenham Hotspur Stadium", "outdoors"))
    assert london == ("LON02", STADIUMS["LON02"])


class FakeForecaster:
    name = "fake-weather"

    def __init__(self) -> None:
        self.calls: list[tuple[float, float, datetime]] = []

    def forecast(self, latitude: float, longitude: float, at: datetime) -> HourlyForecast:
        self.calls.append((latitude, longitude, at))
        return HourlyForecast(kickoff_hour(at), 22.0, 30.0, 40.0, 0.0)


def nfl_game(sf: sessionmaker[Session], observed: datetime, event: str, nflverse_id: str) -> int:
    game = NormalizedGame("espn", event, Sport.NFL, TeamRef("Bears"), TeamRef("Lions"), KICK, None)

    def q(sel: Selection, price: float, book: str) -> NormalizedOddsQuote:
        return NormalizedOddsQuote("fake", event, book, book, Market.TOTAL, sel, 44.5, price, None)

    quotes = [
        q(Selection.OVER, -110, "draftkings"),
        q(Selection.UNDER, -110, "draftkings"),
        q(Selection.OVER, -108, "fanduel"),
        q(Selection.UNDER, -112, "fanduel"),
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
        s.add(run)
        s.flush()
        store_odds(s, OddsFetch([game], quotes, {}), run=run, observed_at=observed, stats=Counter())
        gid = s.scalar(select(GameSourceId.game_id).where(GameSourceId.source_identifier == event))
        assert gid is not None
        s.add(GameSourceId(provider="nflverse", source_identifier=nflverse_id, game_id=gid))
        s.commit()
        return gid


def test_forward_snapshot_takes_one_forecast_per_horizon(
    session_factory: sessionmaker[Session],
) -> None:
    now = KICK - timedelta(hours=20)
    outdoor = nfl_game(session_factory, now - timedelta(minutes=5), "e1", "2026_05_CHI_GB")
    nfl_game(session_factory, now - timedelta(minutes=5), "e2", "2026_05_MIN_DET")
    loads = []

    def load_rows() -> list[dict[str, str]]:
        loads.append(1)
        return [
            {
                "game_id": "2026_05_CHI_GB",
                "stadium_id": "GNB00",
                "stadium": "Lambeau Field",
                "roof": "outdoors",
            },
            {
                "game_id": "2026_05_MIN_DET",
                "stadium_id": "DET00",
                "stadium": "Ford Field",
                "roof": "dome",
            },
        ]

    forecaster = FakeForecaster()
    weather = WeatherState(load_rows, forecaster)
    rows = training_rows()
    with session_factory() as s:
        version = register(
            s,
            name="nfl-total-wind",
            version="1",
            sport=Sport.NFL,
            algorithm="test",
            features=["forecast_wind_mph"],
            artifact=freeze(backtest(rows)),
            training_window="2002-2017",
            validation_window="2018-2021",
            log_loss=None,
            brier=None,
        )
        model = _nfl_wind_model(version, weather)
        stats = snapshot(s, [model], now=now)
        s.commit()
        assert stats.written == 1  # the dome game gets no prediction
        fp = s.scalars(select(ForwardPrediction)).one()
        assert fp.game_id == outdoor and fp.market == Market.TOTAL and fp.home_line == 44.5
        assert fp.features is not None and fp.features["forecast_wind_mph"] == 22.0
        assert fp.home_cover_probability < 0.5  # 22 mph: leans under
        assert fp.expected_margin is not None and fp.expected_margin < 44.5
        stored = s.scalars(select(WeatherForecast)).one()
        assert (stored.horizon_hours, stored.stadium_id, stored.fetched_at) == (24, "GNB00", now)
        lat, lon, at = forecaster.calls[0]
        assert (lat, lon, at) == (STADIUMS["GNB00"].latitude, STADIUMS["GNB00"].longitude, KICK)

        # Same horizon again: no new forecast, no new snapshot.
        snapshot(s, [model], now=now + timedelta(minutes=30))
        s.commit()
        assert len(forecaster.calls) == 1 and len(loads) == 1

        with pytest.raises(DatabaseError, match="append-only"):
            stored.wind_mph = 0.0
            s.flush()
        s.rollback()


def test_no_venues_means_no_forecast(session_factory: sessionmaker[Session]) -> None:
    now = KICK - timedelta(hours=20)
    gid = nfl_game(session_factory, now - timedelta(minutes=5), "e1", "2026_05_CHI_GB")

    def broken() -> list[dict[str, str]]:
        raise ProviderError("nflverse: HTTP 503")

    weather = WeatherState(broken, FakeForecaster())
    with session_factory() as s:
        weather.refresh(s)
        assert weather.forecast(gid, 24, now) is None
        assert weather.errors == ["nflverse: HTTP 503"]
