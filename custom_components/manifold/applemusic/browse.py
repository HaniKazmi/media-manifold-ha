"""Turn Apple Music API responses into BrowseMedia trees.

Content ids are namespaced ``apple-music://<kind>[/<id>]`` so the patched Sonos
browse function can route on the prefix and pass everything else through
untouched.

BrowseMedia has no paging, so each level is capped at PAGE_SIZE and the
remainder reported via ``not_shown``; depth comes from search rather than from
scrolling a level with thousands of children.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any
from urllib.parse import quote, unquote

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaType,
)

from .api import AppleMusicClient, AppleMusicError, artwork_url
from .const import LIVE_STATION_IDS, PAGE_SIZE, URI_PREFIX, is_library_id

_LOGGER = logging.getLogger(__name__)

ROOT_ID = f"{URI_PREFIX}root"

# Every Apple resource this tree renders: its content-id kind, how it appears in
# the browser, and whether it plays or expands. One row per resource is what
# keeps the item builders, the search request and the search result keys from
# drifting apart — a type present in one and missing from another renders
# nothing, with no error to notice.
#
# Library resources are the same shapes under a `library-` type name, so the
# prefix is stripped rather than given five more rows.
ITEM_KINDS: dict[str, tuple[str, MediaClass, MediaType, bool, bool]] = {
    # apple type: (kind, media_class, media_type, can_play, can_expand)
    "songs": ("song", MediaClass.TRACK, MediaType.TRACK, True, False),
    "albums": ("album", MediaClass.ALBUM, MediaType.ALBUM, True, True),
    # Artists expand but do not play — Sonos has no "play this artist" URI.
    "artists": ("artist", MediaClass.ARTIST, MediaType.ARTIST, False, True),
    "playlists": ("playlist", MediaClass.PLAYLIST, MediaType.PLAYLIST, True, True),
    # A station has no track list to browse: Sonos streams it continuously and
    # decides what comes next.
    "stations": ("station", MediaClass.MUSIC, MediaType.MUSIC, True, False),
}

# The artist views this tree offers, as content-id kind -> (label, Apple view).
# Labels are stated here rather than read from each view's own `attributes.title`
# so that a shelf reads the same in the menu as it does once opened; Apple's
# titles for the `gb` storefront are what they mirror.
_ARTIST_VIEWS: dict[str, tuple[str, str]] = {
    "artist_top_songs": ("Top Songs", "top-songs"),
    "artist_albums": ("Albums", "full-albums"),
    "artist_singles": ("Singles & EPs", "singles"),
    "artist_appears_on": ("Appears On", "appears-on-albums"),
    "artist_similar": ("Similar Artists", "similar-artists"),
}

# Apple's chart resource names, mapped to the labels it gives them.
_CHART_TYPES = {
    "songs": "Top Songs",
    "albums": "Top Albums",
    "playlists": "Top Playlists",
}

# Media classes that make sense as search results in this tree, and the Apple
# resource each one asks for.
_SEARCH_TYPES = {
    media_class: apple_type
    for apple_type, (_, media_class, *_) in ITEM_KINDS.items()
}
SEARCH_CLASSES = list(_SEARCH_TYPES)


def _id(kind: str, value: str = "") -> str:
    return f"{URI_PREFIX}{kind}" + (f"/{value}" if value else "")


def parse_content_id(content_id: str) -> tuple[str, str]:
    """Split a namespaced content id into (kind, value)."""
    rest = content_id.removeprefix(URI_PREFIX)
    kind, _, value = rest.partition("/")
    return kind, unquote(value)


def _directory(title: str, kind: str, value: str = "") -> BrowseMedia:
    return BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id=_id(kind, value),
        media_content_type=MediaType.PLAYLIST,
        can_play=False,
        can_expand=True,
        # Opt this subtree into the frontend's search box. Sonos sets can_search
        # nowhere, so without this there is no search input anywhere in its
        # browser; the flag only takes effect below the root.
        can_search=True,
        search_media_classes=SEARCH_CLASSES,
    )


def item_payload(item: dict[str, Any]) -> BrowseMedia | None:
    """Build a child for any Apple Music resource, or None if unrecognised."""
    apple_type = item.get("type", "")
    spec = ITEM_KINDS.get(apple_type.removeprefix("library-"))
    if spec is None:
        _LOGGER.debug("Skipping unrecognised Apple Music type %s", apple_type)
        return None

    kind, media_class, media_type, can_play, can_expand = spec
    attributes = item.get("attributes", {})
    return BrowseMedia(
        title=attributes.get("name", "Unknown"),
        media_class=media_class,
        media_content_id=_id(kind, quote(item["id"])),
        media_content_type=media_type,
        can_play=can_play,
        can_expand=can_expand,
        # Only a node that can be opened can be searched within, so the two
        # flags move together rather than being set per type.
        can_search=can_expand,
        search_media_classes=SEARCH_CLASSES if can_expand else None,
        thumbnail=artwork_url(attributes.get("artwork")),
    )


def _children(
    items: list[dict],
    total: int | None = None,
    build: Callable[[dict[str, Any]], BrowseMedia | None] | None = None,
) -> tuple[list[BrowseMedia], int]:
    """Render a page of items, and count what did not survive rendering.

    `build` is how a shelf level renders its rows. The count of what is not
    shown has to come out the same either way, so both go through here rather
    than restating an arithmetic that is easy to copy wrong.
    """
    build = build or item_payload
    children = [child for item in items if (child := build(item))]
    not_shown = max(0, (total or len(items)) - len(children))
    return children, not_shown


async def _paged_listing(
    client: AppleMusicClient, endpoint: str, title: str, content_id: str
) -> BrowseMedia:
    """One level built straight from a paged endpoint."""
    page = await client.get_paged(endpoint, PAGE_SIZE)
    children, not_shown = _children(page.items, page.total)
    return _listing(title, content_id, children, not_shown)


def root_payload(client: AppleMusicClient) -> BrowseMedia:
    """The Apple Music node grafted onto the Sonos root.

    Library entries are omitted without a user token rather than shown and
    failing, so the tree always reflects what actually works.
    """
    children = [_directory("Charts", "charts"), _directory("Radio", "radio")]
    if client.has_user_token:
        children += [
            _directory("Your Library", "library"),
            _directory("Recently Played", "recent"),
            _directory("Made for You", "recommendations"),
        ]

    return BrowseMedia(
        title="Apple Music",
        media_class=MediaClass.DIRECTORY,
        media_content_id=ROOT_ID,
        media_content_type=MediaType.PLAYLIST,
        can_play=False,
        can_expand=True,
        can_search=True,
        search_media_classes=SEARCH_CLASSES,
        children=children,
        children_media_class=MediaClass.DIRECTORY,
        thumbnail="https://music.apple.com/favicon.ico",
    )


async def async_browse(client: AppleMusicClient, content_id: str) -> BrowseMedia:
    """Resolve one namespaced content id to a BrowseMedia payload."""
    kind, value = parse_content_id(content_id)

    try:
        return await _async_browse(client, kind, value)
    except AppleMusicError as err:
        raise BrowseError(f"Apple Music: {err}") from err


async def _async_browse(
    client: AppleMusicClient, kind: str, value: str
) -> BrowseMedia:
    if kind == "root":
        return root_payload(client)

    if kind == "library":
        return _listing(
            "Your Library",
            _id("library"),
            [
                _directory("Playlists", "library_playlists"),
                _directory("Albums", "library_albums"),
                _directory("Artists", "library_artists"),
                _directory("Songs", "library_songs"),
            ],
        )

    if kind.startswith("library_"):
        resource = kind.removeprefix("library_")
        return await _paged_listing(
            client, f"me/library/{resource}", kind.replace("_", " ").title(), _id(kind)
        )

    if kind == "recent":
        # /tracks yields songs, which is what "recently played" usually means
        # here; the bare me/recent/played returns albums, playlists and stations
        # instead.
        return await _paged_listing(
            client, "me/recent/played/tracks", "Recently Played", _id(kind)
        )

    if kind == "recommendations":
        return await _recommendations(client)

    if kind == "recommendation":
        return await _recommendation(client, value)

    if kind == "charts":
        return await _charts(client, value)

    if kind in ("album", "playlist"):
        return await _container(client, kind, value)

    if kind == "artist":
        return await _artist(client, value)

    if kind.startswith("artist_"):
        return await _artist_view(client, kind, value)

    if kind == "radio":
        return await _radio(client)

    if kind == "station_genre":
        return await _station_genre(client, value)

    raise BrowseError(f"Apple Music: cannot browse {kind}")


def _listing(
    title: str, content_id: str, children: list[BrowseMedia], not_shown: int = 0
) -> BrowseMedia:
    payload = BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id=content_id,
        media_content_type=MediaType.PLAYLIST,
        can_play=False,
        can_expand=True,
        can_search=True,
        search_media_classes=SEARCH_CLASSES,
        children=children,
        children_media_class=MediaClass.TRACK,
        not_shown=not_shown,
    )
    if children:
        # Home Assistant's own count: one class when the children agree, and
        # DIRECTORY when they do not. Radio lists stations before its genre
        # directories, and a shelf lists nested shelves before albums, so
        # reading the first child's class labels most of both levels wrongly.
        payload.calculate_children_class()
    return payload


async def _container(
    client: AppleMusicClient, kind: str, value: str
) -> BrowseMedia:
    """An album or playlist, listing its tracks and playable as a whole."""
    detail = await client.get(client.resource_path(f"{kind}s", value), include="tracks")
    data = detail.get("data", [])
    if not data:
        raise BrowseError(f"Apple Music: no such {kind} {value}")

    attributes = data[0].get("attributes", {})
    tracks = data[0].get("relationships", {}).get("tracks", {})
    children, not_shown = _children(
        tracks.get("data", []), tracks.get("meta", {}).get("total")
    )

    # Read from the table rather than restated: stating a kind's classes twice
    # is the drift that table exists to prevent.
    _, media_class, media_type, _, _ = ITEM_KINDS[f"{kind}s"]
    return BrowseMedia(
        title=attributes.get("name", kind.title()),
        media_class=media_class,
        media_content_id=_id(kind, quote(value)),
        media_content_type=media_type,
        can_play=True,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.TRACK,
        not_shown=not_shown,
        thumbnail=artwork_url(attributes.get("artwork")),
    )


async def _artist(client: AppleMusicClient, value: str) -> BrowseMedia:
    """An artist's sections, or a library artist's albums.

    Only catalog artists have views. A library artist has one relationship and
    no views at all, so it renders as the album list it actually is rather than
    as a menu of sections that each answer 404.
    """
    if is_library_id(value):
        return await _paged_listing(
            client,
            f"me/library/artists/{value}/albums",
            "Albums",
            _id("artist", quote(value)),
        )

    detail = await client.get(
        f"catalog/{client.storefront}/artists/{value}",
        views=",".join(view for _, view in _ARTIST_VIEWS.values()),
    )
    data = detail.get("data", [])
    if not data:
        raise BrowseError(f"Apple Music: no such artist {value}")

    # Asking for every view costs one request and says which of them hold
    # anything, so a section Apple has nothing for is never offered. Plenty of
    # artists have no "Appears On" and no similar artists.
    views = data[0].get("views", {})
    children = [
        _directory(label, kind, quote(value))
        for kind, (label, view) in _ARTIST_VIEWS.items()
        if views.get(view, {}).get("data")
    ]
    title = data[0].get("attributes", {}).get("name", "Artist")
    return _listing(title, _id("artist", quote(value)), children)


async def _artist_view(
    client: AppleMusicClient, kind: str, value: str
) -> BrowseMedia:
    """One section of an artist, paged in its own right."""
    if (spec := _ARTIST_VIEWS.get(kind)) is None:
        raise BrowseError(f"Apple Music: no such artist section {kind}")
    label, view = spec
    return await _paged_listing(
        client,
        f"catalog/{client.storefront}/artists/{value}/view/{view}",
        label,
        _id(kind, quote(value)),
    )


async def _radio(client: AppleMusicClient) -> BrowseMedia:
    """Apple's radio: the live broadcasts, your own station, and the genres."""
    # None of the three depends on another, so the level costs the slowest of
    # them rather than the sum of all three. `_stations` answers [] on failure,
    # so the genre request is still the only one that can raise.
    requests: list[Any] = []
    if client.has_user_token:
        # Apple builds this one from listening history, so it exists only for an
        # account that has one; asking without a cookie answers 403.
        requests.append(
            _stations(client, "personal", **{"filter[identity]": "personal"})
        )
    requests.append(_stations(client, "live", ids=",".join(LIVE_STATION_IDS)))
    requests.append(client.get(f"catalog/{client.storefront}/station-genres"))

    *station_lists, genres = await asyncio.gather(*requests)

    children: list[BrowseMedia] = [
        station for stations in station_lists for station in stations
    ]
    children += [
        _directory(name, "station_genre", quote(genre["id"]))
        for genre in genres.get("data", [])
        if (name := genre.get("attributes", {}).get("name"))
    ]
    return _listing("Radio", _id("radio"), children)


