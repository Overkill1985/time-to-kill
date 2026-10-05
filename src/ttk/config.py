from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ttk.domain import DataQuality, Sport
from ttk.qualification import QualificationRules


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TTK_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/time_to_kill.db"
    odds_api_key: SecretStr | None = None
    propline_api_key: SecretStr | None = None
    cfbd_api_key: SecretStr | None = None
    """CollegeFootballData.com (college football preseason data); bearer token."""
    alerts_notify: bool = True
    """Show health alerts as Windows notifications (they are always logged)."""
    alerts_steam_sports: str = "NFL,NBA"
    """Comma-separated sports watched for steam moves (empty: none)."""
    unit_size: float = 1.0
    """Currency per unit for performance reports (1.0 = units are currency)."""
    bettable_books: str | None = None
    """Comma-separated book keys you can actually bet at (e.g. "draftkings,fanduel").
    Best price and EV use only these; the market consensus still uses every book.
    Unset = every book, including exchanges and prediction markets."""
    # No auth: the API must only answer on loopback (also blocks DNS rebinding).
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "[::1]"]

    odds_max_age_minutes: int = 30
    min_model_probability: float = 0.56
    min_edge: float = 0.02
    min_ev: float = 0.0
    min_data_quality: DataQuality = DataQuality.ACCEPTABLE

    def steam_sports(self) -> list[Sport]:
        names = [s.strip().upper() for s in self.alerts_steam_sports.split(",") if s.strip()]
        return [Sport(n) for n in names]

    def bettable_book_keys(self) -> frozenset[str] | None:
        if not self.bettable_books:
            return None
        return frozenset(k.strip().lower() for k in self.bettable_books.split(",") if k.strip())

    def odds_provider_name(self, requested: str | None = None) -> str | None:
        """The provider to use: the requested one, else PropLine, else The Odds API,
        whichever has a key. None when no key is configured."""
        if requested:
            return requested
        if self.propline_api_key is not None:
            return "propline"
        if self.odds_api_key is not None:
            return "the-odds-api"
        return None

    def qualification_rules(self) -> QualificationRules:
        return QualificationRules(
            min_model_probability=self.min_model_probability,
            min_edge=self.min_edge,
            min_ev=self.min_ev,
            min_data_quality=self.min_data_quality,
            max_odds_age=timedelta(minutes=self.odds_max_age_minutes),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
