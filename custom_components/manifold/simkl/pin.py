"""The PIN flow that gets a SIMKL token without a browser redirect.

SIMKL's device flow hands out a five-character code, the user approves it at
simkl.com/pin on whatever device is convenient, and the same code is exchanged
for a token. No secret and no redirect URI are involved, which is what lets a
Home Assistant config flow use it at all: there is nothing for a self-hosted
instance to register.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE, QUERY

_LOGGER = logging.getLogger(__name__)


class PinError(Exception):
    """SIMKL could not be reached, or answered something unusable."""


class Code(NamedTuple):
    """A code to show the user, and where they approve it."""

    user_code: str
    verification_url: str


async def async_request_code(hass: HomeAssistant) -> Code:
    """Ask SIMKL for a code for the user to approve."""
    result = await _async_get(hass, f"{API_BASE}/oauth/pin")

    if not (user_code := result.get("user_code")):
        raise PinError(f"SIMKL returned no code to approve: {result}")

    # SIMKL sends the URL to show, so a change of address needs no release here.
    return Code(user_code, result.get("verification_url") or "https://simkl.com/pin")


async def async_poll(hass: HomeAssistant, user_code: str) -> str | None:
    """The token for an approved code, or None while it is still unapproved."""
    result = await _async_get(hass, f"{API_BASE}/oauth/pin/{user_code}")

    if token := result.get("access_token"):
        return token

    # "KO" is SIMKL saying not yet, which is the ordinary answer until the user
    # has finished at simkl.com/pin, and not something to report as a failure.
    if result.get("result") == "KO":
        return None

    raise PinError(f"SIMKL answered the code exchange with {result}")


async def _async_get(hass: HomeAssistant, url: str) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, params=QUERY) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
    except (ClientError, TimeoutError, ValueError) as err:
        raise PinError(f"SIMKL is unreachable: {err}") from err

    if not isinstance(result, dict):
        raise PinError(f"SIMKL answered {url} with {result!r}")
    return result
