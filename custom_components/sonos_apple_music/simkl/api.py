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

This module speaks HTTP and nothing else. It raises nothing, and it touches no
Home Assistant state: what a rejected token should show a household is a
question about Home Assistant, and it is answered a layer up in `watch.py`,
where this integration's other user-facing reporting already lives.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from http import HTTPStatus
import logging
import time
from typing import Any, Final

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import playing
from .const import API_BASE, QUERY

_LOGGER = logging.getLogger(__name__)

# How long a title SIMKL could not name stays unsearched. A show it has not
# matched yet is matched as its catalogue fills, so the miss cannot be kept
# forever — but re-asking on every transition is three identical questions an
# episode, about a title whose answer will not change this evening.
_MISS_TTL: Final = 3600

# Home Assistant's shared session carries aiohttp's default of five minutes,
# and one request holds the lock every other scrobble in the household is
# waiting on. A scrobble describes a moment, so one that has not landed in ten
# seconds has already lost the argument with the transition after it.
_TIMEOUT: Final = ClientTimeout(total=10)

# What SIMKL's refusals mean, for the one line that reports them. Every entry
# ends the scrobble; only the reason differs.
_REFUSALS: Final = {
    # Recorded within the hour already, and it declines to do so twice.
    HTTPStatus.CONFLICT: "already holds",
    # Never retried: one that landed after the next transition would describe a
    # moment that has passed, and that transition says it better.
    HTTPStatus.TOO_MANY_REQUESTS: "rate-limited",
    HTTPStatus.UNAUTHORIZED: "rejected the token for",
    # The ids were matched against nothing.
    HTTPStatus.NOT_FOUND: "could not match",
}


class Result(StrEnum):
    """What became of one scrobble."""

    RECORDED = "recorded"
    DECLINED = "declined"
    TOKEN_REJECTED = "token_rejected"


class SimklClient:
    """Talks to SIMKL on behalf of one config entry."""

    def __init__(self, hass: HomeAssistant, token: str) -> None:
        self._hass = hass
        self._token = token
        # Show title -> SIMKL id. A household rewatches a handful of shows, so
        # this is one search per show rather than one per transition.
        self._show_ids: dict[str, int] = {}
        # Show title -> when SIMKL last failed to name it, to the same end.
        self._missed: dict[str, float] = {}
        # SIMKL holds a 20-second lock per user and answers overlapping calls
        # with 429, so the three transitions of one episode are sent in turn.
        # Resolving the show inside it too means the pair an episode boundary
        # produces — a stop and a start of the same show — asks once.
        self._lock = asyncio.Lock()

    @property
    def shows(self) -> dict[str, int]:
        """The ids resolved so far, for diagnostics.

        A wrong id is the one failure invisible from outside: SIMKL accepts the
        scrobble and files the episode under whichever show that id names.
        """
        return dict(self._show_ids)

    def show_id(self, title: str) -> int | None:
        """The id already resolved for a title, without searching for one."""
        return self._show_ids.get(title)

    async def async_scrobble(self, event: playing.Event) -> Result:
        """Report one transition, and say what SIMKL made of it."""
        async with self._lock:
            show_id = await self._async_show_id(event.episode.show)
            if show_id is None:
                return Result.DECLINED

            payload: dict[str, Any] = {
                # `simkl`, not the `simkl_id` the search answers with: the write
                # side ignores an ids object it does not recognise and reports
                # success.
                "show": {"ids": {"simkl": show_id}},
                "episode": {
                    "season": event.episode.season,
                    "number": event.episode.number,
                },
            }
            if event.progress is not None:
                payload["progress"] = event.progress

            return await self._async_post(event, payload)

    async def _async_show_id(self, title: str) -> int | None:
        """The SIMKL id for a show title, searched once and remembered.

        A miss is remembered for `_MISS_TTL` and a failure to reach SIMKL is not
        remembered at all: the first is an answer that may change, the second is
        not an answer.
        """
        if (cached := self._show_ids.get(title)) is not None:
            return cached

        if time.monotonic() - self._missed.get(title, -_MISS_TTL) < _MISS_TTL:
            return None

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                f"{API_BASE}/search/tv", params={**QUERY, "q": title}, timeout=_TIMEOUT
            ) as response:
                response.raise_for_status()
                results = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("SIMKL search for %r failed: %s", title, err)
            return None

        if not isinstance(results, list):
            # Not an answer about the show. Remembering it would suppress the
            # title for an hour on the strength of something SIMKL never said.
            _LOGGER.debug("SIMKL answered the search for %r with %r", title, results)
            return None

        if not results:
            # An answer: the catalogue holds nothing by that name today.
            _LOGGER.debug("SIMKL knows no show called %r; not scrobbling it", title)
            self._missed[title] = time.monotonic()
            return None

        first = results[0]
        ids = first.get("ids") or {} if isinstance(first, dict) else {}
        if (show_id := ids.get("simkl_id")) is None:
            # A hit shaped wrongly is the leading indicator for the `simkl_id`
            # versus `simkl` confusion this module exists to get right, so it is
            # said separately and is not remembered as a miss.
            _LOGGER.debug("SIMKL's first hit for %r carries no id: %r", title, first)
            return None

        self._show_ids[title] = show_id
        return show_id

    async def _async_post(
        self, event: playing.Event, payload: dict[str, Any]
    ) -> Result:
        """Send one scrobble and read what SIMKL made of it.

        Only a body echoing a non-zero id counts as recorded. A 409 does not:
        that episode is on the history, but an earlier call put it there, so
        anything announcing a fresh one would be announcing it twice.
        """
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                f"{API_BASE}/scrobble/{event.act}",
                params=QUERY,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as response:
                status = response.status
                body = await response.json(content_type=None) if status < 400 else None
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("SIMKL %s for %s failed: %s", event.act, event.episode, err)
            return Result.DECLINED

        if status >= 400:
            _LOGGER.debug(
                "SIMKL %s %s (%s, status %s)",
                _REFUSALS.get(status, "answered"),
                event.episode,
                event.act,
                status,
            )
            if status == HTTPStatus.NOT_FOUND:
                # SIMKL will not resolve the id this show was cached under, so
                # keeping it would make every later episode fail the same way.
                # Dropping it sends the next transition back to the search.
                self._show_ids.pop(event.episode.show, None)
            if status == HTTPStatus.UNAUTHORIZED:
                return Result.TOKEN_REJECTED
            return Result.DECLINED

        if not (isinstance(body, dict) and body.get("id")):
            _LOGGER.warning(
                "SIMKL accepted %s for %s but recorded nothing (%s); the show is "
                "unmatched or the ids key it was sent is one it ignores",
                event.act,
                event.episode,
                body,
            )
            return Result.DECLINED

        _LOGGER.debug("SIMKL %s %s at %s%%", event.act, event.episode, event.progress)
        return Result.RECORDED
