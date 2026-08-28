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
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.sonos_apple_music.simkl.const import (
    CONF_SIMKL_TOKEN,
    EVENT_WATCHED,
    WATCHED_AT,
)
from custom_components.sonos_apple_music.simkl.watch import async_start

SEARCH = "https://api.simkl.com/search/tv"
SHOW_ID = 1624792
ACTS = ("start", "pause", "stop")

ATTRIBUTES = {
    "app_id": "com.apple.TVWatchList",
    "media_content_id": "A0054401004",
    "media_title": "Black Bird",
    "media_duration": 3600,
}


def at(position: int) -> dict:
    """A playing state that has reached `position` seconds of the episode."""
    return {
        **ATTRIBUTES,
        "media_position": position,
        "media_position_updated_at": dt_util.utcnow(),
    }


class Simkl:
    """A SIMKL that knows the show, and says when each act has reached it."""

    def __init__(self, aioclient_mock, stop_status: int = 200) -> None:
        self._mock = aioclient_mock
        self._stop_status = stop_status
        self.done = {act: asyncio.Event() for act in ACTS}
        aioclient_mock.get(SEARCH, json=[{"ids": {"simkl_id": SHOW_ID}}])
        for act in ACTS:
            aioclient_mock.post(
                f"https://api.simkl.com/scrobble/{act}", side_effect=self._accept
            )

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
        return [call for call in self._mock.mock_calls if call[0].lower() == "post"]


@pytest.fixture
def simkl_answers(aioclient_mock):
    return Simkl(aioclient_mock)


@pytest.fixture
def simkl_already_has_it(aioclient_mock):
    """SIMKL answering a stop with the 409 it uses for an episode it holds."""
    return Simkl(aioclient_mock, stop_status=409)


def add_player(hass, platform: str = "apple_tv", unique_id: str = "atv-1") -> str:
    """Register a media player the way its integration would."""
    entry = er.async_get(hass).async_get_or_create(
        "media_player", platform, unique_id, suggested_object_id=unique_id
    )
    return entry.entity_id


def watching(hass, config_entry, token: str = "simkl-token"):
    """Start the scrobbler on an entry, returning it, or None when unlinked."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_SIMKL_TOKEN: token}
    )
    return async_start(hass, config_entry)


async def settle() -> None:
    """Let the background task that sends a scrobble run to completion."""
    for _ in range(50):
        await asyncio.sleep(0)


async def scrobbled(simkl: Simkl, act: str = "start") -> list:
    """Wait for one act to reach SIMKL, or fail the test saying it did not."""
    async with asyncio.timeout(1):
        await simkl.done[act].wait()
    await settle()
    return simkl.posts


async def nothing_scrobbled(hass, simkl: Simkl) -> bool:
    """Give a scrobble every chance to happen before concluding none did."""
    await settle()
    await hass.async_block_till_done()
    return not simkl.posts


async def finishes(hass, player: str, simkl: Simkl, position: int) -> None:
    """Play an episode up to `position` seconds and leave it."""
    hass.states.async_set(player, "playing", at(position))
    hass.states.async_set(player, "idle", {})
    await scrobbled(simkl, "stop")


async def test_playing_an_episode_reaches_simkl(
    hass, config_entry, simkl_answers
) -> None:
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)

    hass.states.async_set(player, "playing", ATTRIBUTES)
    sent = await scrobbled(simkl_answers)

    assert sent[0][2]["show"]["ids"] == {"simkl": SHOW_ID}
    scrobbler.async_stop()


async def test_an_apple_tv_added_afterwards_is_picked_up(
    hass, config_entry, simkl_answers
) -> None:
    """The two integrations start in either order, and neither reloads the other."""
    scrobbler = watching(hass, config_entry)

    player = add_player(hass)
    await hass.async_block_till_done()
    hass.states.async_set(player, "playing", ATTRIBUTES)

    assert await scrobbled(simkl_answers)
    scrobbler.async_stop()


async def test_unloading_stops_the_watching(
    hass, config_entry, simkl_answers
) -> None:
    """An entry that is gone must not keep reporting a household's television."""
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)
    scrobbler.async_stop()

    hass.states.async_set(player, "playing", ATTRIBUTES)

    assert await nothing_scrobbled(hass, simkl_answers)


async def test_a_household_without_simkl_watches_nothing(
    hass, config_entry, simkl_answers
) -> None:
    """Scrobbling is opt-in, so an unlinked entry subscribes to nothing at all."""
    player = add_player(hass)

    assert watching(hass, config_entry, token="") is None

    hass.states.async_set(player, "playing", ATTRIBUTES)
    assert await nothing_scrobbled(hass, simkl_answers)


async def test_other_media_players_are_not_watched(
    hass, config_entry, simkl_answers
) -> None:
    """A Sonos speaker reporting the same attributes is still not an Apple TV."""
    speaker = add_player(hass, platform="sonos", unique_id="living-room")
    scrobbler = watching(hass, config_entry)

    hass.states.async_set(speaker, "playing", ATTRIBUTES)

    assert await nothing_scrobbled(hass, simkl_answers)
    scrobbler.async_stop()


async def test_a_second_apple_tv_does_not_double_the_first(
    hass, config_entry, simkl_answers
) -> None:
    """Re-subscribing without dropping the old one reports every episode twice."""
    first = add_player(hass)
    scrobbler = watching(hass, config_entry)

    add_player(hass, unique_id="atv-2")
    await hass.async_block_till_done()
    hass.states.async_set(first, "playing", ATTRIBUTES)

    await scrobbled(simkl_answers)
    for _ in range(50):
        await asyncio.sleep(0)

    assert len(simkl_answers.posts) == 1
    scrobbler.async_stop()


async def test_finishing_an_episode_announces_it(
    hass, config_entry, simkl_answers
) -> None:
    """The event is what the logbook, a dashboard or an automation reads."""
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl_answers, position=3550)

    assert len(watched) == 1
    data = watched[0].data
    assert data["entity_id"] == player
    assert (data["show"], data["season"], data["episode"]) == ("Black Bird", 1, 4)
    assert data["simkl_id"] == SHOW_ID
    assert data["progress"] >= WATCHED_AT
    scrobbler.async_stop()


async def test_starting_an_episode_announces_nothing(
    hass, config_entry, simkl_answers
) -> None:
    """Only reaching the history is worth announcing; pressing play is not."""
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    hass.states.async_set(player, "playing", at(10))
    await scrobbled(simkl_answers, "start")

    assert not watched
    scrobbler.async_stop()


async def test_an_episode_abandoned_early_is_not_announced(
    hass, config_entry, simkl_answers
) -> None:
    """SIMKL saves it as a resume point below 80%, and has not watched it."""
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl_answers, position=600)

    assert not watched
    scrobbler.async_stop()


async def test_an_episode_simkl_already_had_is_not_announced(
    hass, config_entry, simkl_already_has_it
) -> None:
    """A 409 means it reached the history through some other call, not this one."""
    player = add_player(hass)
    scrobbler = watching(hass, config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl_already_has_it, position=3550)

    assert not watched
    scrobbler.async_stop()
