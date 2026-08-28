"""Config flow for Apple Music on Sonos, and for linking SIMKL.

There is no browser sign-in step for Apple. The developer token this integration
uses is locked to Apple's own origin, so a page served by Home Assistant cannot
use it — MusicKit JS would be refused before it ever reached a login prompt.
Library access therefore comes from the ``media-user-token`` cookie of an
already signed-in music.apple.com session, which the user pastes here.

SIMKL is asked for second and is equally optional: it turns on scrobbling of
Apple TV playback and nothing else, so a household that skips it keeps every
other surface. Its device flow needs no secret and no redirect URI, so the code
can be approved on any device and the flow finishes here.

Both credentials are optional, so the flow completes either way, and
``reconfigure`` exists to link or unlink SIMKL later without deleting the entry.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
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
from .simkl import pin
from .simkl.const import CONF_SIMKL_TOKEN, PIN_URL

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
    """Collect the optional credentials and confirm each service is reachable."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing collected."""
        self._data: dict[str, Any] = {}
        self._code: pin.Code | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the Apple Music step, then offer SIMKL."""
        if user_input is None:
            return self._form()

        user_token = (user_input.get(CONF_USER_TOKEN) or "").strip()
        storefront = (
            (user_input.get(CONF_STOREFRONT) or DEFAULT_STOREFRONT).strip().lower()
        )

        checked, errors = await self._async_check(user_token, storefront)
        if errors:
            return self._form(errors)

        self._data = {CONF_USER_TOKEN: user_token, CONF_STOREFRONT: checked}
        return await self.async_step_simkl_link()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link or unlink SIMKL on an entry that already exists.

        Apple Music is not asked again: its cookie has a reauth flow of its own,
        and this is the only way to reach the SIMKL step once setup is done.
        """
        entry = self._get_reconfigure_entry()
        self._data = {
            CONF_USER_TOKEN: entry.data.get(CONF_USER_TOKEN, ""),
            CONF_STOREFRONT: entry.data.get(CONF_STOREFRONT, DEFAULT_STOREFRONT),
        }
        return await self.async_step_simkl_link()

    async def async_step_simkl_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether to link SIMKL at all."""
        return self.async_show_menu(
            step_id="simkl_link", menu_options=["simkl_pin", "simkl_skip"]
        )

    async def async_step_simkl_skip(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish without scrobbling, and drop a token that was there before."""
        return self._async_finish("")

    async def async_step_simkl_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a code to approve at simkl.com/pin, then exchange it.

        The exchange happens when the form is submitted rather than on a timer:
        approving the code means leaving Home Assistant for another device, and
        the press of Submit is the one moment that reliably means it is done.
        """
        if self._code is None:
            try:
                self._code = await pin.async_request_code(self.hass)
            except pin.PinError as err:
                _LOGGER.debug("SIMKL would not issue a code: %s", err)
                return self._pin_form({"base": "cannot_connect"})
            return self._pin_form()

        if user_input is None:
            return self._pin_form()

        try:
            token = await pin.async_poll(self.hass, self._code.user_code)
        except pin.PinError as err:
            _LOGGER.debug("SIMKL would not exchange the code: %s", err)
            return self._pin_form({"base": "cannot_connect"})

        if token is None:
            return self._pin_form({"base": "authorization_pending"})

        return self._async_finish(token)

    def _pin_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """The code, and a button that means "I have approved it"."""
        return self.async_show_form(
            step_id="simkl_pin",
            data_schema=vol.Schema({}),
            errors=errors or {},
            description_placeholders={
                "code": self._code.user_code if self._code else "",
                "url": self._code.verification_url if self._code else PIN_URL,
            },
        )

    def _async_finish(self, simkl_token: str) -> ConfigFlowResult:
        """Store what was collected, whichever step the flow started from."""
        data = {**self._data, CONF_SIMKL_TOKEN: simkl_token}
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data_updates=data
            )
        return self.async_create_entry(title="Apple Music", data=data)

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
