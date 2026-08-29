"""Thin async client for the Apple Music API.

Two credentials are in play and they are independent:

- the developer token, scraped from the web player, which every request needs;
- the music user token, pasted by the user, which only ``me/*`` endpoints need.

Catalog browsing works with the developer token alone, so the integration stays
useful when there is no user token or it has expired.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any, NamedTuple

from aiohttp import ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .const import (
    API_BASE,
    DEFAULT_STOREFRONT,
    ORIGIN,
    RECENT_PAGE_SIZE,
    SAFE_PAGE_SIZE,
    is_library_id,
)
from .dev_token import DeveloperToken

_LOGGER = logging.getLogger(__name__)

# Caps that need no discovering. Probing for one costs a wasted request and
# leaves the caller that opened the node waiting on the retry, so a cap already
# known is stated here rather than learned again on every restart.
_KNOWN_PAGE_LIMITS = {"me/recent/played/tracks": RECENT_PAGE_SIZE}


class AppleMusicError(Exception):
    """An Apple Music API request failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UserTokenInvalid(AppleMusicError):
    """The music user token was rejected; library endpoints are unavailable."""


class Page(NamedTuple):
    """Items collected from an endpoint, and how many Apple says exist.

    `total` is None where Apple publishes no count, and a caller then cannot
    tell a full level from a truncated one.
    """

    items: list[dict]
    total: int | None


