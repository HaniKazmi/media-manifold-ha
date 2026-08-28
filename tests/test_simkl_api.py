"""Talking to SIMKL.

The requests here are checked against the shapes SIMKL was measured to accept,
and the one it accepts while recording nothing. A status code alone would pass
whether or not anything reached the account, which is exactly the failure these
tests exist to catch.

Nothing here asserts on Home Assistant state: this client reports what SIMKL
said and the scrobbler decides what to do about it, so the repair issue is
tested in test_simkl_watch.py.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.sonos_apple_music.simkl.api import Result, SimklClient
from custom_components.sonos_apple_music.simkl.playing import Episode, Event

from .conftest import SHOW_ID, SIMKL_SEARCH, scrobble_url, sent, simkl_finds

EPISODE = Episode("Black Bird", 1, 4)


def scrobble(act: str = "stop", progress: float | None = 100.0) -> Event:
    return Event(act, EPISODE, progress)


async def test_the_write_key_is_simkl_not_the_one_the_search_answers_with(
    hass, aioclient_mock
) -> None:
    """`simkl_id` on a write is answered 201 with an empty body and stored nowhere.

    The two endpoints spell the same id differently, so the conversion is the
    single most breakable line in this module.
    """
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), json={"id": SHOW_ID})

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.RECORDED
    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert body["show"]["ids"] == {"simkl": SHOW_ID}
    assert body["episode"] == {"season": 1, "number": 4}
    assert body["progress"] == 100.0
    assert headers["Authorization"] == "Bearer token"


async def test_a_start_without_a_measurable_progress_omits_it(
    hass, aioclient_mock
) -> None:
    """Sending zero would claim the viewer is at the beginning of the episode."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("start"), json={"id": SHOW_ID})

    await SimklClient(hass, "token").async_scrobble(scrobble("start", None))

    assert "progress" not in aioclient_mock.mock_calls[-1][2]


async def test_a_recorded_nothing_is_reported_rather_than_passed_as_success(
    hass, aioclient_mock, caplog
) -> None:
    """SIMKL answers an ids object it does not recognise with 201 and no show."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), json={"id": 0}, status=201)

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
    assert "recorded nothing" in caplog.text


async def test_a_show_simkl_cannot_name_is_not_scrobbled(hass, aioclient_mock) -> None:
    """With no id and no year there is nothing to send that would resolve."""
    aioclient_mock.get(SIMKL_SEARCH, json=[])

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
    assert not sent(aioclient_mock, "post")


async def test_a_first_hit_carrying_no_id_is_not_scrobbled(
    hass, aioclient_mock
) -> None:
    simkl_finds(aioclient_mock, show_id=None)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert not sent(aioclient_mock, "post")


async def test_a_show_is_searched_once_however_many_episodes_follow(
    hass, aioclient_mock
) -> None:
    """A household rewatches a handful of shows; three searches an episode is waste."""
    simkl_finds(aioclient_mock)
    for act in ("start", "pause", "stop"):
        aioclient_mock.post(scrobble_url(act), json={"id": SHOW_ID})

    client = SimklClient(hass, "token")
    for act in ("start", "pause", "stop"):
        await client.async_scrobble(scrobble(act))

    assert len(sent(aioclient_mock, "get")) == 1


async def test_a_show_simkl_cannot_name_is_not_re_searched_every_transition(
    hass, aioclient_mock
) -> None:
    """Otherwise an unmatched show costs three round trips an episode, forever."""
    aioclient_mock.get(SIMKL_SEARCH, json=[])

    client = SimklClient(hass, "token")
    for act in ("start", "pause", "stop"):
        await client.async_scrobble(scrobble(act))

    assert len(sent(aioclient_mock, "get")) == 1


async def test_a_miss_is_forgotten_in_time_to_matter(
    hass, aioclient_mock, freezer
) -> None:
    """SIMKL matches new shows as its catalogue fills, so it is asked again."""
    aioclient_mock.get(SIMKL_SEARCH, json=[])
    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())

    freezer.tick(timedelta(hours=2))
    await client.async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "get")) == 2


async def test_a_search_that_cannot_be_reached_is_not_a_remembered_miss(
    hass, aioclient_mock
) -> None:
    """An unreachable SIMKL is not an answer about the show, so it is asked again."""
    aioclient_mock.get(SIMKL_SEARCH, exc=TimeoutError())

    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())
    await client.async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "get")) == 2
    assert not sent(aioclient_mock, "post")


async def test_an_episode_simkl_already_has_is_not_an_error(
    hass, aioclient_mock, caplog
) -> None:
    """409 means the scrobble arrived, an hour ago; there is nothing to fix."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), status=409)

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
    assert "WARNING" not in caplog.text


