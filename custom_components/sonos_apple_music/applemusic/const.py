"""Constants for Apple Music, and for the Sonos service that carries it."""

from __future__ import annotations

from typing import Final

# Apple Music's Sonos music-service id, and the RINCON service type derived from
# it as (id << 8) + 7. Both are read back from any Apple Music favorite on the
# household, so they are checkable rather than guessed.
SONOS_SERVICE_ID: Final = 204
SONOS_SERVICE_TYPE: Final = (SONOS_SERVICE_ID << 8) + 7  # 52231

# The <desc id="cdudn"> value binding a queue item to the household's Apple Music
# account. This exact form is what the Sonos app writes into its own favorites and
# is the only one confirmed to play; soco defaults `desc` to RINCON_AssociatedZPUDN,
# so every item we build must pass desc= explicitly.
CDUDN: Final = f"SA_RINCON{SONOS_SERVICE_TYPE}_X_#Svc{SONOS_SERVICE_TYPE}-0-Token"

# Account serial. Sonos S2 no longer exposes /status/accounts, so this is
# recovered from an existing Apple Music favorite's res URI; DEFAULT_SN is only a
# fallback. sn=0 is rejected by services that require a binding.
DEFAULT_SN: Final = 1

# Track playback. The hls-static form defers Apple-side resolution to play time;
# the older x-sonos-http:...mp4 form validates every track at enqueue time and
# makes bulk adds crawl. Speakers rewrite flags 8232 -> 73768 in the URI they
# report back, so never match on flags when recognising our own tracks.
TRACK_URI: Final = "x-sonosapi-hls-static:song%3a{song_id}?sid={sid}&flags=8232&sn={sn}"
TRACK_ITEM_ID: Final = "00032020song%3a{song_id}"
TRACK_PARENT_ID: Final = "0004206calbum%3a{album_id}"
TRACK_PROTOCOL_INFO: Final = "x-rincon-playlist:*:*:*"

# Container playback. add_to_queue expands these server-side into real queue
# entries; play_uri answers UPnP 714 "Illegal MIME-Type" for every cpcontainer
# form, which is why containers never go through it.
CONTAINER_URI: Final = "x-rincon-cpcontainer:{item_id}?sid={sid}&flags=8300&sn={sn}"
CONTAINER_PROTOCOL_INFO: Final = "x-rincon-cpcontainer:*:*:*"
ALBUM_CONTAINER_ID: Final = "1004206calbum%3A{album_id}"
PLAYLIST_CONTAINER_ID: Final = "1006206cplaylist%3A{playlist_id}"

# Station playback. Stations are radio: the URI is set as the transport's
# CurrentURI rather than queued, and flags is 0 rather than the track/container
# values. Sonos itself writes no <res> into the metadata — the URI travels
# separately — though it accepts one, and both forms have been confirmed to play.
STATION_URI: Final = "x-sonosapi-radio:radio%3a{station_id}?sid={sid}&flags=0&sn={sn}"
STATION_ITEM_ID: Final = "000c0000radio%3a{station_id}"
STATION_PARENT_ID: Final = "-1"
STATION_PROTOCOL_INFO: Final = "x-sonosapi-radio:*:*:*"

# Apple's live broadcast stations. They belong to no station genre and no
# filtered listing reaches them, so the only way to offer them is by id.
# Fetched rather than hardcoded beyond the id, so their names and artwork come
# from Apple; an id Apple retires is simply absent from the response, which is
# what a fourth id already in this list would look like.
LIVE_STATION_IDS: Final = (
    "ra.978194965",  # Apple Music 1
    "ra.1498155548",  # Apple Music Hits
    "ra.1498157166",  # Apple Music Country
)

# Apple Music API. The developer token is origin-locked to apple.com, so ORIGIN
# must be sent on every request or the API answers 401.
API_BASE: Final = "https://api.music.apple.com/v1"
ORIGIN: Final = "https://music.apple.com"
WEB_PLAYER_URL: Final = "https://music.apple.com/us/browse"
# The web player bundle carries three ES256 JWTs; only the one issued by
# AMPWebPlay is accepted by the API. The other two look valid and answer 401.
DEV_TOKEN_ISSUER: Final = "AMPWebPlay"

CONF_USER_TOKEN: Final = "music_user_token"
CONF_STOREFRONT: Final = "storefront"
DEFAULT_STOREFRONT: Final = "gb"

# BrowseMedia has no paging, so every level is capped and the remainder is
# reported via not_shown rather than flooding the websocket.
PAGE_SIZE: Final = 48
# Apple caps the me/recent/* endpoints well below the catalog ones and answers a
# bare 400 when the limit is exceeded, with nothing naming the offending field.
RECENT_PAGE_SIZE: Final = 30
# The page size every Apple Music endpoint documented so far accepts, used as the
# retry size when one rejects a larger request.
SAFE_PAGE_SIZE: Final = 10

URI_PREFIX: Final = "apple-music://"

# Apple's library-scoped id prefixes, as `me/library/*` hands them back: songs
# `i.`, albums `l.`, artists `r.`, playlists `p.`. Catalog ids are bare numbers,
# or `pl.` for a playlist.
#
# `pl.u-` belongs to the catalog side despite naming a user's own playlist: it
# is what a library playlist carries in playParams.globalId, and it is what
# Sonos is handed after translation. Against a live account,
# catalog/gb/playlists/pl.u-06oxDj6to1qMmx answers 200 and
# me/library/playlists/pl.u-06oxDj6to1qMmx answers 404, with p.rXAJKVahZNRX4g
# the exact reverse. Listing `pl.u-` here sends a translated id back to the
# library endpoint it came from; omitting `p.` sends every library playlist to
# a catalog endpoint that 404s. `p.` does not match `pl.` — the dot separates
# them — so editorial playlists still reach the catalog.
LIBRARY_PREFIXES: Final = ("i.", "l.", "r.", "p.")


def is_library_id(value: str) -> bool:
    """Whether an Apple id belongs to the user's library rather than the catalog.

    The two need different endpoints, and Sonos rejects library ids outright.
    """
    return value.startswith(LIBRARY_PREFIXES)
