"""The setup flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.manifold.applemusic.api import AppleMusicError, UserTokenInvalid
from custom_components.manifold.applemusic.const import CONF_STOREFRONT, CONF_USER_TOKEN
from custom_components.manifold.applemusic.dev_token import TokenError
from custom_components.manifold.const import DOMAIN
from custom_components.manifold.simkl.const import CONF_SIMKL_TOKEN, PIN_URL

from .conftest import USER_TOKEN


async def start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def answering(*, storefront: str = "gb", error: Exception | None = None):
    """Stand in for the one library request the flow makes to check the token."""
    return patch(
        "custom_components.manifold.applemusic.api.AppleMusicClient.get",
        AsyncMock(side_effect=error, return_value={"data": [{"id": storefront}]}),
    )


async def skip_simkl(hass, result):
    """Decline the SIMKL offer, which every completed Apple Music step reaches."""
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "simkl_link"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "simkl_skip"}
    )


async def test_the_form_is_shown_first(hass) -> None:
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_setup_without_a_user_token_succeeds(hass) -> None:
    """Catalog browsing and playback work without one, so it must not be required.

    Demanding a cookie up front would block a setup that is already useful.
    """
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )
    result = await skip_simkl(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USER_TOKEN] == ""
    assert result["data"][CONF_STOREFRONT] == "gb"


async def test_a_valid_user_token_is_kept(hass) -> None:
    with answering():
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: "us"}
        )
    result = await skip_simkl(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USER_TOKEN] == USER_TOKEN


async def test_the_storefront_follows_the_account(hass) -> None:
    """A typed storefront is a guess; the account knows its own."""
    with answering(storefront="fr"):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: "us"}
        )
    result = await skip_simkl(hass, result)

    assert result["data"][CONF_STOREFRONT] == "fr"


async def test_a_rejected_user_token_is_reported_on_its_field(hass) -> None:
    with answering(error=UserTokenInvalid("rejected")):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "stale", CONF_STOREFRONT: "gb"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_USER_TOKEN: "invalid_user_token"}


async def test_apple_being_unreachable_is_not_blamed_on_the_token(hass) -> None:
    """Telling the user to re-copy a cookie that is fine sends them in circles."""
    with answering(error=AppleMusicError("connection reset")):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: "gb"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_failed_scrape_blocks_setup_with_a_reason(
    hass, no_token_scraping
) -> None:
    """Nothing works without a developer token, so this cannot be waved through."""
    no_token_scraping.side_effect = TokenError("Apple changed the bundle")

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_developer_token"}


async def start_reauth(hass, config_entry):
    config_entry.add_to_hass(hass)
    return await config_entry.start_reauth_flow(hass)


async def test_reauth_asks_for_a_fresh_cookie(hass, config_entry) -> None:
    result = await start_reauth(hass, config_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


async def test_reauth_replaces_the_stored_token(hass, config_entry) -> None:
    """The entry is what everything else reads, so nothing is fixed until it is."""
    with answering():
        result = await start_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "B" * 42}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_USER_TOKEN] == "B" * 42


async def test_reauth_follows_the_account_storefront(hass, config_entry) -> None:
    """A re-pasted cookie may belong to a different account than the last one."""
    with answering(storefront="fr"):
        result = await start_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN}
        )

    assert config_entry.data[CONF_STOREFRONT] == "fr"


async def test_reauth_accepts_an_empty_token(hass, config_entry) -> None:
    """Giving up library access is a real answer, not a validation failure.

    Refusing it leaves the entry permanently broken for anyone unwilling to
    paste a cookie every few months.
    """
    result = await start_reauth(hass, config_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: ""}
    )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_USER_TOKEN] == ""


async def test_reauth_rejects_a_token_apple_still_refuses(hass, config_entry) -> None:
    with answering(error=UserTokenInvalid("rejected")):
        result = await start_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "still stale"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_USER_TOKEN: "invalid_user_token"}
    assert config_entry.data[CONF_USER_TOKEN] == USER_TOKEN


async def test_only_one_entry_is_allowed(hass, config_entry) -> None:
    """Two entries would install the patches twice and graft two Apple nodes."""
    config_entry.add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# --- SIMKL ------------------------------------------------------------------
#
# These drive the real PIN module against a mocked transport rather than
# patching it out: the flow's whole job here is reading SIMKL's two answers
# apart, and a stub of that module would assert only that the stub was called.


PIN_CODE = "https://api.simkl.com/oauth/pin"
PIN_POLL = "https://api.simkl.com/oauth/pin/ABCDE"


async def reach_simkl_pin(hass, aioclient_mock):
    """Get as far as the code being shown, the way a real setup does."""
    aioclient_mock.get(
        PIN_CODE,
        json={"result": "OK", "user_code": "ABCDE", "verification_url": PIN_URL},
    )
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "simkl_pin"}
    )


async def test_skipping_simkl_stores_no_token(hass) -> None:
    """Scrobbling is opt-in, and the rest of the integration must not need it."""
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )
    result = await skip_simkl(hass, result)

    assert result["data"][CONF_SIMKL_TOKEN] == ""


async def test_the_code_is_shown_before_it_is_polled(hass, aioclient_mock) -> None:
    """Polling a code the user has not seen can only ever answer 'not yet'."""
    result = await reach_simkl_pin(hass, aioclient_mock)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "simkl_pin"
    assert result["description_placeholders"]["code"] == "ABCDE"
    assert result["description_placeholders"]["url"] == PIN_URL


async def test_an_unapproved_code_keeps_the_same_code_on_screen(
    hass, aioclient_mock
) -> None:
    """A second code would invalidate the one the user is part way through."""
    result = await reach_simkl_pin(hass, aioclient_mock)
    aioclient_mock.get(PIN_POLL, json={"result": "KO", "message": "pending"})

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "authorization_pending"}
    assert result["description_placeholders"]["code"] == "ABCDE"


async def test_an_approved_code_stores_the_token(hass, aioclient_mock) -> None:
    result = await reach_simkl_pin(hass, aioclient_mock)
    aioclient_mock.get(PIN_POLL, json={"result": "OK", "access_token": "simkl-token"})

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SIMKL_TOKEN] == "simkl-token"
    assert result["data"][CONF_USER_TOKEN] == ""


async def test_simkl_being_unreachable_keeps_the_flow_alive(
    hass, aioclient_mock
) -> None:
    """The Apple Music answers are already collected; losing them costs a retype."""
    aioclient_mock.get(PIN_CODE, status=500)

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "simkl_pin"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "simkl_pin"
    assert result["errors"] == {"base": "simkl_unreachable"}


async def test_reconfigure_links_simkl_without_asking_for_apple_again(
    hass, config_entry, aioclient_mock
) -> None:
    """The entry is single-instance, so this is the only route to the step."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(
        PIN_CODE,
        json={"result": "OK", "user_code": "ABCDE", "verification_url": PIN_URL},
    )
    aioclient_mock.get(PIN_POLL, json={"result": "OK", "access_token": "simkl-token"})

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "simkl_pin"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_SIMKL_TOKEN] == "simkl-token"
    assert config_entry.data[CONF_USER_TOKEN] == USER_TOKEN


