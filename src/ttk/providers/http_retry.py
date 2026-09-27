"""GET with retries for unofficial APIs that return occasional 5xx (ESPN)."""

from __future__ import annotations

import time

import httpx

from ttk.providers.base import ProviderError


def get_with_retries(
    client: httpx.Client, url: str, *, name: str, retries: int = 3, backoff: float = 5.0
) -> httpx.Response:
    """Retries transport errors and 5xx with linear backoff; returns the last
    response otherwise (the caller handles 4xx and the final 5xx)."""
    for attempt in range(retries + 1):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise ProviderError(f"{name}: request failed ({type(exc).__name__})") from None
        else:
            if response.status_code < 500 or attempt == retries:
                return response
        time.sleep(backoff * (attempt + 1))
    raise AssertionError("unreachable")