async def _stations(
    client: AppleMusicClient, description: str, **params: Any
) -> list[BrowseMedia]:
    """Stations from one filtered catalog request, or none if it fails.

    A radio surface Apple declines is dropped from the node rather than failing
    it. Which surfaces an account has is not knowable before asking, and the
    genres alone still make the node worth opening.
    """
    try:
        result = await client.get(f"catalog/{client.storefront}/stations", **params)
    except AppleMusicError as err:
        _LOGGER.debug("Leaving out the %s stations: %s", description, err)
        return []
    children, _ = _children(result.get("data", []))
    return children


async def _station_genre(client: AppleMusicClient, value: str) -> BrowseMedia:
    """One radio genre and its stations.

    The stations arrive with the genre rather than from the relationship
    endpoint, because the genre's own name is what titles this node and the
    relationship endpoint does not carry it.
    """
    detail = await client.get(
        f"catalog/{client.storefront}/station-genres/{value}",
        include="stations",
        **{"limit[stations]": PAGE_SIZE},
    )
    data = detail.get("data", [])
    if not data:
        raise BrowseError(f"Apple Music: no such radio genre {value}")

    stations = data[0].get("relationships", {}).get("stations", {}).get("data", [])
    children, not_shown = _children(stations)
    return _listing(
        data[0].get("attributes", {}).get("name", "Radio"),
        _id("station_genre", quote(value)),
        children,
        not_shown,
    )


