"""Fixtures for the Media Manifold tests."""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)
from soco.data_structures import DidlResource

from custom_components.manifold import GRAFTS
from custom_components.manifold.applemusic.api import AppleMusicClient, Page
from custom_components.manifold.applemusic.const import CONF_STOREFRONT, CONF_USER_TOKEN
from custom_components.manifold.const import DOMAIN

USER_TOKEN = "A" * 40 + "=="
STOREFRONT = "gb"

# Real ids from the Apple Music catalog, used throughout so a payload and the
# URI built from it can be compared by eye.
SONG_ID = "1097862231"
ALBUM_ID = "1097862062"
PLAYLIST_ID = "pl.ba2404fbc4464b8ba2d60399189cf24e"
STATION_ID = "ra.1478164763"
ARTIST_ID = "657515"
GENRE_ID = "1422614960"
# The artist views browse.py asks for, in the order it lists them.
ARTIST_VIEW_NAMES = (
    "top-songs",
    "full-albums",
    "singles",
    "appears-on-albums",
    "similar-artists",
)
RECOMMENDATION_ID = "6-27s5hU6IJ4pO4YMFRnYbHu"
GROUP_RECOMMENDATION_ID = "6-15489182170"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components/."""
    return


@pytest.fixture(autouse=True)
def no_leaked_grafts():
    """Take every graft back off after every test.

    Each replaces attributes on modules and classes Home Assistant owns. A test
    that fails between installing and removing would otherwise leave a wrapper on
    the real thing for the rest of the session, and every later failure would be
    a consequence of that one rather than its own fact. Every removal is a no-op
    when nothing is installed.
    """
    yield
    for graft in GRAFTS:
        graft.async_remove()


@contextlib.contextmanager
def no_grafts():
    """Every graft declining to install, for a household with nothing to patch.

    Patched by module rather than by name, so a graft added to `GRAFTS` is
    declined here without a test changing.
    """
    with contextlib.ExitStack() as stack:
        for graft in GRAFTS:
            stack.enter_context(patch.object(graft, "async_install", return_value=False))
        yield


# 840 bytes from a television playing Slow Horses season 6, episode 2, whose
# content id slices to season 3, episode 8: the case the numbering graft exists
# for.
SLOW_HORSES_ARCHIVE = (
    pathlib.Path(__file__).parent / "fixtures" / "appletv_nowplaying_slow_horses_s6e2.bin"
).read_bytes()


@pytest.fixture(autouse=True)
def no_token_scraping():
    """Never reach music.apple.com from a test.

    The developer token is scraped over the network, and every setup path asks
    for one; without this the suite would depend on Apple being reachable and on
    the shape of their JS bundle.
    """
    with patch(
        "custom_components.manifold.applemusic.dev_token.DeveloperToken.async_get",
        AsyncMock(return_value="developer-token"),
    ) as mocked:
        yield mocked


def song(song_id: str = SONG_ID, name: str = "Creep") -> dict[str, Any]:
    """An Apple Music songs resource."""
    return {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": name,
            "artistName": "Radiohead",
            "albumName": "Pablo Honey",
            "artwork": {"url": "https://example.invalid/{w}x{h}bb.jpg"},
        },
        "relationships": {"albums": {"data": [{"id": ALBUM_ID, "type": "albums"}]}},
    }


def album(album_id: str = ALBUM_ID, name: str = "Pablo Honey") -> dict[str, Any]:
    return {
        "id": album_id,
        "type": "albums",
        "attributes": {
            "name": name,
            "artwork": {"url": "https://example.invalid/{w}x{h}bb.jpg"},
        },
        "relationships": {"tracks": {"data": [song()]}},
    }


def playlist(playlist_id: str = PLAYLIST_ID) -> dict[str, Any]:
    return {
        "id": playlist_id,
        "type": "playlists",
        "attributes": {"name": "Hits in Spatial Audio"},
        "relationships": {"tracks": {"data": [song()]}},
    }


def station(station_id: str = STATION_ID) -> dict[str, Any]:
    return {
        "id": station_id,
        "type": "stations",
        "attributes": {"name": "HUNTR/X & Similar Artists"},
    }


def artist(artist_id: str = ARTIST_ID) -> dict[str, Any]:
    return {"id": artist_id, "type": "artists", "attributes": {"name": "Radiohead"}}


# What each artist view holds, so a view's items are the type the tree renders
# them as: songs for top-songs, artists for similar-artists, albums for the rest.
VIEW_ITEMS = {"top-songs": song, "similar-artists": artist}


def artist_with_views(
    artist_id: str = ARTIST_ID, views: tuple[str, ...] = ARTIST_VIEW_NAMES
) -> dict[str, Any]:
    """A catalog artist carrying the views Apple found something for."""
    return artist(artist_id) | {
        "views": {
            view: {"data": [VIEW_ITEMS.get(view, album)()]} for view in views
        }
    }


def station_genre(
    genre_id: str = GENRE_ID,
    name: str = "Alternative & Indie",
    stations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    genre: dict[str, Any] = {
        "id": genre_id,
        "type": "station-genres",
        "attributes": {"name": name},
    }
    if stations is not None:
        genre["relationships"] = {"stations": {"data": stations}}
    return genre


class FakeFavorite:
    """Minimal stand-in for soco's DidlFavorite."""

    def __init__(self, uri: str | None) -> None:
        self.resources = [DidlResource(uri=uri, protocol_info="x:*:*:*")] if uri else []