class AppleMusicClient:
    """Async wrapper over api.music.apple.com."""

    def __init__(
        self,
        hass: HomeAssistant,
        developer_token: DeveloperToken,
        user_token: str | None = None,
        storefront: str = DEFAULT_STOREFRONT,
        on_token_invalid: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._developer_token = developer_token
        self._user_token = user_token
        self.storefront = storefront
        # Called when Apple rejects the user token. A cookie expires while Home
        # Assistant is running, not while it is starting, so the only setup-time
        # check would be one that never fires for the case that matters.
        self._on_token_invalid = on_token_invalid
        # endpoint -> page size Apple actually accepts. Seeded with the caps
        # already known, and extended as others are learned from a 400.
        self._page_limits: dict[str, int] = dict(_KNOWN_PAGE_LIMITS)

    @property
    def has_user_token(self) -> bool:
        """Whether library endpoints can be reached."""
        return bool(self._user_token)

    @property
    def developer_token_expires_at(self) -> datetime | None:
        """When the held developer token lapses, for diagnostics."""
        return self._developer_token.expires_at

    def resource_path(self, resource: str, apple_id: str) -> str:
        """The endpoint for one Apple resource, library or catalog as its id says.

        A library id 404s against the catalog endpoints and vice versa, so the
        choice belongs beside the storefront rather than with each caller.
        """
        if is_library_id(apple_id):
            return f"me/library/{resource}/{apple_id}"
        return f"catalog/{self.storefront}/{resource}/{apple_id}"

    async def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = await self._developer_token.async_get(force_refresh=force_refresh)
        # Origin is not optional: the scraped token is locked to apple.com and the
        # API answers 401 without a matching Origin, including on public endpoints.
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        if self._user_token:
            headers["Music-User-Token"] = self._user_token
        return headers

    async def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """GET an endpoint, refreshing the developer token once on a 401.

        A 401 is ambiguous — an expired developer token and a rejected user token
        look alike — so a retry with a fresh developer token disambiguates: if it
        still fails and a user token is set, the user token is the bad one.
        """
        session = async_get_clientsession(self._hass)
        url = f"{API_BASE}/{endpoint.lstrip('/')}"

        for attempt in (0, 1):
            # Inside the try: a failed developer-token scrape raises TokenError,
            # and every caller of this method handles AppleMusicError alone. Left
            # outside, a broken scrape reaches the frontend as a raw traceback
            # instead of a browse error or a failed play.
            try:
                headers = await self._headers(force_refresh=attempt == 1)
                async with session.get(url, headers=headers, params=params) as response:
                    response.raise_for_status()
                    return await response.json()
            except ClientResponseError as err:
                if err.status == 401 and attempt == 0:
                    _LOGGER.debug("401 from %s, refreshing developer token", endpoint)
                    continue
                # A 403 is only evidence about the cookie on a library
                # endpoint. Apple also answers 403 for catalog content a
                # storefront does not carry, and `browse._stations` drops such a
                # surface on purpose — raising a reauth repair there would ask
                # the user to replace a cookie that is working.
                if self._user_token and (
                    err.status == 401
                    or (err.status == 403 and endpoint.startswith("me/"))
                ):
                    if self._on_token_invalid is not None:
                        self._on_token_invalid()
                    raise UserTokenInvalid(
                        f"Apple Music rejected the request to {endpoint}; the "
                        "music user token has most likely expired"
                    ) from err
                raise AppleMusicError(
                    f"{endpoint} failed: {err.status}", status=err.status
                ) from err
            except Exception as err:
                raise AppleMusicError(f"{endpoint} failed: {err}") from err

        # Unreachable: the loop above either returns or raises on the second pass.
        raise AppleMusicError(f"{endpoint} failed after a token refresh")

    async def get_limited(
        self, endpoint: str, limit: int, **params: Any
    ) -> tuple[dict[str, Any], int]:
        """GET one page, backing off if Apple rejects the limit asked for.

        Apple caps page size per endpoint, publishes no way to ask what the cap
        is, and rejects an oversized one with a bare 400 that names no field. So
        an over-large request is retried at a size every endpoint accepts, and
        the working size is remembered against the exact path it was learned
        from. A per-artist view therefore pays that wasted request once per
        artist rather than once per shape — still better than a wrong constant
        that only shows up in production, and it never caps a path Apple would
        have served.

        Returns the page and the size that worked, which is what a caller
        following `next` links must keep asking for.
        """
        effective = min(limit, self._page_limits.get(endpoint, limit))
        try:
            return await self.get(endpoint, **params, limit=effective), effective
        except AppleMusicError as err:
            if err.status != 400 or effective <= SAFE_PAGE_SIZE:
                raise
            _LOGGER.debug(
                "%s rejected limit=%d; retrying at %d",
                endpoint,
                effective,
                SAFE_PAGE_SIZE,
            )
            page = await self.get(endpoint, **params, limit=SAFE_PAGE_SIZE)
            self._page_limits[endpoint] = SAFE_PAGE_SIZE
            _LOGGER.info(
                "Apple Music caps %s at %d items per request", endpoint, SAFE_PAGE_SIZE
            )
            return page, SAFE_PAGE_SIZE

    async def get_paged(
        self, endpoint: str, limit: int, page_size: int = 100, **params: Any
    ) -> Page:
        """Collect up to `limit` items, following Apple's `next` links.

        BrowseMedia cannot page, so callers cap `limit` and report the remainder
        via the total rather than walking the whole collection.
        """
        page, effective = await self.get_limited(
            endpoint, min(limit, page_size), **params
        )
        total = page.get("meta", {}).get("total")

        items: list[dict] = []
        while True:
            before = len(items)
            items.extend(page.get("data", []))
            next_url = page.get("next")
            # A `next` link over an empty page, or one echoing an offset it has
            # already served, would loop here forever: nothing else in this
            # function grows, so neither condition below would ever come true.
            if not next_url or len(items) >= limit or len(items) == before:
                break
            # `next` arrives as an absolute /v1/... path carrying its own offset.
            # It may also echo back `limit`, which would arrive twice and raise
            # a TypeError no caller is equipped to turn into a BrowseError.
            link = URL(next_url)
            query = dict(link.query)
            query.pop("limit", None)
            page = await self.get(
                link.path.removeprefix("/v1/"), **query, limit=effective
            )

        return Page(items[:limit], total)

    async def async_resolve_storefront(self) -> str:
        """Return the user's storefront, falling back to the configured default."""
        if not self._user_token:
            return self.storefront
        try:
            result = await self.get("me/storefront")
        except UserTokenInvalid:
            # Not a transient failure: keeping the configured storefront here
            # would hide the one condition this request can prove.
            raise
        except AppleMusicError as err:
            _LOGGER.debug(
                "Could not read storefront, keeping %s: %s", self.storefront, err
            )
            return self.storefront
        if data := result.get("data"):
            self.storefront = data[0]["id"]
        return self.storefront


def artwork_url(artwork: dict[str, Any] | None, size: int = 500) -> str | None:
    """Render an Apple artwork descriptor to a concrete URL.

    Apple returns a template carrying {w}/{h} placeholders that must be filled in
    before the URL resolves to an image.
    """
    if not artwork or not (template := artwork.get("url")):
        return None
    return template.replace("{w}", str(size)).replace("{h}", str(size))
