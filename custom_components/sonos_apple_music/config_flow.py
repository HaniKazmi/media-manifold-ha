"""Config flow for Apple Music on Sonos.

There is no browser sign-in step. The developer token this integration uses is
locked to Apple's own origin, so a page served by Home Assistant cannot use it —
MusicKit JS would be refused before it ever reached a login prompt. Library
access therefore comes from the ``media-user-token`` cookie of an already
signed-in music.apple.com session, which the user pastes here.

The cookie is optional: catalog browsing, search and playback all work without
it, so the flow completes either way.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .applemusic.api import AppleMusicClient, AppleMusicError, UserTokenInvalid
from .applemusic.const import CONF_STOREFRONT, CONF_USER_TOKEN, DEFAULT_STOREFRONT
from .applemusic.dev_token import DeveloperToken, TokenError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_TOKEN_FIELD = {
    vol.Optional(CONF_USER_TOKEN, default=""): TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD)
    )
}

_SCHEMA = vol.Schema(
    {**_TOKEN_FIELD, vol.Optional(CONF_STOREFRONT, default=DEFAULT_STOREFRONT): str}
)

# Reauth asks for the cookie alone: the storefront is read back from the account
# the cookie belongs to, and the entry already holds the answer for the case
# where the field is left empty.
_REAUTH_SCHEMA = vol.Schema(_TOKEN_FIELD)


class AppleMusicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect the optional user token and confirm Apple is reachable."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the single setup step."""
        if user_input is None:
            return self._form()

        user_token = (user_input.get(CONF_USER_TOKEN) or "").strip()
        storefront = (
            (user_input.get(CONF_STOREFRONT) or DEFAULT_STOREFRONT).strip().lower()
        )

        checked, errors = await self._async_check(user_token, storefront)
        if errors:
            return self._form(errors)

        return self.async_create_entry(
            title="Apple Music",
            data={CONF_USER_TOKEN: user_token, CONF_STOREFRONT: checked},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Apple has rejected the stored cookie; ask for a fresh one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the cookie, or drop it and keep the catalog surfaces.

        An empty field is a real answer rather than a validation failure: it
        gives up library access, which is what someone who no longer wants to
        paste a cookie every few months is asking for.
        """
        entry = self._get_reauth_entry()
        if user_input is None:
            return self._form(step_id="reauth_confirm")

        user_token = (user_input.get(CONF_USER_TOKEN) or "").strip()
        storefront = entry.data.get(CONF_STOREFRONT, DEFAULT_STOREFRONT)

        checked, errors = await self._async_check(user_token, storefront)
        if errors:
            return self._form(errors, step_id="reauth_confirm")

        return self.async_update_reload_and_abort(
            entry,
            data_updates={CONF_USER_TOKEN: user_token, CONF_STOREFRONT: checked},
        )

    async def _async_check(
        self, user_token: str, storefront: str
    ) -> tuple[str, dict[str, str]]:
        """Confirm Apple is reachable, returning the storefront to store.

        The returned storefront is the account's own whenever a cookie proves
        which account that is; the typed or stored one is only a guess.
        """
        developer_token = DeveloperToken(self.hass)
        try:
            # Forced: setup is the moment to find out that the scrape still
            # works, rather than at the first browse days later.
            await developer_token.async_get(force_refresh=True)
        except TokenError as err:
            _LOGGER.error("Developer token unavailable: %s", err)
            return storefront, {"base": "no_developer_token"}

        if not user_token:
            return storefront, {}

        client = AppleMusicClient(self.hass, developer_token, user_token, storefront)
        try:
            # One request does both jobs: it proves the cookie reaches a library
            # endpoint, and a typed storefront is only a guess while the account
            # knows its own.
            result = await client.get("me/storefront")
        except UserTokenInvalid:
            return storefront, {CONF_USER_TOKEN: "invalid_user_token"}
        except AppleMusicError as err:
            _LOGGER.debug("Apple Music unreachable during setup: %s", err)
            return storefront, {"base": "cannot_connect"}

        if not (data := result.get("data")):
            return storefront, {CONF_USER_TOKEN: "invalid_user_token"}
        return data[0]["id"], {}

    def _form(
        self, errors: dict[str, str] | None = None, step_id: str = "user"
    ) -> ConfigFlowResult:
        schema = _REAUTH_SCHEMA if step_id == "reauth_confirm" else _SCHEMA
        return self.async_show_form(
            step_id=step_id, data_schema=schema, errors=errors or {}
        )
