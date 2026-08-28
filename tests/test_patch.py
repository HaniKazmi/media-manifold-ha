"""Grafting onto the Sonos integration.

These wrap functions Home Assistant owns, so the tests import the real Sonos
modules rather than stubs — an upstream rename should fail here, not in
someone's living room.

Where a test needs to see what reaches Sonos, it replaces the Sonos function
*before* installing, so the wrapper captures the replacement as its original.
Reaching into the patch module's saved originals afterwards would test the test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_ENQUEUE,
    BrowseMedia,
    MediaClass,
    MediaPlayerEnqueue,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.sonos import media_browser
from homeassistant.components.sonos.media_player import SonosMediaPlayerEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.sonos_apple_music.applemusic.api import AppleMusicError
from custom_components.sonos_apple_music.applemusic.const import DEFAULT_SN, URI_PREFIX
from custom_components.sonos_apple_music.applemusic.patch import (
    async_install,
    async_remove,
)
from custom_components.sonos_apple_music.const import DOMAIN
from custom_components.sonos_apple_music.data import RuntimeData

from .conftest import ALBUM_ID, APPLE_FAVORITE, SONG_ID, FakeFavorite, FakeSoco


def directory(title: str, content_type: str = "favorites") -> BrowseMedia:
    return BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id="",
        media_content_type=content_type,
        can_play=False,
        can_expand=True,
    )


def sonos_root(*titles: str) -> BrowseMedia:
    """What sonos's own root_payload returns when several sources exist."""
    root = directory("Sonos", "root")
    root.children = [directory(title) for title in titles]
    return root


@pytest.fixture
def sonos(monkeypatch):
    """Stand in for the Sonos functions, and record what reaches them."""
    calls = SimpleNamespace(
        browse=[], play=[], search=[], browse_result=sonos_root("Favorites")
    )

    async def browse(*args, **kwargs):
        calls.browse.append(args)
        return calls.browse_result

    async def play(self, media_type, media_id, **kwargs):
        calls.play.append((media_type, media_id, kwargs))

    async def search(self, query):
        calls.search.append(query)
        return SearchMedia(result=[])

    monkeypatch.setattr(media_browser, "async_browse_media", browse)
    monkeypatch.setattr(SonosMediaPlayerEntity, "async_play_media", play)
    monkeypatch.setattr(SonosMediaPlayerEntity, "async_search_media", search)
    return calls


@pytest.fixture
def installed(hass, client, sonos):
    """Patch Sonos for the duration of a test, and always put it back."""
    hass.data[DOMAIN] = RuntimeData(apple_music=client)
    assert async_install(hass) is True
    yield sonos
    async_remove()
    hass.data.pop(DOMAIN, None)


