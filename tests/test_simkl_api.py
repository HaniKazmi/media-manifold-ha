"""Talking to SIMKL.

The requests here are checked against the shapes SIMKL was measured to accept,
and the one it accepts while recording nothing. A status code alone would pass
whether or not anything reached the account, which is exactly the failure these
tests exist to catch.
"""

from __future__ import annotations

import asyncio

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.sonos_apple_music.const import DOMAIN
from custom_components.sonos_apple_music.simkl.api import TOKEN_ISSUE, SimklClient
from custom_components.sonos_apple_music.simkl.playing import Episode, Event

SEARCH = "https://api.simkl.com/search/tv"
SHOW_ID = 1624792
EPISODE = Episode("Black Bird", 1, 4)


def url(act: str) -> str:
    return f"https://api.simkl.com/scrobble/{act}"


def scrobble(act: str = "stop", progress: float | None = 100.0) -> Event:
    return Event(act, EPISODE, progress)


def found(aioclient_mock, show_id: int | None = SHOW_ID) -> None:
    """Answer the search the way SIMKL does — with `simkl_id`, not `simkl`."""
    ids = {"simkl_id": show_id} if show_id is not None else {}
    aioclient_mock.get(SEARCH, json=[{"title": "Black Bird", "ids": ids}])


def sent(aioclient_mock, method: str) -> list:
    """The requests of one method that reached SIMKL, in order."""
    return [call for call in aioclient_mock.mock_calls if call[0].lower() == method]


async def test_the_write_key_is_simkl_not_the_one_the_search_answers_with(
    hass, aioclient_mock
) -> None:
    """`simkl_id` on a write is answered 201 with an empty body and stored nowhere.

    The two endpoints spell the same id differently, so the conversion is the
    single most breakable line in this module.
    """
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), json={"id": SHOW_ID})

    await SimklClient(hass, "token").async_scrobble(scrobble())

    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert body["show"]["ids"] == {"simkl": SHOW_ID}
    assert body["episode"] == {"season": 1, "number": 4}
    assert body["progress"] == 100.0
    assert headers["Authorization"] == "Bearer token"


async def test_a_start_without_a_measurable_progress_omits_it(
    hass, aioclient_mock
) -> None:
    """Sending zero would claim the viewer is at the beginning of the episode."""
    found(aioclient_mock)
    aioclient_mock.post(url("start"), json={"id": SHOW_ID})

    await SimklClient(hass, "token").async_scrobble(scrobble("start", None))

    assert "progress" not in aioclient_mock.mock_calls[-1][2]


async def test_a_recorded_nothing_is_reported_rather_than_passed_as_success(
    hass, aioclient_mock, caplog
) -> None:
    """SIMKL answers an ids object it does not recognise with 201 and no show."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), json={"id": 0}, status=201)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert "recorded nothing" in caplog.text


async def test_a_show_simkl_cannot_name_is_not_scrobbled(hass, aioclient_mock) -> None:
    """With no id and no year there is nothing to send that would resolve."""
    aioclient_mock.get(SEARCH, json=[])

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert not sent(aioclient_mock, "post")


async def test_a_first_hit_carrying_no_id_is_not_scrobbled(
    hass, aioclient_mock
) -> None:
    found(aioclient_mock, show_id=None)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert not sent(aioclient_mock, "post")


async def test_a_show_is_searched_once_however_many_episodes_follow(
    hass, aioclient_mock
) -> None:
    """A household rewatches a handful of shows; three searches an episode is waste."""
    found(aioclient_mock)
    for act in ("start", "pause", "stop"):
        aioclient_mock.post(url(act), json={"id": SHOW_ID})

    client = SimklClient(hass, "token")
    for act in ("start", "pause", "stop"):
        await client.async_scrobble(scrobble(act))

    searches = sent(aioclient_mock, "get")
    assert len(searches) == 1


async def test_a_search_miss_is_not_remembered(hass, aioclient_mock) -> None:
    """SIMKL matches new shows as its catalogue fills, and this one may be next."""
    aioclient_mock.get(SEARCH, json=[])

    client = SimklClient(hass, "token")
    await client.async_scrobble(scrobble())
    await client.async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "get")) == 2


async def test_an_episode_simkl_already_has_is_not_an_error(
    hass, aioclient_mock, caplog
) -> None:
    """409 means the scrobble arrived, an hour ago; there is nothing to fix."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), status=409)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert "recorded nothing" not in caplog.text
    assert "WARNING" not in caplog.text


async def test_being_rate_limited_drops_the_scrobble(hass, aioclient_mock) -> None:
    """A retry would describe a moment that has already passed."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), status=429)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert len(sent(aioclient_mock, "post")) == 1


async def test_a_rejected_token_raises_a_repair(hass, aioclient_mock) -> None:
    """Nothing else in the entry is broken, so nothing else is interrupted."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), status=401)

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert ir.async_get(hass).async_get_issue(DOMAIN, TOKEN_ISSUE) is not None


async def test_a_scrobble_that_lands_clears_the_repair(hass, aioclient_mock) -> None:
    """A token replaced by hand must not leave the warning standing."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        TOKEN_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TOKEN_ISSUE,
    )
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), json={"id": SHOW_ID})

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert ir.async_get(hass).async_get_issue(DOMAIN, TOKEN_ISSUE) is None


async def test_simkl_being_unreachable_is_survivable(hass, aioclient_mock) -> None:
    """A scrobble is a report about television; it may not break anything."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), exc=TimeoutError())

    await SimklClient(hass, "token").async_scrobble(scrobble())


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

    found(aioclient_mock)
    aioclient_mock.post(url("stop"), side_effect=hold)

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


async def test_a_show_simkl_cannot_match_is_reported_and_survived(
    hass, aioclient_mock, caplog
) -> None:
    """404 id_err is what a scrobble carrying no usable id is answered with."""
    found(aioclient_mock)
    aioclient_mock.post(url("stop"), status=404, json={"error": "id_err", "code": 404})

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert "WARNING" not in caplog.text
    assert len(sent(aioclient_mock, "post")) == 1


async def test_a_search_that_cannot_be_reached_scrobbles_nothing(
    hass, aioclient_mock
) -> None:
    """Guessing an id would attach the episode to whatever that id happens to be."""
    aioclient_mock.get(SEARCH, exc=TimeoutError())

    await SimklClient(hass, "token").async_scrobble(scrobble())

    assert not sent(aioclient_mock, "post")