async def _charts(client: AppleMusicClient, value: str = "") -> BrowseMedia:
    """Chart types as a menu, or one chart's entries.

    Each section is a real content id rather than a pre-expanded child list: the
    frontend re-requests a node by id when it is opened, so a section that only
    exists as inlined children is a dead end.
    """
    if not value:
        return _listing(
            "Charts",
            _id("charts"),
            [_directory(label, "charts", key) for key, label in _CHART_TYPES.items()],
        )

    if value not in _CHART_TYPES:
        raise BrowseError(f"Apple Music: no {value} chart")

    # Through get_limited so a chart endpoint capped below PAGE_SIZE backs off
    # like every other level; a bare 400 here takes out the whole Charts node.
    result, _ = await client.get_limited(
        f"catalog/{client.storefront}/charts", PAGE_SIZE, types=value
    )
    charts = result.get("results", {}).get(value, [])
    items = charts[0].get("data", []) if charts else []
    children, not_shown = _children(items)
    return _listing(_CHART_TYPES[value], _id("charts", value), children, not_shown)


def _shelf_title(recommendation: dict[str, Any]) -> str:
    """The label Apple gives a shelf.

    `reason` is what the Music app shows under a title-less shelf ("Because you
    listened to Radiohead"), so it stands in before the constant does: a shelf
    named "Recommended" is indistinguishable from every other one.
    """
    attributes = recommendation.get("attributes", {})
    for key in ("title", "reason"):
        if display := attributes.get(key, {}).get("stringForDisplay"):
            return display
    return "Recommended"


