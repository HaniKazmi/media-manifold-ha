"""Build Sonos queue items for Apple Music content and play them.

Every shape here was verified against a live household: a track plays via
`add_to_queue` + `play_from_queue`, and album/playlist containers expand
server-side into real queue entries the same way. `play_uri` is deliberately
unused — it answers UPnP 714 "Illegal MIME-Type" for every cpcontainer form.

soco is imported inside the functions that need it, not at module scope. It
belongs to the Sonos integration and this one declares no requirements of its
own, so it is absent until Sonos has been set up. A module-scope import makes
that absence an ImportError while Home Assistant is loading the package, which
is before ``patch.async_install`` — the code whose whole job is to notice Sonos
is unavailable and leave it alone — ever runs, and takes the config flow with
it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import MediaPlayerEnqueue

from .const import (
    ALBUM_CONTAINER_ID,
    CDUDN,
    CONTAINER_PROTOCOL_INFO,
    CONTAINER_URI,
    PLAYLIST_CONTAINER_ID,
    SONOS_SERVICE_ID,
    STATION_ITEM_ID,
    STATION_PARENT_ID,
    STATION_PROTOCOL_INFO,
    STATION_URI,
    TRACK_ITEM_ID,
    TRACK_PARENT_ID,
    TRACK_PROTOCOL_INFO,
    TRACK_URI,
)

if TYPE_CHECKING:
    from soco import SoCo
    from soco.data_structures import DidlAudioBroadcast, DidlMusicTrack, DidlObject

_LOGGER = logging.getLogger(__name__)

# Matches sn= on a res URI that belongs to Apple Music, whatever the parameter
# order. Anchored on sid so another service's serial is never picked up.
_SN_RE = re.compile(rf"(?=.*\bsid={SONOS_SERVICE_ID}\b).*?\bsn=(\d+)")


def discover_sn(favorites: Any) -> int | None:
    """The household's Apple Music account serial, or None if none is observable.

    Sonos S2 removed /status/accounts, so the serial is only observable in URIs
    the Sonos app has already written. Favorites are already in memory, so this
    costs no network call.

    Reads each favorite's own top-level resource rather than `.reference`:
    soco 0.31.1 lets IndexError/AttributeError escape from `DidlFavorite.reference`
    when a favorite carries no resMD, and one such favorite would otherwise take
    out the whole scan.

    Returning None rather than the fallback keeps the two cases apart: a serial
    that was read and a serial that was guessed fail in completely different
    ways, and only the caller is placed to say so.
    """
    for favorite in favorites or ():
        resources = getattr(favorite, "resources", None)
        if not resources:
            continue
        if match := _SN_RE.search(resources[0].uri or ""):
            return int(match.group(1))
    return None


def build_track(
    song_id: str,
    album_id: str,
    sn: int,
    title: str = "",
    artist: str = "",
    album: str = "",
) -> DidlMusicTrack:
    """Build a queue item for a single Apple Music catalog track.

    `album_id` must be a real album catalog id. A placeholder parent is accepted
    into the queue and then silently refuses to start playback, which reads as a
    working enqueue right up until nothing plays.
    """
    from soco.data_structures import DidlMusicTrack, DidlResource

    uri = TRACK_URI.format(song_id=song_id, sid=SONOS_SERVICE_ID, sn=sn)
    return DidlMusicTrack(
        title=title,
        parent_id=TRACK_PARENT_ID.format(album_id=album_id),
        item_id=TRACK_ITEM_ID.format(song_id=song_id),
        desc=CDUDN,
        resources=[DidlResource(uri=uri, protocol_info=TRACK_PROTOCOL_INFO)],
        creator=artist or None,
        album=album or None,
    )


def build_container(kind: str, apple_id: str, sn: int, title: str = "") -> DidlObject:
    """Build a queue item for an Apple Music album or playlist.

    `parent_id` mirrors `item_id`, matching what the Sonos app writes into its own
    favorites; these classes serialise as <item>, not <container>.
    """
    from soco.data_structures import DidlMusicAlbum, DidlPlaylistContainer, DidlResource

    if kind == "album":
        item_id = ALBUM_CONTAINER_ID.format(album_id=apple_id)
        cls: type[DidlObject] = DidlMusicAlbum
    elif kind == "playlist":
        item_id = PLAYLIST_CONTAINER_ID.format(playlist_id=apple_id)
        cls = DidlPlaylistContainer
    else:
        raise ValueError(f"Not a container kind: {kind}")

    uri = CONTAINER_URI.format(item_id=item_id, sid=SONOS_SERVICE_ID, sn=sn)
    return cls(
        title=title,
        parent_id=item_id,
        item_id=item_id,
        desc=CDUDN,
        resources=[DidlResource(uri=uri, protocol_info=CONTAINER_PROTOCOL_INFO)],
    )


def build_station(station_id: str, sn: int, title: str = "") -> DidlAudioBroadcast:
    """Build a playable item for an Apple Music radio station.

    Stations are not queueable: the URI goes to the transport as CurrentURI,
    which is why `enqueue` routes them to `play_uri` and ignores the enqueue
    mode. `flags=0` and the `000c0000` prefix are what the Sonos app writes.
    """
    from soco.data_structures import DidlAudioBroadcast, DidlResource

    uri = STATION_URI.format(station_id=station_id, sid=SONOS_SERVICE_ID, sn=sn)
    return DidlAudioBroadcast(
        title=title,
        parent_id=STATION_PARENT_ID,
        item_id=STATION_ITEM_ID.format(station_id=station_id),
        desc=CDUDN,
        resources=[DidlResource(uri=uri, protocol_info=STATION_PROTOCOL_INFO)],
    )


def enqueue(
    soco: SoCo,
    item: DidlObject,
    enqueue_mode: MediaPlayerEnqueue,
    queue_position: int | None,
    timeout: float,
) -> None:
    """Add an item to the queue and start it, honouring the enqueue mode.

    Mirrors the structure of SonosMediaPlayerEntity._play_media so the four modes
    behave the same as they do for every other Sonos source. A container expands
    into several queue entries, so REPLACE starts at index 0 rather than at the
    position add_to_queue reports. Stations take the radio path instead, matching
    how core's own _play_favorite splits radio from everything else.
    """
    from soco.data_structures import DidlAudioBroadcast, to_didl_string

    if isinstance(item, DidlAudioBroadcast):
        # A station is a continuous stream with no queue entries to sit beside,
        # so every enqueue mode collapses to replacing what is playing.
        if enqueue_mode is not MediaPlayerEnqueue.REPLACE:
            _LOGGER.debug("Ignoring enqueue mode %s for a station", enqueue_mode)
        soco.play_uri(item.resources[0].uri, meta=to_didl_string(item), timeout=timeout)
        return

    if enqueue_mode is MediaPlayerEnqueue.ADD:
        soco.add_to_queue(item, timeout=timeout)
        return

    if enqueue_mode in (MediaPlayerEnqueue.NEXT, MediaPlayerEnqueue.PLAY):
        position = (queue_position or 0) + 1
        new_position = soco.add_to_queue(item, position=position, timeout=timeout)
        if enqueue_mode is MediaPlayerEnqueue.PLAY:
            soco.play_from_queue(new_position - 1)
        return

    soco.clear_queue()
    soco.add_to_queue(item, timeout=timeout)
    soco.play_from_queue(0)
