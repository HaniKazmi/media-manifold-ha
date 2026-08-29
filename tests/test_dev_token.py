"""Scraping the Apple Music developer token.

This is the most fragile thing here: it depends on the shape of Apple's web
player build, which nothing obliges them to keep. These tests pin the parts that
can rot silently rather than loudly.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util
import pytest

from custom_components.manifold.applemusic.const import DEV_TOKEN_ISSUER, WEB_PLAYER_URL
from custom_components.manifold.applemusic.dev_token import (
    DeveloperToken,
    TokenError,
    _expiry_of,
)

BUNDLE_URL = "https://music.apple.com/assets/index~abc123.js"
PAGE = f'<html><body><script src="{BUNDLE_URL.removeprefix("https://music.apple.com")}"></script></body></html>'


def jwt(issuer: str, expires: int | None = None) -> str:
    """A structurally valid JWT; only the payload is ever read."""
    payload = {"iss": issuer, "exp": expires or int(time.time()) + 90 * 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.{encoded}.{'s' * 40}"


GOOD = jwt("AMPWebPlay")
DECOYS = [jwt("M62YD85FTQ"), jwt("5IKPP2IECQ")]


@pytest.fixture(autouse=True)
def no_token_scraping():
    """Override the suite-wide patch; these tests exercise the scrape itself."""
    return None


@pytest.fixture
def token(hass) -> DeveloperToken:
    return DeveloperToken(hass)


def bundle(*tokens: str) -> str:
    return "var a=1;" + ";".join(f'k="{t}"' for t in tokens) + ";"


@pytest.mark.parametrize(
    "order",
    [(DECOYS[0], DECOYS[1], GOOD), (GOOD, DECOYS[0], DECOYS[1])],
    ids=["last", "first"],
)
async def test_the_ampwebplay_token_is_selected_wherever_it_sits(
    token, aioclient_mock, order
) -> None:
    """The other JWTs in the bundle are well-formed, unexpired, and answer 401.

    Selection is on the issuer rather than position: the order the three appear
    in the bundle differs between hosts.
    """
    aioclient_mock.get(WEB_PLAYER_URL, text=PAGE)
    aioclient_mock.get(BUNDLE_URL, text=bundle(*order))

    assert await token.async_get(force_refresh=True) == GOOD


async def test_a_bundle_without_our_issuer_fails_loudly(token, aioclient_mock) -> None:
    """Silently returning a decoy would surface as an unexplained 401 later."""
    aioclient_mock.get(WEB_PLAYER_URL, text=PAGE)
    aioclient_mock.get(BUNDLE_URL, text=bundle(*DECOYS))

    with pytest.raises(TokenError, match="AMPWebPlay"):
        await token.async_get(force_refresh=True)


async def test_a_page_without_a_bundle_fails_loudly(token, aioclient_mock) -> None:
    aioclient_mock.get(WEB_PLAYER_URL, text="<html><body>nothing here</body></html>")

    with pytest.raises(TokenError, match="index bundle"):
        await token.async_get(force_refresh=True)


async def test_an_unreachable_web_player_fails_loudly(token, aioclient_mock) -> None:
    aioclient_mock.get(WEB_PLAYER_URL, status=503)

    with pytest.raises(TokenError):
        await token.async_get(force_refresh=True)


async def test_a_cached_token_is_reused(token, aioclient_mock) -> None:
    """One scrape pulls a 3 MB bundle; doing it per request would be absurd."""
    aioclient_mock.get(WEB_PLAYER_URL, text=PAGE)
    aioclient_mock.get(BUNDLE_URL, text=bundle(GOOD))

    await token.async_get()
    calls = len(aioclient_mock.mock_calls)
    await token.async_get()

    assert len(aioclient_mock.mock_calls) == calls


async def test_a_token_near_expiry_is_refreshed(hass, aioclient_mock) -> None:
    """Refreshing early keeps a long browse from straddling the expiry."""
    nearly_expired = jwt("AMPWebPlay", expires=int(time.time()) + 3600)
    aioclient_mock.get(WEB_PLAYER_URL, text=PAGE)
    aioclient_mock.get(BUNDLE_URL, text=bundle(nearly_expired))

    token = DeveloperToken(hass)
    await token.async_get()
    calls = len(aioclient_mock.mock_calls)
    await token.async_get()

    assert len(aioclient_mock.mock_calls) > calls


@pytest.mark.parametrize("exp", [None, "not-a-number"])
async def test_a_token_with_no_readable_expiry_is_still_cached(hass, exp) -> None:
    """Reading an unusable `exp` as 0 puts the token permanently past renewal,
    so every request re-downloads the multi-megabyte player bundle."""
    claims = {"iss": DEV_TOKEN_ISSUER}
    if exp is not None:
        claims["exp"] = exp

    token = DeveloperToken(hass)
    with patch.object(
        DeveloperToken, "_async_scrape", AsyncMock(return_value=("t", _expiry_of(claims)))
    ) as scrape:
        assert await token.async_get() == "t"
        assert await token.async_get() == "t"

    assert scrape.call_count == 1


async def test_the_expiry_is_readable_once_a_token_is_held(
    token, aioclient_mock
) -> None:
    """Diagnostics reports it, and a scrape that never ran has none to report."""
    expires = int(time.time()) + 90 * 86400
    aioclient_mock.get(WEB_PLAYER_URL, text=PAGE)
    aioclient_mock.get(BUNDLE_URL, text=bundle(jwt(DEV_TOKEN_ISSUER, expires)))

    assert token.expires_at is None
    await token.async_get()

    assert token.expires_at == dt_util.utc_from_timestamp(expires)