def _shelf(recommendation: dict[str, Any]) -> BrowseMedia | None:
    """One recommendation as a directory, or None if it has no id to open."""
    if not (apple_id := recommendation.get("id")):
        return None
    return _directory(_shelf_title(recommendation), "recommendation", quote(apple_id))


async def _recommendations(client: AppleMusicClient) -> BrowseMedia:
    """The recommendation shelves as a menu.

    Flattening the shelves into one list loses the labels that give the items
    their meaning — "Heavy Rotation" and "New Releases for You" are the same
    albums otherwise — and spends the level's single cap on whichever shelves
    happen to come first.
    """
    page = await client.get_paged("me/recommendations", PAGE_SIZE)
    children, not_shown = _children(page.items, page.total, _shelf)
    return _listing("Made for You", _id("recommendations"), children, not_shown)


async def _recommendation(client: AppleMusicClient, value: str) -> BrowseMedia:
    """One shelf: its own contents, plus any shelves nested inside it.

    A group recommendation holds shelves rather than resources, and carries them
    under a different relationship. Rendering both means the flag saying which
    kind this is need not be present or trusted.
    """
    detail = await client.get(f"me/recommendations/{value}")
    data = detail.get("data", [])
    if not data:
        raise BrowseError(f"Apple Music: no such recommendation {value}")

    relationships = data[0].get("relationships", {})
    nested = relationships.get("recommendations", {}).get("data", [])
    contents = relationships.get("contents", {}).get("data", [])
    children, _ = _children(nested, build=_shelf)
    items, not_shown = _children(contents[:PAGE_SIZE], len(contents))
    children.extend(items)

    return _listing(
        _shelf_title(data[0]), _id("recommendation", quote(value)), children, not_shown
    )


async def async_search(
    client: AppleMusicClient, term: str, media_class: str | None = None
) -> list[BrowseMedia]:
    """Search the Apple Music catalog and return flat BrowseMedia results."""
    types = _SEARCH_TYPES.get(media_class) or ",".join(_SEARCH_TYPES.values())

    result = await client.get(
        f"catalog/{client.storefront}/search",
        term=term,
        types=types,
        limit=min(PAGE_SIZE, 25),
    )
    # Apple returns one section per requested type, in the order they were
    # asked for, so the sections need no second list naming them here.
    children: list[BrowseMedia] = []
    for section in result.get("results", {}).values():
        found, _ = _children(section.get("data", []))
        children.extend(found)
    return children