APPLE_FAVORITE = "x-rincon-cpcontainer:1006206cplaylist%3Apl.x?sid=204&flags=8300&sn=2"


def recommendation(
    rec_id: str = RECOMMENDATION_ID,
    title: str | None = "Heavy Rotation",
    contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A personal-recommendation shelf holding playable resources."""
    attributes: dict[str, Any] = {"reason": {"stringForDisplay": "Because you like"}}
    if title is not None:
        attributes["title"] = {"stringForDisplay": title}
    return {
        "id": rec_id,
        "type": "personal-recommendation",
        "attributes": attributes,
        "relationships": {
            "contents": {"data": contents if contents is not None else [album()]}
        },
    }


def group_recommendation(
    rec_id: str = GROUP_RECOMMENDATION_ID, title: str = "Made for You"
) -> dict[str, Any]:
    """A shelf whose contents are further shelves rather than resources."""
    return {
        "id": rec_id,
        "type": "personal-recommendation",
        "attributes": {
            "title": {"stringForDisplay": title},
            "isGroupRecommendation": True,
        },
        "relationships": {"recommendations": {"data": [recommendation()]}},
    }


class FakeClient:
    """Stands in for AppleMusicClient, recording the endpoints it is asked for.

    Routing lives in one place so a test can override a single endpoint with
    `responses[...]` without restating the rest of the catalog.
    """

    def __init__(self, *, user_token: str | None = USER_TOKEN) -> None:
        self._user_token = user_token
        self.storefront = STOREFRONT
        self.requested: list[tuple[str, dict]] = []
        self.responses: dict[str, Any] = {}
        self._page_limits: dict[str, int] = {}

    # The real page-size backoff, not a reimplementation of it: this fake stands
    # in for the transport, and a caller that skips the backoff is exactly the
    # kind of mistake these tests exist to catch.
    get_limited = AppleMusicClient.get_limited

    @property
    def has_user_token(self) -> bool:
        return bool(self._user_token)

    # Borrowed for the same reason as `get_limited`: which endpoint an id routes
    # to is a rule of the client, and a fake that restates it lets a change to
    # that rule leave every browse test passing against the old routing.
    resource_path = AppleMusicClient.resource_path

    async def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        self.requested.append((endpoint, params))
        if endpoint in self.responses:
            value = self.responses[endpoint]
            if isinstance(value, Exception):
                raise value
            return value
        return self._default(endpoint)

    async def get_paged(
        self, endpoint: str, limit: int, page_size: int = 100, **params: Any
    ) -> Page:
        page = await self.get(endpoint, limit=limit, **params)
        return Page(page.get("data", []), page.get("meta", {}).get("total"))

    async def async_resolve_storefront(self) -> str:
        return self.storefront

    def _default(self, endpoint: str) -> dict[str, Any]:
        if "/songs/" in endpoint:
            return {"data": [song()]}
        if "/albums/" in endpoint or endpoint.endswith("/albums"):
            return {"data": [album()]}
        if "/playlists/" in endpoint:
            return {"data": [playlist()]}
        if "/stations/" in endpoint:
            return {"data": [station()]}
        if "/view/" in endpoint:
            view = endpoint.rpartition("/")[2]
            return {"data": [VIEW_ITEMS.get(view, album)()]}
        if "/artists/" in endpoint:
            return {"data": [artist_with_views()]}
        if endpoint.endswith("/station-genres"):
            return {"data": [station_genre(), station_genre("1422616206", "Pop")]}
        if "/station-genres/" in endpoint:
            return {"data": [station_genre(stations=[station()])]}
        if endpoint.endswith("/stations"):
            return {"data": [station()]}
        if endpoint.endswith("charts"):
            return {
                "results": {
                    "songs": [{"name": "Top Songs", "data": [song()]}],
                    "albums": [{"name": "Top Albums", "data": [album()]}],
                    "playlists": [{"name": "Top Playlists", "data": [playlist()]}],
                }
            }
        if endpoint.endswith("search"):
            return {
                "results": {
                    "songs": {"data": [song()]},
                    "albums": {"data": [album()]},
                    "artists": {"data": [artist()]},
                    "playlists": {"data": [playlist()]},
                    "stations": {"data": [station()]},
                }
            }
        if endpoint == "me/recommendations":
            return {"data": [recommendation(), group_recommendation()]}
        if endpoint.startswith("me/recommendations/"):
            rec_id = endpoint.rpartition("/")[2]
            if rec_id == GROUP_RECOMMENDATION_ID:
                return {"data": [group_recommendation()]}
            return {"data": [recommendation(rec_id)]}
        if endpoint == "me/storefront":
            return {"data": [{"id": STOREFRONT}]}
        return {"data": [song()]}


@pytest.fixture
def client() -> FakeClient:
    """An Apple Music client with a user token, so library surfaces appear."""
    return FakeClient()


@pytest.fixture
def catalog_only_client() -> FakeClient:
    """An Apple Music client without a user token."""
    return FakeClient(user_token=None)


class FakeSoco:
    """Stands in for a speaker, recording the transport calls made against it.

    Shared so that a new call added to `play.enqueue` is visible to every test
    that drives a speaker, rather than to whichever file has the richer stub.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_position = 4

    def play_uri(self, uri, **kwargs):
        self.calls.append(("play_uri", {"uri": uri, **kwargs}))

    def add_to_queue(self, item, position=0, **kwargs):
        self.calls.append(("add_to_queue", {"item": item, "position": position}))
        return self.next_position

    def clear_queue(self):
        self.calls.append(("clear_queue", {}))

    def play_from_queue(self, index):
        self.calls.append(("play_from_queue", {"index": index}))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def resolved_storefront():
    """Answer the storefront lookup without reaching Apple."""
    with patch(
        "custom_components.manifold.applemusic.api.AppleMusicClient.async_resolve_storefront",
        AsyncMock(return_value=STOREFRONT),
    ) as mocked:
        yield mocked


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Media Manifold",
        data={CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: STOREFRONT},
    )


