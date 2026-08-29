"""The PIN device flow.

What matters here is telling SIMKL's two ordinary answers apart — a code not yet
approved, and a code exchanged for a token — and refusing to read anything else
as either of them.
"""

from __future__ import annotations

import pytest

from custom_components.manifold.simkl.pin import PinError, async_poll, async_request_code

CODE = "https://api.simkl.com/oauth/pin"
POLL = "https://api.simkl.com/oauth/pin/ABCDE"


async def test_a_code_is_returned_with_where_to_approve_it(
    hass, aioclient_mock
) -> None:
    aioclient_mock.get(
        CODE,
        json={
            "result": "OK",
            "user_code": "ABCDE",
            "verification_url": "https://simkl.com/pin",
        },
    )

    assert await async_request_code(hass) == ("ABCDE", "https://simkl.com/pin")


async def test_the_url_simkl_names_is_the_one_shown(hass, aioclient_mock) -> None:
    """A moved approval page needs no release here."""
    aioclient_mock.get(
        CODE,
        json={"result": "OK", "user_code": "ABCDE", "verification_url": "https://s/p"},
    )

    assert (await async_request_code(hass)).verification_url == "https://s/p"


async def test_an_answer_with_no_code_is_refused(hass, aioclient_mock) -> None:
    """Showing an empty box would leave the user nothing to type."""
    aioclient_mock.get(CODE, json={"result": "KO", "message": "no"})

    with pytest.raises(PinError):
        await async_request_code(hass)


async def test_simkl_being_unreachable_is_a_pin_error(hass, aioclient_mock) -> None:
    aioclient_mock.get(CODE, status=502)

    with pytest.raises(PinError):
        await async_request_code(hass)


async def test_an_answer_that_is_not_an_object_is_refused(
    hass, aioclient_mock
) -> None:
    """A proxy's error page parses as JSON and is not an answer."""
    aioclient_mock.get(CODE, json=["nope"])

    with pytest.raises(PinError):
        await async_request_code(hass)


async def test_an_unapproved_code_is_not_an_error(hass, aioclient_mock) -> None:
    """Not yet is the ordinary answer until the user finishes at simkl.com/pin."""
    aioclient_mock.get(POLL, json={"result": "KO", "message": "Authorization pending"})

    assert await async_poll(hass, "ABCDE") is None


async def test_an_approved_code_gives_up_the_token(hass, aioclient_mock) -> None:
    aioclient_mock.get(POLL, json={"result": "OK", "access_token": "simkl-token"})

    assert await async_poll(hass, "ABCDE") == "simkl-token"


async def test_an_answer_that_is_neither_is_refused(hass, aioclient_mock) -> None:
    """Reading an unknown answer as 'pending' would loop the user forever."""
    aioclient_mock.get(POLL, json={"result": "OK"})

    with pytest.raises(PinError):
        await async_poll(hass, "ABCDE")
