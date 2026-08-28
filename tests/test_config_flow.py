"""The setup flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sonos_apple_music.applemusic.api import (
    AppleMusicError,
    UserTokenInvalid,
)
from custom_components.sonos_apple_music.applemusic.const import (
    CONF_STOREFRONT,
    CONF_USER_TOKEN,
)
from custom_components.sonos_apple_music.applemusic.dev_token import TokenError
from custom_components.sonos_apple_music.const import DOMAIN

from .conftest import USER_TOKEN


async def start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def answering(*, storefront: str = "gb", error: Exception | None = None):
    """Stand in for the one library request the flow makes to check the token."""
    return patch(
        "custom_components.sonos_apple_music.applemusic.api.AppleMusicClient.get",
        AsyncMock(side_effect=error, return_value={"data": [{"id": storefront}]}),
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USER_TOKEN] == ""
    assert result["data"][CONF_STOREFRONT] == "gb"


async def test_a_valid_user_token_is_kept(hass) -> None:
    with answering():
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: "us"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USER_TOKEN] == USER_TOKEN


async def test_the_storefront_follows_the_account(hass) -> None:
    """A typed storefront is a guess; the account knows its own."""
    with answering(storefront="fr"):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: USER_TOKEN, CONF_STOREFRONT: "us"}
        )

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
