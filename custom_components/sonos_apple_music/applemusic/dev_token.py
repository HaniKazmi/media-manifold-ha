"""Obtain and cache the Apple Music developer token.

Apple's web player ships a signed developer token inside its JS bundle. There is
no supported way to mint one without an Apple Developer membership, so the token
is read from the bundle and refreshed when it expires or is rejected.

The token is origin-locked: its claims carry
``"root_https_origin": ["apple.com"]`` and the API answers 401 to any request
that does not present a matching Origin header. Everything sending this token
must therefore also send ORIGIN, and no browser page served from Home Assistant
can use it — which is why there is no in-browser sign-in flow here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
import jwt

from ..const import DOMAIN
from .const import DEV_TOKEN_ISSUER, WEB_PLAYER_URL

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}.developer_token"

# A desktop UA; the bundle served to unknown agents does not carry the token.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_BUNDLE_RE = re.compile(r'src="(/assets/index~[^"]+\.js)"')
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")

# Refresh this far ahead of expiry so a long-running browse never straddles it.
_RENEW_MARGIN = 24 * 60 * 60

# How long a token whose `exp` cannot be read is assumed to last. It must exceed
# _RENEW_MARGIN, or such a token counts as already due for renewal and is
# re-scraped on every request.
_ASSUMED_LIFETIME = 7 * 24 * 60 * 60


class TokenError(Exception):
    """The developer token could not be obtained."""


def _decode_claims(token: str) -> dict | None:
    """Decode a JWT payload without verifying it; only public claims are read.

    Turning the signature check off turns every other check off with it, `exp`
    included — which is what this needs, since reading the expiry of an already
    expired token is the whole point of the call.
    """
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None


def _expiry_of(claims: dict) -> int:
    """When a token stops being usable, as a unix timestamp.

    `exp` is informational here — the API decides what it accepts — so a token
    carrying none is still used, on a short assumed life. Reading a missing or
    non-numeric `exp` as 0 instead puts every token permanently past renewal,
    which re-downloads the multi-megabyte web player bundle on every request.
    """
    try:
        return int(claims["exp"])
    except (KeyError, TypeError, ValueError):
        _LOGGER.warning(
            "Apple Music developer token carries no readable expiry; assuming %d days",
            _ASSUMED_LIFETIME // 86400,
        )
        return int(time.time()) + _ASSUMED_LIFETIME


class DeveloperToken:
    """Cached developer token with scrape-on-demand and refresh-on-401."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict] = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._token: str | None = None
        self._expires: int = 0
        self._lock = asyncio.Lock()

    async def async_get(self, *, force_refresh: bool = False) -> str:
        """Return a usable developer token, scraping one if needed.

        Callers are serialised so that a browse fanning out into several
        requests, all of which see the same expired token, downloads the web
        player bundle once rather than once per request. `stale` is captured
        before the lock: whoever gets in first refreshes, and the rest find a
        token that is no longer the one they saw fail.
        """
        stale = self._token
        async with self._lock:
            refreshing = force_refresh and self._token is stale
            if not refreshing and (usable := await self._async_cached()):
                return usable

            token, expires = await self._async_scrape()
            self._token, self._expires = token, expires
            await self._store.async_save({"token": token, "expires": expires})
            _LOGGER.debug("Fetched Apple Music developer token, expires %s", expires)
            return token

    async def _async_cached(self) -> str | None:
        """The in-memory or on-disk token, if one is far enough from expiry."""
        if self._token and self._expires - _RENEW_MARGIN > time.time():
            return self._token
        if (cached := await self._store.async_load()) and (
            cached.get("expires", 0) - _RENEW_MARGIN > time.time()
        ):
            self._token = cached["token"]
            self._expires = cached["expires"]
            return self._token
        return None

    async def _async_fetch(self, url: str, what: str) -> str:
        """GET a page or a bundle, naming what could not be loaded when it fails."""
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(url, headers={"User-Agent": _USER_AGENT}) as response:
                response.raise_for_status()
                return await response.text()
        except Exception as err:
            raise TokenError(f"Could not load {what}: {err}") from err

    async def _async_scrape(self) -> tuple[str, int]:
        """Pull the developer token out of the web player's JS bundle.

        The bundle carries three ES256 JWTs. Only the one issued by AMPWebPlay is
        accepted; the other two are well-formed, unexpired and answer 401, so
        selection is on the issuer rather than by trying each in turn.
        """
        page = await self._async_fetch(
            WEB_PLAYER_URL, "the Apple Music web player"
        )

        if not (bundle := _BUNDLE_RE.search(page)):
            raise TokenError(
                "No index bundle found in the Apple Music web player; "
                "the page layout has changed"
            )

        bundle_url = f"https://music.apple.com{bundle.group(1)}"
        script = await self._async_fetch(bundle_url, bundle_url)

        # The bundle is megabytes; scanning it on the event loop stalls
        # everything else Home Assistant is doing for as long as it takes.
        candidates = await self._hass.async_add_executor_job(_JWT_RE.findall, script)
        for token in candidates:
            claims = _decode_claims(token)
            if claims and claims.get("iss") == DEV_TOKEN_ISSUER:
                return token, _expiry_of(claims)

        raise TokenError(
            f"No {DEV_TOKEN_ISSUER} token among {len(candidates)} candidates in "
            f"{bundle_url}; Apple has changed how the web player is built"
        )
