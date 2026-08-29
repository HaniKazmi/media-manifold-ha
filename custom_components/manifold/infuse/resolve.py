"""Turning a Jellyfin item into something Infuse will play.

Nothing here is configured twice. The server URL, the API key and the user all
come from the core Jellyfin integration's own config entry, read at play time so
that the two integrations can load in either order and Jellyfin can be added
afterwards without a restart.

Two ways in, and choosing between them is the whole of this module's judgement:
a library deep link when TMDB knows the item, and the stream URL when it does
not. What the fallback gives up is the writeback of watched state, which for an
unmatched file was never going anywhere useful.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import link

_LOGGER = logging.getLogger(__name__)

JELLYFIN_DOMAIN = "jellyfin"

# Series id -> TMDB id. Playing through a season otherwise asks the server once
# per episode for an id that, once Jellyfin has one, does not change. Bounded by
# the number of series a household plays, and dropped when the graft is removed.
_series_tmdb: dict[str, str] = {}


def forget() -> None:
    """Drop everything learned about the server."""
    _series_tmdb.clear()


def async_client(hass: HomeAssistant) -> Any | None:
    """The Jellyfin integration's authenticated client, if it is loaded.

    This walks the same chain Jellyfin's own media source walks to reach it, so
    the graft is coupled no more tightly than Home Assistant already is.
    """
    if not (entries := hass.config_entries.async_loaded_entries(JELLYFIN_DOMAIN)):
        return None
    return entries[0].runtime_data.api_client.jellyfin


async def async_item(hass: HomeAssistant, api: Any, item_id: str) -> Any:
    """Fetch one item. The client is synchronous, so it goes to the executor."""
    return await hass.async_add_executor_job(api.get_item, item_id)


async def _async_series_tmdb(hass: HomeAssistant, api: Any, series: str) -> str | None:
    if (cached := _series_tmdb.get(series)) is not None:
        return cached

    # ProviderIds is the only field wanted here, and the client's default set
    # makes the server aggregate item counts and running times across every
    # episode of the show to answer.
    item = await hass.async_add_executor_job(api.get_item, series, "ProviderIds")
    tmdb = link.tmdb_id(item) if item else None

    # Only a hit is kept. A series Jellyfin has not matched against TMDB yet
    # gets an id the moment it does, and remembering the miss would hold every
    # episode of it on the fallback — giving up the watched-state writeback the
    # deep link exists for — until something reloads the entry.
    if tmdb is not None:
        _series_tmdb[series] = tmdb
    return tmdb


async def async_url(hass: HomeAssistant, api: Any, item_id: str, item: Any) -> str:
    """The Infuse URL that plays a Jellyfin item."""
    series_tmdb = None
    if (series := link.series_id(item)) is not None:
        series_tmdb = await _async_series_tmdb(hass, api, series)

    if (url := link.deep_link(item, series_tmdb)) is not None:
        return url

    # `video_url` fills in a template and issues nothing, so it stays on the loop.
    if not (stream_url := api.video_url(item_id)):
        raise HomeAssistantError(
            f"Jellyfin item {link.display_name(item) or item_id} has no TMDB id "
            f"and no stream URL, so Infuse has no way to play it"
        )

    _LOGGER.debug("No TMDB id for %s; playing its stream directly", item_id)
    return link.direct_play(
        stream_url,
        filename=link.display_name(item),
        position=link.resume_position(item),
    )