async def test_being_rate_limited_drops_the_scrobble(hass, aioclient_mock) -> None:
    """A retry would describe a moment that has already passed."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), status=429)

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
    assert len(sent(aioclient_mock, "post")) == 1


async def test_a_show_simkl_cannot_match_is_reported_and_survived(
    hass, aioclient_mock, caplog
) -> None:
    """404 id_err is what a scrobble carrying no usable id is answered with."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(
        scrobble_url("stop"), status=404, json={"error": "id_err", "code": 404}
    )

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
    assert "WARNING" not in caplog.text


async def test_a_rejected_token_is_told_apart_from_other_refusals(
    hass, aioclient_mock
) -> None:
    """It is the one failure a household can fix, so the caller must know it."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), status=401)

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.TOKEN_REJECTED


async def test_simkl_being_unreachable_is_survivable(hass, aioclient_mock) -> None:
    """A scrobble is a report about television; it may not break anything."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), exc=TimeoutError())

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED


async def test_scrobbles_are_sent_one_at_a_time(hass, aioclient_mock) -> None:
    """SIMKL holds a 20-second lock per user and answers an overlap with 429."""
    gate = asyncio.Event()
    arrived = asyncio.Event()
    started: list[str] = []

    async def hold(method, url_, data):
        started.append(data["show"]["ids"]["simkl"])
        arrived.set()
        await gate.wait()
        return AiohttpClientMockResponse(method, url_, json={"id": SHOW_ID})

    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), side_effect=hold)

    client = SimklClient(hass, "token")
    both = asyncio.gather(
        client.async_scrobble(scrobble()), client.async_scrobble(scrobble())
    )

    await arrived.wait()
    # The second one gets every chance to overtake, and the lock is what stops it.
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(started) == 1

    gate.set()
    await both
    assert len(started) == 2


async def test_an_episode_boundary_asks_about_the_show_once(
    hass, aioclient_mock
) -> None:
    """A stop and a start of one show are raised together, and share one answer."""
    simkl_finds(aioclient_mock)
    for act in ("start", "stop"):
        aioclient_mock.post(scrobble_url(act), json={"id": SHOW_ID})

    client = SimklClient(hass, "token")
    await asyncio.gather(
        client.async_scrobble(scrobble("stop")),
        client.async_scrobble(scrobble("start")),
    )

    assert len(sent(aioclient_mock, "get")) == 1


async def test_an_id_simkl_will_not_resolve_is_dropped(hass, aioclient_mock) -> None:
    """Keeping it would make every later episode of that show fail the same way."""
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("stop"), status=404, json={"error": "id_err"})

    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())

    assert client.shows == {}


async def test_an_answer_that_is_not_a_list_is_not_remembered(
    hass, aioclient_mock
) -> None:
    """It says nothing about the show, so it must not suppress it for an hour."""
    aioclient_mock.get(SIMKL_SEARCH, json={"error": "bad"})

    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())
    await client.async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "get")) == 2


async def test_a_hit_carrying_no_id_is_not_remembered(hass, aioclient_mock) -> None:
    """A wrongly shaped hit is the leading indicator for the ids-key confusion."""
    simkl_finds(aioclient_mock, show_id=None)

    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())
    await client.async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "get")) == 2


async def test_a_search_hit_that_is_not_an_object_is_survived(
    hass, aioclient_mock
) -> None:
    """A proxy's error page parses as JSON, and this client raises nothing."""
    aioclient_mock.get(SIMKL_SEARCH, json=["Black Bird"])

    result = await SimklClient(hass, "token").async_scrobble(scrobble())

    assert result is Result.DECLINED
