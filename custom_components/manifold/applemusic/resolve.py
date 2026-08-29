"""Resolve a browse content id into a Sonos queue item.

Sonos only accepts Apple *catalog* ids. Library items carry their own ids
(``i.…``, ``l.…``, ``r.…``, ``p.…``) which the speaker rejects, so anything from the
library is translated to its catalog equivalent first — via ``playParams``
where Apple supplies it, and by a title+artist catalog search otherwise.

Tracks also need a real album catalog id for the DIDL parent: a placeholder is
accepted into the queue and then silently refuses to play.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .api import AppleMusicClient, AppleMusicError
from .browse import parse_content_id
from .const import is_library_id
from .play import build_container, build_station, build_track

if TYPE_CHECKING:
    from soco.data_structures import DidlObject

_LOGGER = logging.getLogger(__name__)


class NotPlayable(Exception):
    """The item has no catalog equivalent Sonos can stream."""


async def async_build_item(
    client: AppleMusicClient, content_id: str, sn: int
) -> DidlObject:
    """Build the soco item for a playable browse content id."""
    kind, value = parse_content_id(content_id)

    try:
        if kind == "song":
            return await _song(client, value, sn)
        if kind in ("album", "playlist"):
            return await _container(client, kind, value, sn)
        if kind == "station":
            return await _station(client, value, sn)
    except AppleMusicError as err:
        raise NotPlayable(f"Apple Music lookup failed: {err}") from err

    raise NotPlayable(f"Apple Music: {kind} is not playable")


async def _song(client: AppleMusicClient, value: str, sn: int) -> DidlObject:
    """Resolve a song id to a track item, including its album's catalog id."""
    catalog_id = value
    if is_library_id(value):
        catalog_id = await _catalog_id_for_library_song(client, value)

    detail = await client.get(
        f"catalog/{client.storefront}/songs/{catalog_id}", include="albums"
    )
    data = detail.get("data") or []
    if not data:
        raise NotPlayable(f"Apple Music has no catalog song {catalog_id}")

    attributes = data[0].get("attributes", {})
    albums = data[0].get("relationships", {}).get("albums", {}).get("data", [])
    if not albums:
        raise NotPlayable(
            f"'{attributes.get('name', catalog_id)}' has no catalog album, so Sonos "
            "has no valid parent for it"
        )

    return build_track(
        song_id=catalog_id,
        album_id=albums[0]["id"],
        sn=sn,
        title=attributes.get("name", ""),
        artist=attributes.get("artistName", ""),
        album=attributes.get("albumName", ""),
    )


async def _catalog_id_for_library_song(client: AppleMusicClient, value: str) -> str:
    """Map a library song to its catalog id, by playParams then by search."""
    detail = await client.get(f"me/library/songs/{value}")
    data = detail.get("data") or []
    attributes = data[0].get("attributes", {}) if data else {}

    if catalog_id := attributes.get("playParams", {}).get("catalogId"):
        return str(catalog_id)

    name, artist = attributes.get("name"), attributes.get("artistName")
    if not name:
        raise NotPlayable(f"Library song {value} has no catalog equivalent")

    # A song with no artistName is exactly the kind that reaches this fallback,
    # and interpolating the missing one searches for the word "None".
    result = await client.get(
        f"catalog/{client.storefront}/search",
        term=" ".join(filter(None, (name, artist))),
        types="songs",
        limit=1,
    )
    songs = result.get("results", {}).get("songs", {}).get("data", [])
    if not songs:
        raise NotPlayable(
            f"'{name}' is not in the Apple Music catalog — personal uploads cannot "
            "be streamed to Sonos"
        )
    _LOGGER.debug(
        "Resolved library song %s to catalog %s by search", value, songs[0]["id"]
    )
    return str(songs[0]["id"])


async def _station(client: AppleMusicClient, value: str, sn: int) -> DidlObject:
    """Stations need no catalog translation; the `ra.` id is what Sonos wants.

    The title is fetched only so the speaker shows something sensible before the
    first track's metadata arrives.
    """
    title = ""
    try:
        detail = await client.get(f"catalog/{client.storefront}/stations/{value}")
    except AppleMusicError as err:
        _LOGGER.debug(
            "Could not read station %s, playing without a title: %s", value, err
        )
    else:
        if data := detail.get("data"):
            title = data[0].get("attributes", {}).get("name", "")
    return build_station(value, sn, title=title)


async def _container(
    client: AppleMusicClient, kind: str, value: str, sn: int
) -> DidlObject:
    """Resolve an album or playlist id, translating library ids to catalog ones."""
    catalog_id, title = value, ""
    if is_library_id(value):
        detail = await client.get(f"me/library/{kind}s/{value}")
        data = detail.get("data") or []
        attributes = data[0].get("attributes", {}) if data else {}
        title = attributes.get("name", "")
        play_params = attributes.get("playParams", {})
        global_id = play_params.get("globalId") or play_params.get("catalogId")
        if not global_id:
            raise NotPlayable(
                f"'{title or value}' exists only in your library, so Sonos cannot "
                "stream it as a whole; play its tracks individually"
            )
        catalog_id = str(global_id)

    return build_container(kind, catalog_id, sn, title=title)
