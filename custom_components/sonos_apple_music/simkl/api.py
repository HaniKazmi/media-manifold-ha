"""Reporting Apple TV playback to SIMKL.

A scrobble has to name a show in a way SIMKL resolves, and the television gives
too little for that on its own. The endpoints accept an id, or a title *and* a
year; the Apple TV reports a title and no year, so the id has to be looked up.
That lookup is the whole reason this module makes two kinds of request:

    {"title": "Black Bird"}                  ->  404 id_err
    {"title": "Black Bird", "year": 2022}    ->  201
    {"ids": {"simkl": 1624792}}              ->  201
    {"ids": {"simkl_id": 1624792}}           ->  201, body {"id": 0}

The last line is why a status code is not read as success here. The search
endpoint *returns* the key as ``simkl_id`` and the scrobble endpoints expect
``simkl``; handing the one straight to the other is answered with a 201 carrying
no show, and nothing is recorded. Only the body distinguishes them, so only the
body is believed.

Nothing here raises into the caller. A scrobble is a report about television,
and the worst it may cost is a log line.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import DOMAIN
from . import playing
from .const import API_BASE, QUERY

_LOGGER = logging.getLogger(__name__)

TOKEN_ISSUE = "simkl_token_rejected"


class SimklClient:
    """Talks to SIMKL on behalf of one config entry."""

    def __init__(self, hass: HomeAssistant, token: str) -> None:
        self._hass = hass
        self._token = token
        # Show title -> SIMKL id. A household rewatches a handful of shows, so
        # this is one search per show rather than one per transition.
        self._show_ids: dict[str, int] = {}
        # SIMKL holds a 20-second lock per user and answers overlapping calls
        # with 429, so the three transitions of one episode are sent in turn.
        self._lock = asyncio.Lock()

    async def async_scrobble(self, event: playing.Event) -> None:
        """Report one transition, if the show can be named to SIMKL."""
        show_id = await self.async_show_id(event.episode.show)
        if show_id is None:
            return

        payload: dict[str, Any] = {
            # `simkl`, not the `simkl_id` the search answers with: the write side
            # ignores an ids object it does not recognise and reports success.
            "show": {"ids": {"simkl": show_id}},
            "episode": {
                "season": event.episode.season,
                "number": event.episode.number,
            },
        }
        if event.progress is not None:
            payload["progress"] = event.progress

        async with self._lock:
            await self._async_post(event, payload)

    async def async_show_id(self, title: str) -> int | None:
        """The SIMKL id for a show title, searched once and remembered.

        Only hits are kept. A miss caches nothing because SIMKL matches new shows
        as its catalogue fills, and remembering the miss would hold a show
        unscrobbled until something reloaded the entry.
        """
        if (cached := self._show_ids.get(title)) is not None:
            return cached

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                f"{API_BASE}/search/tv", params={**QUERY, "q": title}
            ) as response:
                response.raise_for_status()
                results = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("SIMKL search for %r failed: %s", title, err)
            return None

        if not results or not isinstance(results, list):
            _LOGGER.debug("SIMKL knows no show called %r; not scrobbling it", title)
            return None

        if (show_id := (results[0].get("ids") or {}).get("simkl_id")) is None:
            _LOGGER.debug("SIMKL's first hit for %r carries no id", title)
            return None

        self._show_ids[title] = show_id
        return show_id

    async def _async_post(self, event: playing.Event, payload: dict[str, Any]) -> None:
        """Send one scrobble and read what SIMKL made of it."""
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                f"{API_BASE}/scrobble/{event.act}",
                params=QUERY,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as response:
                status = response.status
                body = await response.json(content_type=None) if status < 400 else None
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("SIMKL %s for %s failed: %s", event.act, event.episode, err)
            return

        if status == HTTPStatus.CONFLICT:
            # SIMKL has recorded this episode within the hour and declines to do
            # it twice. The scrobble arrived; there is nothing to fix.
            _LOGGER.debug("SIMKL already has %s; %s ignored", event.episode, event.act)
            return

        if status == HTTPStatus.TOO_MANY_REQUESTS:
            # Not retried. By the time a retry landed it would describe a moment
            # that has passed, and the next transition says the same thing better.
            _LOGGER.debug("SIMKL rate-limited %s for %s", event.act, event.episode)
            return

        if status == HTTPStatus.UNAUTHORIZED:
            self._async_token_rejected()
            return

        if status >= 400:
            _LOGGER.debug(
                "SIMKL answered %s to %s for %s", status, event.act, event.episode
            )
            return

        if not (isinstance(body, dict) and body.get("id")):
            _LOGGER.warning(
                "SIMKL accepted %s for %s but recorded nothing (%s); the show is "
                "unmatched or the ids key it was sent is one it ignores",
                event.act,
                event.episode,
                body,
            )
            return

        _LOGGER.debug(
            "SIMKL %s %s at %s%%", event.act, event.episode, event.progress
        )
        ir.async_delete_issue(self._hass, DOMAIN, TOKEN_ISSUE)

    def _async_token_rejected(self) -> None:
        """Raise a repair, rather than putting the whole entry into reauth.

        A SIMKL token lasts five years, and the entry's reauth flow asks for the
        Apple Music cookie. Sending someone there to fix scrobbling would make
        the common path stranger to serve the rare one.
        """
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            TOKEN_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=TOKEN_ISSUE,
        )
        _LOGGER.warning(
            "SIMKL rejected the stored token; Apple TV playback is not being "
            "scrobbled. Reconfigure the integration to link SIMKL again"
        )