# --- SIMKL -------------------------------------------------------------------
#
# Shared so that the search's answer is written once. It replies with `simkl_id`
# and the scrobble endpoints expect `simkl`; confusing the two is answered with
# a 201 that records nothing, and a per-file copy of this reply would let two
# suites keep passing against a shape SIMKL no longer sends.

SIMKL_SEARCH = "https://api.simkl.com/search/tv"
SIMKL_ACTS = ("start", "pause", "stop")
SHOW_ID = 1624792

# What the Apple TV app reports while playing Black Bird, season 1, episode 4.
APPLE_TV_ATTRIBUTES = {
    "app_id": "com.apple.TVWatchList",
    "media_content_id": "A0054401004",
    "media_title": "Black Bird",
    "media_duration": 3600,
}


def scrobble_url(act: str) -> str:
    return f"https://api.simkl.com/scrobble/{act}"


def simkl_finds(aioclient_mock, show_id: int | None = SHOW_ID) -> None:
    """Answer the search the way SIMKL does — with `simkl_id`, not `simkl`."""
    ids = {"simkl_id": show_id} if show_id is not None else {}
    aioclient_mock.get(SIMKL_SEARCH, json=[{"title": "Black Bird", "ids": ids}])


def sent(aioclient_mock, method: str) -> list:
    """The requests of one method that reached SIMKL, in order."""
    return [call for call in aioclient_mock.mock_calls if call[0].lower() == method]


def add_player(hass, platform: str = "apple_tv", unique_id: str = "atv-1") -> str:
    """Register a media player the way its integration would."""
    entry = er.async_get(hass).async_get_or_create(
        "media_player", platform, unique_id, suggested_object_id=unique_id
    )
    return entry.entity_id


class Simkl:
    """A SIMKL that knows the show, and says when each act has reached it."""

    def __init__(self, aioclient_mock, stop_status: int = 200) -> None:
        self._mock = aioclient_mock
        self._stop_status = stop_status
        self.done = {act: asyncio.Event() for act in SIMKL_ACTS}
        simkl_finds(aioclient_mock)
        for act in SIMKL_ACTS:
            aioclient_mock.post(scrobble_url(act), side_effect=self._accept)

    async def _accept(self, method, url, data):
        act = url.path.rsplit("/", 1)[-1]
        self.done[act].set()
        if act == "stop" and self._stop_status != 200:
            return AiohttpClientMockResponse(
                method, url, status=self._stop_status, json={"error": "already_watched"}
            )
        return AiohttpClientMockResponse(method, url, json={"id": SHOW_ID})

    @property
    def posts(self) -> list:
        return sent(self._mock, "post")

    async def wait_for(self, hass, act: str = "start") -> list:
        """Wait for one act to reach SIMKL, and for its task to finish."""
        async with asyncio.timeout(1):
            await self.done[act].wait()
        await hass.async_block_till_done(wait_background_tasks=True)
        return self.posts

    async def nothing_sent(self, hass) -> bool:
        """Whether nothing was scrobbled, once everything in flight is done."""
        await hass.async_block_till_done(wait_background_tasks=True)
        return not self.posts


@pytest.fixture
def simkl(aioclient_mock) -> Simkl:
    return Simkl(aioclient_mock)


@pytest.fixture
def simkl_already_has_it(aioclient_mock) -> Simkl:
    """SIMKL answering a stop with the 409 it uses for an episode it holds."""
    return Simkl(aioclient_mock, stop_status=409)
