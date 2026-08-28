"""Watching the Apple TV players a household has.

The players are real entity registry entries rather than a stub, because what is
under test is which entities get subscribed to — a stub would only prove that
the stub was asked.

Scrobbles are sent as background tasks, which `async_block_till_done` explicitly
does not wait for, so the assertions wait for the request itself.
"""

from __future__ import annotations

import asyncio

from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.sonos_apple_music.simkl.const import CONF_SIMKL_TOKEN
from custom_components.sonos_apple_music.simkl.watch import async_start

SEARCH = "https://api.simkl.com/search/tv"
START = "https://api.simkl.com/scrobble/start"
SHOW_ID = 1624792

ATTRIBUTES = {
    "app_id": "com.apple.TVWatchList",
    "media_content_id": "A0054401004",
    "media_title": "Black Bird",
    "media_duration": 3600,
}


class Simkl:
    """A SIMKL that knows the show, accepts the scrobble, and says when it has."""

    def __init__(self, aioclient_mock) -> None:
        self._mock = aioclient_mock
        self.landed = asyncio.Event()
        aioclient_mock.get(SEARCH, json=[{"ids": {"simkl_id": SHOW_ID}}])
        aioclient_mock.post(START, side_effect=self._accept)

    async def _accept(self, method, url, data):
        self.landed.set()
        return AiohttpClientMockResponse(method, url, json={"id": SHOW_ID})

    @property
    def posts(self) -> list:
        return [call for call in self._mock.mock_calls if call[0].lower() == "post"]


@pytest.fixture
def simkl_answers(aioclient_mock):
    return Simkl(aioclient_mock)


def add_player(hass, platform: str = "apple_tv", unique_id: str = "atv-1") -> str:
    """Register a media player the way its integration would."""
    entry = er.async_get(hass).async_get_or_create(
        "media_player", platform, unique_id, suggested_object_id=unique_id
    )
    return entry.entity_id


def watching(hass, config_entry, token: str = "simkl-token"):
    """Start the scrobbler on an entry, returning what stops it."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_SIMKL_TOKEN: token}
    )
    return async_start(hass, config_entry)


async def scrobbled(simkl: Simkl) -> list:
    """Wait for a scrobble to reach SIMKL, or fail the test saying it did not."""
    async with asyncio.timeout(1):
        await simkl.landed.wait()
    return simkl.posts


async def nothing_scrobbled(hass, simkl: Simkl) -> bool:
    """Give a scrobble every chance to happen before concluding none did."""
    for _ in range(50):
        await asyncio.sleep(0)
    await hass.async_block_till_done()
    return not simkl.posts


async def test_playing_an_episode_reaches_simkl(
    hass, config_entry, simkl_answers
) -> None:
    player = add_player(hass)
    unsub = watching(hass, config_entry)

    hass.states.async_set(player, "playing", ATTRIBUTES)
    sent = await scrobbled(simkl_answers)

    assert sent[0][2]["show"]["ids"] == {"simkl": SHOW_ID}
    unsub()


async def test_an_apple_tv_added_afterwards_is_picked_up(
    hass, config_entry, simkl_answers
) -> None:
    """The two integrations start in either order, and neither reloads the other."""
    unsub = watching(hass, config_entry)

    player = add_player(hass)
    await hass.async_block_till_done()
    hass.states.async_set(player, "playing", ATTRIBUTES)

    assert await scrobbled(simkl_answers)
    unsub()


async def test_unloading_stops_the_watching(
    hass, config_entry, simkl_answers
) -> None:
    """An entry that is gone must not keep reporting a household's television."""
    player = add_player(hass)
    unsub = watching(hass, config_entry)
    unsub()

    hass.states.async_set(player, "playing", ATTRIBUTES)

    assert await nothing_scrobbled(hass, simkl_answers)


async def test_a_household_without_simkl_watches_nothing(
    hass, config_entry, simkl_answers
) -> None:
    """Scrobbling is opt-in, so an unlinked entry subscribes to nothing at all."""
    player = add_player(hass)
    unsub = watching(hass, config_entry, token="")

    hass.states.async_set(player, "playing", ATTRIBUTES)

    assert await nothing_scrobbled(hass, simkl_answers)
    unsub()


async def test_other_media_players_are_not_watched(
    hass, config_entry, simkl_answers
) -> None:
    """A Sonos speaker reporting the same attributes is still not an Apple TV."""
    speaker = add_player(hass, platform="sonos", unique_id="living-room")
    unsub = watching(hass, config_entry)

    hass.states.async_set(speaker, "playing", ATTRIBUTES)

    assert await nothing_scrobbled(hass, simkl_answers)
    unsub()


async def test_a_second_apple_tv_does_not_double_the_first(
    hass, config_entry, simkl_answers
) -> None:
    """Re-subscribing without dropping the old one reports every episode twice."""
    first = add_player(hass)
    unsub = watching(hass, config_entry)

    add_player(hass, unique_id="atv-2")
    await hass.async_block_till_done()
    hass.states.async_set(first, "playing", ATTRIBUTES)

    await scrobbled(simkl_answers)
    for _ in range(50):
        await asyncio.sleep(0)

    assert len(simkl_answers.posts) == 1
    unsub()
