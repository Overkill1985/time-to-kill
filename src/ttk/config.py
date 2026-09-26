from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ttk.domain import DataQuality
from ttk.qualification import QualificationRules


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TTK_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/time_to_kill.db"
    odds_api_key: SecretStr | None = None
    propline_api_key: SecretStr | None = None
    # No auth: the API must only answer on loopback (also blocks DNS rebinding).
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "[::1]"]

    odds_max_age_minutes: int = 30
    min_model_probability: float = 0.56
    min_edge: float = 0.02
    min_ev: float = 0.0
    min_data_quality: DataQuality = DataQuality.ACCEPTABLE

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
