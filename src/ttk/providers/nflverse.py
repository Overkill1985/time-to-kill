"""nflverse NFL schedule/results history with reported betting lines.

Source: ``nflverse/nfldata`` ``data/games.csv`` - every NFL game since 1999.
Verified 2026-09-26:
- ``result`` = home score - away score.
- ``spread_line`` is the home team's expected margin (positive = home favored;
  agrees with the moneyline favorite in 98.4% of games, the rest near pick'em).
  The bookmaker-style home line is therefore ``-spread_line``.
- Moneylines and spread/total prices exist from 2006; spreads/totals from 1999.
- ``gametime`` is US Eastern, 24-hour (documented upstream).
- ``espn`` is ESPN's event id, so games link to ESPN rows without name matching.
- Upstream does **not** document whether lines are opening or closing, or their
  source. They are stored as reported lines for benchmarking; a model may use one
  as an input only when the simulated bet is placed at that same line.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from ttk.domain import GameStatus, Sport
from ttk.providers.base import NormalizedGame, ProviderError, TeamRef

GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
EASTERN = ZoneInfo("America/New_York")

# nflverse team code -> (ESPN team id, current ESPN display name). Franchise-level:
# relocations share an id. Each historical code was verified against ESPN's own
# event record (e.g. 1999_03_CHI_OAK -> ESPN home team 13).
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("22", "Arizona Cardinals"),
    "ATL": ("1", "Atlanta Falcons"),
    "BAL": ("33", "Baltimore Ravens"),
    "BUF": ("2", "Buffalo Bills"),
    "CAR": ("29", "Carolina Panthers"),
    "CHI": ("3", "Chicago Bears"),
    "CIN": ("4", "Cincinnati Bengals"),
    "CLE": ("5", "Cleveland Browns"),
    "DAL": ("6", "Dallas Cowboys"),
    "DEN": ("7", "Denver Broncos"),
    "DET": ("8", "Detroit Lions"),
    "GB": ("9", "Green Bay Packers"),
    "HOU": ("34", "Houston Texans"),
    "IND": ("11", "Indianapolis Colts"),
    "JAX": ("30", "Jacksonville Jaguars"),
    "KC": ("12", "Kansas City Chiefs"),
    "LA": ("14", "Los Angeles Rams"),
    "STL": ("14", "Los Angeles Rams"),
    "LAC": ("24", "Los Angeles Chargers"),
    "SD": ("24", "Los Angeles Chargers"),
    "LV": ("13", "Las Vegas Raiders"),
    "OAK": ("13", "Las Vegas Raiders"),
    "MIA": ("15", "Miami Dolphins"),
    "MIN": ("16", "Minnesota Vikings"),
    "NE": ("17", "New England Patriots"),
    "NO": ("18", "New Orleans Saints"),
    "NYG": ("19", "New York Giants"),
    "NYJ": ("20", "New York Jets"),
    "PHI": ("21", "Philadelphia Eagles"),
    "PIT": ("23", "Pittsburgh Steelers"),
    "SEA": ("26", "Seattle Seahawks"),
    "SF": ("25", "San Francisco 49ers"),
    "TB": ("27", "Tampa Bay Buccaneers"),
    "TEN": ("10", "Tennessee Titans"),
    "WAS": ("28", "Washington Commanders"),
}

SEASON_TYPES = {"REG": "REG", "WC": "POST", "DIV": "POST", "CON": "POST", "SB": "POST"}


@dataclass(frozen=True)
class ReportedLines:
    """Lines as reported by nflverse (timing undocumented). Benchmark data; usable as a
    model input only when the simulated bet is at this same line (see governance)."""

    home_spread: float | None
    """Bookmaker-style home line: -3.5 means home favored by 3.5."""
    home_spread_odds: float | None
    away_spread_odds: float | None
    total: float | None
    over_odds: float | None
    under_odds: float | None
    home_moneyline: float | None
    away_moneyline: float | None


@dataclass(frozen=True)
class Starter:
    player_id: str
    player_name: str | None


@dataclass(frozen=True)
class HistoricalGame:
    game: NormalizedGame
    lines: ReportedLines | None
    home_qb: Starter | None = None
    away_qb: Starter | None = None
    """Starting quarterbacks (nflverse lists them for played games and for upcoming
    games once known). Known at kickoff - see docs/MODEL-GOVERNANCE.md."""


def _num(value: str | None) -> float | None:
    if value in (None, "", "NA"):
        return None
    return float(value)


def _int(value: str | None) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _team(code: str) -> TeamRef:
    espn_id, name = TEAMS[code]
    return TeamRef(name=name, espn_id=espn_id, other_names=(code,))


def _kickoff(gameday: str, gametime: str) -> datetime:
    local = datetime.fromisoformat(f"{gameday}T{gametime or '13:00'}").replace(tzinfo=EASTERN)
    return local.astimezone(UTC)


def parse_row(row: Mapping[str, str]) -> HistoricalGame:
    result = _int(row.get("result"))
    spread = _num(row.get("spread_line"))
    lines = ReportedLines(
        home_spread=None if spread is None else -spread,
        home_spread_odds=_num(row.get("home_spread_odds")),
        away_spread_odds=_num(row.get("away_spread_odds")),
        total=_num(row.get("total_line")),
        over_odds=_num(row.get("over_odds")),
        under_odds=_num(row.get("under_odds")),
        home_moneyline=_num(row.get("home_moneyline")),
        away_moneyline=_num(row.get("away_moneyline")),
    )
    has_lines = any(v is not None for v in vars(lines).values())
    game = NormalizedGame(
        provider=NflverseProvider.name,
        source_identifier=row["game_id"],
        sport=Sport.NFL,
        home=_team(row["home_team"]),
        away=_team(row["away_team"]),
        commence_time=_kickoff(row["gameday"], row.get("gametime", "")),
        source_timestamp=None,
        espn_event_id=row.get("espn") or None,
        status=GameStatus.FINAL if result is not None else GameStatus.SCHEDULED,
        home_score=_int(row.get("home_score")) if result is not None else None,
        away_score=_int(row.get("away_score")) if result is not None else None,
        neutral_site=row.get("location") == "Neutral",
        season=int(row["season"]),
        season_type=SEASON_TYPES.get(row.get("game_type", "")),
        week=_int(row.get("week")),
    )
    return HistoricalGame(
        game,
        lines if has_lines else None,
        home_qb=_starter(row.get("home_qb_id"), row.get("home_qb_name")),
        away_qb=_starter(row.get("away_qb_id"), row.get("away_qb_name")),
    )


def _starter(player_id: str | None, name: str | None) -> Starter | None:
    if not player_id or player_id == "NA":
        return None
    return Starter(player_id, name or None)


def parse_games(
    rows: Iterable[Mapping[str, str]], *, first_season: int, last_season: int
) -> tuple[list[HistoricalGame], dict[str, int]]:
    games: list[HistoricalGame] = []
    skipped: Counter[str] = Counter()
    for row in rows:
        season = int(row["season"])
        if not first_season <= season <= last_season:
            continue
        if row["home_team"] not in TEAMS or row["away_team"] not in TEAMS:
            skipped["unknown_team_code"] += 1
            continue
        games.append(parse_row(row))
    return games, dict(skipped)


class NflverseProvider:
    name = "nflverse"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=True)

    def fetch_history(
        self, *, first_season: int = 1999, last_season: int | None = None
    ) -> tuple[list[HistoricalGame], dict[str, int]]:
        try:
            response = self._client.get(GAMES_URL)
        except httpx.HTTPError as exc:
            raise ProviderError(f"nflverse: request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ProviderError(f"nflverse: HTTP {response.status_code}")
        rows = csv.DictReader(io.StringIO(response.text))
        return parse_games(
            rows, first_season=first_season, last_season=last_season or date.today().year
        )
