"""Watching the Apple TV players a household has.

The players are real entity registry entries rather than a stub, because what is
under test is which entities get subscribed to — a stub would only prove that
the stub was asked.

Scrobbles are sent as background tasks, so the assertions wait on
`async_block_till_done(wait_background_tasks=True)`. The plain call returns
before those tasks finish, which would let a scrobbler that never sends anything
pass every test asserting that nothing was sent.
"""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.sonos_apple_music.const import DOMAIN
from custom_components.sonos_apple_music.simkl.const import (
    CONF_SIMKL_TOKEN,
    EVENT_WATCHED,
    TOKEN_ISSUE,
    WATCHED_AT,
)
from custom_components.sonos_apple_music.simkl.watch import async_start

from .conftest import APPLE_TV_ATTRIBUTES, SHOW_ID, add_player, scrobble_url, simkl_finds


def at(position: int) -> dict:
    """A playing state that has reached `position` seconds of the episode."""
    return {
        **APPLE_TV_ATTRIBUTES,
        "media_position": position,
        "media_position_updated_at": dt_util.utcnow(),
    }


@pytest.fixture
def watching(hass):
    """Start the scrobbler on an entry, and always stop it again.

    A test that fails before its own teardown would otherwise leave a live bus
    listener and state subscription behind for the rest of the session, which is
    what conftest's `no_leaked_grafts` exists to prevent for the grafts.
    """
    started = []

    def start(config_entry, token: str = "simkl-token"):
        config_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, CONF_SIMKL_TOKEN: token}
        )
        if (scrobbler := async_start(hass, config_entry)) is not None:
            started.append(scrobbler)
        return scrobbler

    yield start

    for scrobbler in started:
        scrobbler.async_stop()


async def finishes(hass, player: str, simkl, position: int) -> None:
    """Play an episode up to `position` seconds and leave it."""
    hass.states.async_set(player, "playing", at(position))
    hass.states.async_set(player, "idle", {})
    await simkl.wait_for(hass, "stop")


async def test_playing_an_episode_reaches_simkl(
    hass, config_entry, watching, simkl
) -> None:
    player = add_player(hass)
    watching(config_entry)

    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    sent = await simkl.wait_for(hass)

    assert sent[0][2]["show"]["ids"] == {"simkl": SHOW_ID}


async def test_an_apple_tv_added_afterwards_is_picked_up(
    hass, config_entry, watching, simkl
) -> None:
    """The two integrations start in either order, and neither reloads the other."""
    watching(config_entry)

    player = add_player(hass)
    await hass.async_block_till_done()
    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)

    assert await simkl.wait_for(hass)


async def test_an_unrelated_entity_appearing_changes_nothing(
    hass, config_entry, watching, simkl
) -> None:
    """Every entity in Home Assistant passes through, and a household has thousands."""
    player = add_player(hass)
    watching(config_entry)

    add_player(hass, platform="sonos", unique_id="kitchen")
    await hass.async_block_till_done()
    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)

    assert len(await simkl.wait_for(hass)) == 1


async def test_unloading_stops_the_watching(
    hass, config_entry, watching, simkl
) -> None:
    """An entry that is gone must not keep reporting a household's television."""
    player = add_player(hass)
    scrobbler = watching(config_entry)
    scrobbler.async_stop()

    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)

    assert await simkl.nothing_sent(hass)


async def test_a_household_without_simkl_watches_nothing(
    hass, config_entry, watching, simkl
) -> None:
    """Scrobbling is opt-in, so an unlinked entry subscribes to nothing at all."""
    player = add_player(hass)

    assert watching(config_entry, token="") is None

    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    assert await simkl.nothing_sent(hass)


async def test_other_media_players_are_not_watched(
    hass, config_entry, watching, simkl
) -> None:
    """A Sonos speaker reporting the same attributes is still not an Apple TV."""
    speaker = add_player(hass, platform="sonos", unique_id="living-room")
    watching(config_entry)

    hass.states.async_set(speaker, "playing", APPLE_TV_ATTRIBUTES)

    assert await simkl.nothing_sent(hass)


async def test_a_second_apple_tv_does_not_double_the_first(
    hass, config_entry, watching, simkl
) -> None:
    """Re-subscribing without dropping the old one reports every episode twice."""
    first = add_player(hass)
    watching(config_entry)

    add_player(hass, unique_id="atv-2")
    await hass.async_block_till_done()
    hass.states.async_set(first, "playing", APPLE_TV_ATTRIBUTES)

    assert len(await simkl.wait_for(hass)) == 1


async def test_finishing_an_episode_announces_it(
    hass, config_entry, watching, simkl
) -> None:
    """The event is what the logbook, a dashboard or an automation reads."""
    player = add_player(hass)
    watching(config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl, position=3550)

    assert len(watched) == 1
    data = watched[0].data
    assert data["entity_id"] == player
    assert (data["show"], data["season"], data["episode"]) == ("Black Bird", 1, 4)
    assert data["simkl_id"] == SHOW_ID
    assert data["progress"] >= WATCHED_AT


async def test_starting_an_episode_announces_nothing(
    hass, config_entry, watching, simkl
) -> None:
    """Only reaching the history is worth announcing; pressing play is not."""
    player = add_player(hass)
    watching(config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    hass.states.async_set(player, "playing", at(10))
    await simkl.wait_for(hass)

    assert not watched


async def test_an_episode_abandoned_early_is_not_announced(
    hass, config_entry, watching, simkl
) -> None:
    """SIMKL saves it as a resume point below 80%, and has not watched it."""
    player = add_player(hass)
    watching(config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl, position=600)

    assert not watched


async def test_an_episode_simkl_already_had_is_not_announced(
    hass, config_entry, watching, simkl_already_has_it
) -> None:
    """A 409 means it reached the history through some other call, not this one."""
    player = add_player(hass)
    watching(config_entry)
    watched = async_capture_events(hass, EVENT_WATCHED)

    await finishes(hass, player, simkl_already_has_it, position=3550)

    assert not watched


async def test_a_rejected_token_raises_a_repair(
    hass, config_entry, watching, aioclient_mock
) -> None:
    """Nothing else in the entry is broken, so nothing else is interrupted."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("start"), status=401)
    player = add_player(hass)
    watching(config_entry)

    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert ir.async_get(hass).async_get_issue(DOMAIN, TOKEN_ISSUE) is not None


async def test_a_scrobble_that_lands_clears_the_repair(
    hass, config_entry, watching, simkl
) -> None:
    """A token replaced by hand must not leave the warning standing."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        TOKEN_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TOKEN_ISSUE,
    )
    player = add_player(hass)
    watching(config_entry)

    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    await simkl.wait_for(hass)

    assert ir.async_get(hass).async_get_issue(DOMAIN, TOKEN_ISSUE) is None


async def test_removing_the_apple_tv_stops_watching_it(
    hass, config_entry, watching, simkl
) -> None:
    """A player that is gone must not stay subscribed."""
    player = add_player(hass)
    scrobbler = watching(config_entry)
    assert scrobbler.watching == [player]

    er.async_get(hass).async_remove(player)
    await hass.async_block_till_done()

    assert scrobbler.watching == []


async def test_renaming_an_entity_is_not_a_change_of_players(
    hass, config_entry, watching, simkl
) -> None:
    """Updates are the bulk of what arrives here, and none of them change the set."""
    player = add_player(hass)
    scrobbler = watching(config_entry)

    er.async_get(hass).async_update_entity(player, name="Telly")
    await hass.async_block_till_done()

    assert scrobbler.watching == [player]
    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    assert await simkl.wait_for(hass)