class FakeEntity:
    """The parts of SonosMediaPlayerEntity the play wrapper touches."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.soco = FakeSoco()
        self.coordinator = SimpleNamespace(soco=self.soco)
        self.speaker = SimpleNamespace(favorites=[])
        self.media = SimpleNamespace(queue_position=0)

    @property
    def enqueued(self) -> list[Any]:
        """Whatever was handed to the speaker, queued or set as the transport."""
        return [
            call.get("item") or call.get("uri")
            for name, call in self.soco.calls
            if name in ("add_to_queue", "play_uri")
        ]


def test_install_replaces_and_remove_restores(hass, client, sonos) -> None:
    """A removed add-on must leave no trace in an integration it does not own."""
    before = (
        media_browser.async_browse_media,
        SonosMediaPlayerEntity.async_play_media,
        SonosMediaPlayerEntity.async_search_media,
    )
    hass.data[DOMAIN] = RuntimeData(apple_music=client)
    async_install(hass)
    assert media_browser.async_browse_media is not before[0]

    async_remove()
    assert media_browser.async_browse_media is before[0]
    assert SonosMediaPlayerEntity.async_play_media is before[1]
    assert SonosMediaPlayerEntity.async_search_media is before[2]


def test_install_is_idempotent(hass, client, sonos) -> None:
    """A second install must not capture the already-patched function as the
    original, which would leave the patch in place after remove."""
    before = media_browser.async_browse_media
    hass.data[DOMAIN] = RuntimeData(apple_music=client)

    async_install(hass)
    patched = media_browser.async_browse_media
    async_install(hass)
    assert media_browser.async_browse_media is patched

    async_remove()
    assert media_browser.async_browse_media is before


async def test_an_apple_id_is_served_by_us(hass, installed) -> None:
    payload = await media_browser.async_browse_media(
        hass, None, None, None, f"{URI_PREFIX}root", "directory"
    )
    assert payload.title == "Apple Music"
    assert not installed.browse, "an Apple id must not reach Sonos"


async def test_another_id_is_delegated_untouched(hass, installed) -> None:
    """Everything Sonos already browses must reach it with its arguments intact."""
    await media_browser.async_browse_media(hass, "spk", "med", "img", "A:ALBUM", "album")

    assert len(installed.browse) == 1
    # The upstream order puts media_content_id before media_content_type.
    assert installed.browse[0][4] == "A:ALBUM"
    assert installed.browse[0][5] == "album"


async def test_the_root_gains_an_apple_music_node(hass, installed) -> None:
    installed.browse_result = sonos_root("Favorites", "Music Library")

    payload = await media_browser.async_browse_media(
        hass, "spk", "med", "img", None, None
    )

    assert [child.title for child in payload.children] == [
        "Favorites",
        "Music Library",
        "Apple Music",
    ]


async def test_a_collapsed_root_is_rebuilt(hass, installed) -> None:
    """sonos's root_payload returns its single child directly when only one
    source exists. Appending to that child would nest Apple Music inside
    Favorites instead of beside it."""
    installed.browse_result = directory("Favorites")

    payload = await media_browser.async_browse_media(
        hass, "spk", "med", "img", None, None
    )

    assert payload.media_content_type == "root"
    assert [child.title for child in payload.children] == ["Favorites", "Apple Music"]


async def test_play_builds_and_enqueues_an_apple_item(hass, installed) -> None:
    entity = FakeEntity(hass)
    await SonosMediaPlayerEntity.async_play_media(
        entity, "music", f"{URI_PREFIX}song/{SONG_ID}"
    )

    assert entity.enqueued[0].item_id == f"00032020song%3a{SONG_ID}"
    assert not installed.play


def issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, "no_apple_music_favorite")


async def test_playing_without_a_favorite_says_why_it_may_be_silent(
    hass, installed
) -> None:
    """The serial is only observable in URIs the Sonos app has already written.

    Guessed wrong, the item queues cleanly and never sounds — a failure with no
    error attached to it, which is the one thing a user cannot debug.
    """
    entity = FakeEntity(hass)
    await SonosMediaPlayerEntity.async_play_media(
        entity, "music", f"{URI_PREFIX}song/{SONG_ID}"
    )

    assert issue(hass) is not None
    assert f"sn={DEFAULT_SN}" in entity.enqueued[0].resources[0].uri


async def test_a_favorite_settles_the_serial_and_clears_the_warning(
    hass, installed
) -> None:
    """The household that adds the favorite must not keep being told to."""
    entity = FakeEntity(hass)
    await SonosMediaPlayerEntity.async_play_media(
        entity, "music", f"{URI_PREFIX}song/{SONG_ID}"
    )
    assert issue(hass) is not None

    entity.speaker.favorites = [FakeFavorite(APPLE_FAVORITE)]
    await SonosMediaPlayerEntity.async_play_media(
        entity, "music", f"{URI_PREFIX}song/{SONG_ID}"
    )

    assert issue(hass) is None
    assert "sn=2" in entity.enqueued[-1].resources[0].uri
    # A household whose playback enqueues and stays silent has no other record
    # of which serial that playback carried.
    assert hass.data[DOMAIN].account_serial == 2


async def test_play_delegates_anything_else(hass, installed) -> None:
    """A Sonos favorite or a media-source URL must not touch our code path."""
    await SonosMediaPlayerEntity.async_play_media(
        FakeEntity(hass), "music", "http://example.invalid/x.mp3"
    )

    assert installed.play == [("music", "http://example.invalid/x.mp3", {})]


async def test_play_passes_the_enqueue_mode_through(hass, installed) -> None:
    entity = FakeEntity(hass)
    await SonosMediaPlayerEntity.async_play_media(
        entity,
        "music",
        f"{URI_PREFIX}album/{ALBUM_ID}",
        **{ATTR_MEDIA_ENQUEUE: MediaPlayerEnqueue.ADD},
    )
    assert entity.enqueued


async def test_an_unplayable_item_surfaces_as_a_home_assistant_error(
    hass, installed
) -> None:
    """A raw exception here shows the user a traceback rather than a reason."""
    with pytest.raises(HomeAssistantError):
        await SonosMediaPlayerEntity.async_play_media(
            FakeEntity(hass), "music", f"{URI_PREFIX}artist/123"
        )


async def test_search_routes_apple_queries_to_apple(hass, installed) -> None:
    result = await SonosMediaPlayerEntity.async_search_media(
        FakeEntity(hass),
        SearchMediaQuery(search_query="radiohead", media_content_id=f"{URI_PREFIX}root"),
    )
    assert result.result
    assert not installed.search


async def test_search_delegates_sonos_queries(hass, installed) -> None:
    await SonosMediaPlayerEntity.async_search_media(
        FakeEntity(hass), SearchMediaQuery(search_query="x", media_content_id="A:ALBUM")
    )
    assert len(installed.search) == 1


async def test_a_failing_search_is_stated_rather_than_unknown(hass, client) -> None:
    """websocket_search_media converts SearchError and nothing else, so an Apple
    outage during a search otherwise reaches the user as `unknown_error` with a
    traceback in the log instead of a stated failure."""
    from homeassistant.components.media_player.errors import SearchError

    hass.data[DOMAIN] = RuntimeData(apple_music=client)
    client.responses["catalog/gb/search"] = AppleMusicError("Apple is down")
    async_install(hass)

    query = SearchMediaQuery(search_query="creep", media_content_id=f"{URI_PREFIX}root")
    with pytest.raises(SearchError, match="Apple is down"):
        await SonosMediaPlayerEntity.async_search_media(FakeEntity(hass), query)


async def test_playing_after_the_entry_unloads_says_what_is_wrong(
    hass, client, sonos
) -> None:
    """A patch outlives its entry by however long the removal takes to run."""
    hass.data[DOMAIN] = RuntimeData(apple_music=client)
    assert async_install(hass) is True
    entity = FakeEntity(hass)
    hass.data.pop(DOMAIN)

    with pytest.raises(HomeAssistantError, match="not configured"):
        await SonosMediaPlayerEntity.async_play_media(
            entity, "music", f"{URI_PREFIX}song/{SONG_ID}"
        )
