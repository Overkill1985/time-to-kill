import httpx
import pytest

from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.the_odds_api import BASE_URL, TheOddsApiProvider

SECRET = "test-key-not-real-123"


def _provider(handler: httpx.MockTransport) -> TheOddsApiProvider:
    return TheOddsApiProvider(SECRET, client=httpx.Client(base_url=BASE_URL, transport=handler))


def test_fetch_normalizes_and_reads_quota() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"x-requests-remaining": "480"},
            json=[
                {
                    "id": "abc",
                    "home_team": "A",
                    "away_team": "B",
                    "commence_time": "2026-10-01T00:00:00Z",
                    "bookmakers": [
                        {
                            "key": "dk",
                            "title": "DraftKings",
                            "markets": [
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"name": "A", "price": -120},
                                        {"name": "B", "price": 100},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        )

    provider = _provider(httpx.MockTransport(handle))
    fetch = provider.fetch_odds(Sport.CFB)
    assert seen[0].url.path == "/v4/sports/americanfootball_ncaaf/odds"
    assert seen[0].url.params["oddsFormat"] == "american"
    assert len(fetch.quotes) == 2
    assert provider.requests_remaining == 480


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_never_leak_key(status: int) -> None:
    provider = _provider(httpx.MockTransport(lambda r: httpx.Response(status, text=SECRET)))
    with pytest.raises(ProviderError) as info:
        provider.fetch_odds(Sport.NFL)
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None


def test_network_error_never_leaks_key() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}")

    with pytest.raises(ProviderError) as info:
        _provider(httpx.MockTransport(boom)).fetch_odds(Sport.NFL)
    assert SECRET not in str(info.value)


def test_requires_key() -> None:
    with pytest.raises(ValueError):
        TheOddsApiProvider("")