async def test_reconfigure_can_unlink_simkl(hass, config_entry) -> None:
    """Unlinking has to be reachable, or the only way out is deleting the entry."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_SIMKL_TOKEN: "simkl-token"}
    )

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "simkl_skip"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_SIMKL_TOKEN] == ""


async def test_re_entering_the_step_keeps_the_code_it_issued(
    hass, aioclient_mock
) -> None:
    """A second code would invalidate the one the user is part way through typing."""
    result = await reach_simkl_pin(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["description_placeholders"]["code"] == "ABCDE"
    searches = [c for c in aioclient_mock.mock_calls if c[0].lower() == "get"]
    assert len(searches) == 1


async def test_simkl_failing_the_exchange_says_so_on_the_form(
    hass, aioclient_mock
) -> None:
    """The code may still be good, so the user is left able to try again."""
    result = await reach_simkl_pin(hass, aioclient_mock)
    aioclient_mock.get(PIN_POLL, status=500)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "simkl_unreachable"}


async def test_reauth_leaves_simkl_linked(hass, config_entry) -> None:
    """The two credentials expire on their own schedules and are replaced apart."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_SIMKL_TOKEN: "simkl-token"}
    )

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: ""}
    )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_SIMKL_TOKEN] == "simkl-token"


async def test_a_code_simkl_will_not_exchange_is_replaced(hass, aioclient_mock) -> None:
    """A PIN expires, and showing the same dead one again can only fail again."""
    result = await reach_simkl_pin(hass, aioclient_mock)
    aioclient_mock.get(PIN_POLL, status=404)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        PIN_CODE,
        json={"result": "OK", "user_code": "FRESH", "verification_url": PIN_URL},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["description_placeholders"]["code"] == "FRESH"


async def test_the_entry_is_named_for_the_integration(hass) -> None:
    """The flow signs off with "Created configuration for <title>", so it is read.

    The conftest fixture builds an entry with this name too, and nothing else
    compares the two, so a title that drifts from it goes unnoticed.
    """
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", CONF_STOREFRONT: "gb"}
    )
    result = await skip_simkl(hass, result)

    assert result["title"] == "Media Manifold"
