"""Grafting Jellyfin playback onto the Apple TV integration.

This wraps a method Home Assistant owns, so the tests import the real apple_tv
module rather than a stub — an upstream rename should fail here, not in someone's
living room.

The recorder replaces the real method *before* installing, so the wrapper captures
it as its original. Reaching into the patch module's saved originals afterwards
would test the test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.apple_tv import media_player as apple_media_player
from homeassistant.components.apple_tv.media_player import AppleTvMediaPlayer
from homeassistant.components.media_player import MediaType
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.manifold.infuse.link import TICKS_PER_SECOND
from custom_components.manifold.infuse.patch import (
    URI_PREFIX,
    async_install,
    async_remove,
)
from custom_components.manifold.infuse.resolve import JELLYFIN_DOMAIN

MOVIE_ID = "0f3a1c"
EPISODE_ID = "9b2d4e"
SERIES_ID = "77cc11"
SERIES_TMDB = "1396"


class FakeJellyfin:
    """Stands in for the Jellyfin client, recording what was asked of it."""

    def __init__(self, items: dict[str, dict[str, Any]], stream: bool = True) -> None:
        self.items = items
        self.stream = stream
        self.asked: list[tuple[str, str | None]] = []

    def get_item(self, item_id: str, fields: str | None = None) -> dict[str, Any] | None:
        self.asked.append((item_id, fields))
        return self.items.get(item_id)

    @property
    def ids(self) -> list[str]:
        return [item_id for item_id, _ in self.asked]

    def video_url(self, item_id: str) -> str:
        if not self.stream:
            return ""
        return f"http://jelly:8096/Videos/{item_id}/stream?static=true&api_key=k"


def library() -> dict[str, dict[str, Any]]:
    items = {
        MOVIE_ID: {
            "Type": "Movie",
            "MediaType": "Video",
            "Name": "Inception",
            "ProviderIds": {"Tmdb": "27205"},
        },
        EPISODE_ID: {
            "Type": "Episode",
            "MediaType": "Video",
            "Name": "Ozymandias",
            "SeriesId": SERIES_ID,
            "ParentIndexNumber": 5,
            "IndexNumber": 14,
        },
        SERIES_ID: {
            "Type": "Series",
            "MediaType": "Video",
            "Name": "Breaking Bad",
            "ProviderIds": {"Tmdb": SERIES_TMDB},
        },
    }
    return items


@pytest.fixture
def jellyfin(hass, monkeypatch):
    """A loaded Jellyfin entry, carrying its client where the real one carries it.

    The entry stands outside `hass.config_entries` and the lookup is replaced
    instead. Marking a real integration's entry LOADED without setting it up
    leaves the harness holding a registry timer nothing cancels, and every test
    after it fails on a closed event loop. The lookup is the whole of what this
    graft reads, so the lookup is what stands in.
    """

    def _load(items: dict[str, dict[str, Any]] | None = None, stream: bool = True):
        api = FakeJellyfin(library() if items is None else items, stream=stream)
        entry = MockConfigEntry(domain=JELLYFIN_DOMAIN, title="Jellyfin")
        entry.runtime_data = SimpleNamespace(api_client=SimpleNamespace(jellyfin=api))
        monkeypatch.setattr(
            hass.config_entries,
            "async_loaded_entries",
            lambda domain: [entry] if domain == JELLYFIN_DOMAIN else [],
        )
        return api

    return _load


@pytest.fixture
def played(hass, monkeypatch):
    """Install the graft over a recorder, and take it back off afterwards.

    monkeypatch is a dependency rather than a convenience: its undo runs after
    this fixture's own teardown, so `async_remove` still restores the recorder
    before the real method is put back, and an exception from `async_install`
    cannot leave the recorder on the real class for the rest of the session.
    """
    calls: list[tuple[Any, str, dict[str, Any]]] = []

    async def recorder(self, media_type, media_id, **kwargs):
        calls.append((media_type, media_id, kwargs))

    monkeypatch.setattr(AppleTvMediaPlayer, "async_play_media", recorder)
    assert async_install(hass) is True
    yield calls
    async_remove()


async def play(hass, media_id: str, media_type: Any = MediaType.VIDEO, **kwargs) -> None:
    entity = SimpleNamespace(hass=hass)
    await AppleTvMediaPlayer.async_play_media(entity, media_type, media_id, **kwargs)


async def test_home_assistant_still_routes_a_url_to_launch_app() -> None:
    """The graft rests on MediaType.URL reaching `apps.launch_app`, which is the
    call that opens a deep link on tvOS. Routed to AirPlay instead — the branch
    directly below it upstream — every Infuse URL would fail as an unplayable
    stream, and nothing else here would notice.

    The real method, unpatched: it returns at that branch, so the Apple TV it is
    handed need be no more than the one attribute that branch reads.
    """
    launch_app = AsyncMock()
    apps = SimpleNamespace(launch_app=launch_app)
    entity = SimpleNamespace(atv=SimpleNamespace(apps=apps))

    await AppleTvMediaPlayer.async_play_media(
        entity, MediaType.URL, "infuse://movie/27205?play"
    )

    launch_app.assert_awaited_once_with("infuse://movie/27205?play")


async def test_a_movie_reaches_infuse_as_a_url_launch(hass, jellyfin, played) -> None:
    """MediaType.URL is what upstream answers with `apps.launch_app`, the call
    that opens a deep link on tvOS."""
    jellyfin()
    await play(hass, f"{URI_PREFIX}{MOVIE_ID}")

    assert played == [(MediaType.URL, "infuse://movie/27205?play", {})]


async def test_an_episode_is_addressed_through_its_series(hass, jellyfin, played) -> None:
    jellyfin()
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")

    assert played == [(MediaType.URL, f"infuse://series/{SERIES_TMDB}-5-14?play", {})]


async def test_the_series_lookup_asks_only_for_what_it_reads(
    hass, jellyfin, played
) -> None:
    """The client's default field set makes the server aggregate item counts and
    running times across every episode of a show to answer one id."""
    api = jellyfin()
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")

    assert (SERIES_ID, "ProviderIds") in api.asked


async def test_a_series_is_looked_up_once_across_episodes(hass, jellyfin, played) -> None:
    """Binging a season otherwise costs a round trip per episode for an answer
    that cannot change."""
    api = jellyfin()
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")

    assert api.ids == [EPISODE_ID, SERIES_ID, EPISODE_ID]


async def test_a_series_without_a_tmdb_id_yet_is_asked_again(
    hass, jellyfin, played
) -> None:
    """Jellyfin matches a series against TMDB after it is added, not before.

    Remembering the miss would hold every episode of that series on the stream
    fallback — giving up the watched-state writeback the deep link exists for —
    until something reloaded the entry.
    """
    items = library()
    items[SERIES_ID] = {"Type": "Series", "MediaType": "Video", "Name": "Breaking Bad"}
    api = jellyfin(items)

    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")
    items[SERIES_ID]["ProviderIds"] = {"Tmdb": SERIES_TMDB}
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")

    assert api.ids.count(SERIES_ID) == 2
    assert played[-1] == (MediaType.URL, f"infuse://series/{SERIES_TMDB}-5-14?play", {})


async def test_the_cache_is_dropped_with_the_patch(hass, jellyfin, played) -> None:
    """A stale series id outliving a reload would answer for a library that has
    since been re-matched."""
    api = jellyfin()
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")
    async_remove()
    assert async_install(hass) is True
    await play(hass, f"{URI_PREFIX}{EPISODE_ID}")

    assert api.ids.count(SERIES_ID) == 2


async def test_an_unmatched_item_falls_back_to_its_stream(hass, jellyfin, played) -> None:
    """A home video has no TMDB id and so no place in Infuse's library, but it
    still has a URL that plays."""
    items = library()
    items[MOVIE_ID] = {
        "Type": "Movie",
        "MediaType": "Video",
        "Name": "Wedding 2014",
        "UserData": {"PlaybackPositionTicks": 42 * TICKS_PER_SECOND},
    }
    jellyfin(items)
    await play(hass, f"{URI_PREFIX}{MOVIE_ID}")

    (media_type, url, _), = played
    assert media_type is MediaType.URL
    assert url.startswith("infuse://x-callback-url/play?url=")
    assert "Wedding%202014" in url
    assert "position=42" in url


async def test_music_keeps_the_apple_tv_own_streaming(hass, jellyfin, played) -> None:
    """RAOP already plays Jellyfin audio, and Infuse is a video player: handing
    it a track would open the app on nothing."""
    items = library()
    items["track"] = {"Type": "Audio", "MediaType": "Audio", "Name": "Creep"}
    jellyfin(items)
    await play(hass, f"{URI_PREFIX}track")

    assert played == [(MediaType.VIDEO, f"{URI_PREFIX}track", {})]


async def test_other_media_reaches_the_apple_tv_untouched(hass, jellyfin, played) -> None:
    jellyfin()
    await play(hass, "media-source://media_source/local/clip.mp4")
    await play(hass, "com.apple.TVWatchList", media_type=MediaType.APP)

    assert played == [
        (MediaType.VIDEO, "media-source://media_source/local/clip.mp4", {}),
        (MediaType.APP, "com.apple.TVWatchList", {}),
    ]


async def test_enqueue_and_other_arguments_pass_through(hass, jellyfin, played) -> None:
    """Forwarding kwargs verbatim means an argument added upstream keeps working
    without a change here."""
    jellyfin()
    await play(hass, f"{URI_PREFIX}{MOVIE_ID}", announce=True)

    assert played == [(MediaType.URL, "infuse://movie/27205?play", {"announce": True})]


async def test_an_unknown_item_is_left_to_the_apple_tv_to_refuse(
    hass, jellyfin, played
) -> None:
    """Upstream already answers a media source id that resolves to nothing, and
    its error names the source rather than this graft."""
    jellyfin()
    await play(hass, f"{URI_PREFIX}missing")

    assert played == [(MediaType.VIDEO, f"{URI_PREFIX}missing", {})]


async def test_no_jellyfin_says_so_rather_than_failing_quietly(
    hass, jellyfin, played
) -> None:
    with pytest.raises(HomeAssistantError, match="Jellyfin integration is not loaded"):
        await play(hass, f"{URI_PREFIX}{MOVIE_ID}")


async def test_an_item_with_neither_route_says_so(hass, jellyfin, played) -> None:
    """A play button that does nothing and logs is indistinguishable from a
    broken television."""
    items = library()
    items[MOVIE_ID] = {"Type": "Movie", "MediaType": "Video", "Name": "Wedding 2014"}
    jellyfin(items, stream=False)

    with pytest.raises(HomeAssistantError, match="Wedding 2014"):
        await play(hass, f"{URI_PREFIX}{MOVIE_ID}")


async def test_installing_twice_does_not_double_wrap(hass, jellyfin, played) -> None:
    """A second install capturing the first wrapper as its original would resolve
    every item twice."""
    jellyfin()
    assert async_install(hass) is True
    await play(hass, f"{URI_PREFIX}{MOVIE_ID}")

    assert played == [(MediaType.URL, "infuse://movie/27205?play", {})]


async def test_removing_restores_the_apple_tv_completely(hass, jellyfin) -> None:
    """A removed add-on must leave no trace in an integration it does not own."""
    original = AppleTvMediaPlayer.async_play_media
    assert async_install(hass) is True
    async_remove()

    assert AppleTvMediaPlayer.async_play_media is original


async def test_removing_what_was_never_installed_is_harmless(hass) -> None:
    original = AppleTvMediaPlayer.async_play_media
    async_remove()

    assert AppleTvMediaPlayer.async_play_media is original


async def test_a_moved_seam_leaves_the_apple_tv_alone(hass, monkeypatch) -> None:
    """Breaking the television would be worse than not adding Infuse."""
    original = AppleTvMediaPlayer.async_play_media
    monkeypatch.setattr(
        apple_media_player, "AppleTvMediaPlayer", SimpleNamespace(), raising=True
    )

    assert async_install(hass) is False
    assert AppleTvMediaPlayer.async_play_media is original
