"""Infuse URLs for Jellyfin items.

Infuse's third-party API offers two ways to start playback, and they differ in
what they leave behind:

- ``infuse://movie/{tmdb}`` and ``infuse://series/{tmdb}[-{season}[-{episode}]]``
  address an item Infuse already holds in its own library. Playback stays inside
  that library, which is what carries watched and resume state back to the
  Jellyfin server Infuse is connected to.
- ``infuse://x-callback-url/play?url=`` plays any URL and needs no library entry,
  but reports nothing back: a film watched this way stays unwatched in Jellyfin.

So the deep link comes first, and direct play is the fallback for items with no
TMDB id — Jellyfin matches most things against TMDB, but home videos and
unmatched files have none, and those are exactly the items that would otherwise
have no way to play at all.

This module owns the vocabulary of both sides — which Jellyfin keys carry the
facts, and what Infuse does with them. It imports nothing from Home Assistant,
so every rule here is checkable against a dict without a server or a running
instance behind it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

# Jellyfin measures playback position in 100-nanosecond ticks; Infuse's position
# parameter is in seconds.
TICKS_PER_SECOND = 10_000_000

# Season and Episode carry only their own numbering, so both need the parent
# series' TMDB id before they can be addressed.
_CHILD_TYPES = frozenset({"Season", "Episode"})


def is_video(item: dict[str, Any]) -> bool:
    """Whether Infuse is the right player for an item.

    The Apple TV plays Jellyfin audio over RAOP already, and Infuse is a video
    player: handed a track it opens on nothing. The graft therefore claims only
    what it can improve on.
    """
    return item.get("MediaType") == "Video"


def display_name(item: dict[str, Any]) -> str | None:
    """What to call an item in an error, or in Infuse's own player."""
    return item.get("Name")


def tmdb_id(item: dict[str, Any]) -> str | None:
    """Return an item's TMDB id, or None when Jellyfin matched it against nothing."""
    return (item.get("ProviderIds") or {}).get("Tmdb")


def series_id(item: dict[str, Any]) -> str | None:
    """Return the parent series to look up before this item's link can be built."""
    if item.get("Type") in _CHILD_TYPES:
        return item.get("SeriesId")
    return None


def deep_link(item: dict[str, Any], series_tmdb: str | None = None) -> str | None:
    """Return the ``infuse://`` library link for an item, or None if it has none.

    None is the signal to fall back to direct play rather than an error: it means
    only that Infuse's library cannot name this item, which is routine for
    anything TMDB does not carry.
    """
    item_type = item.get("Type")

    if item_type == "Movie":
        if (movie := tmdb_id(item)) is None:
            return None
        return f"infuse://movie/{movie}?play"

    if item_type == "Series":
        if (series := tmdb_id(item)) is None:
            return None
        return f"infuse://series/{series}?play"

    if item_type not in _CHILD_TYPES or series_tmdb is None:
        return None

    if item_type == "Season":
        if (season := item.get("IndexNumber")) is None:
            return None
        return f"infuse://series/{series_tmdb}-{season}?play"

    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    if season is None or episode is None:
        return None
    return f"infuse://series/{series_tmdb}-{season}-{episode}?play"


def resume_position(item: dict[str, Any]) -> int | None:
    """Return where to resume an item in whole seconds, or None to start over.

    A position under a second is not a resume point; it is a film that was opened
    and closed, and passing it would seek to zero anyway.
    """
    ticks = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
    return seconds if (seconds := int(ticks) // TICKS_PER_SECOND) else None


def direct_play(
    stream_url: str,
    *,
    filename: str | None = None,
    position: int | None = None,
) -> str:
    """Return an ``infuse://x-callback-url/play`` URL for a direct stream.

    ``quote`` rather than the default ``quote_plus``: the value being wrapped is
    itself a URL with its own query string, and a ``+`` there would be read back
    as a literal plus by anything that unquotes it strictly.
    """
    params: dict[str, str | int] = {"url": stream_url}
    if filename:
        params["filename"] = filename
    if position:
        params["position"] = position
    return "infuse://x-callback-url/play?" + urlencode(params, quote_via=quote)
