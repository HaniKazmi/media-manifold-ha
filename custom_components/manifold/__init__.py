"""Wiring between the media players a household has and the services around them.

Three grafts, each onto an integration Home Assistant ships: Apple Music into
the Sonos media browser, Jellyfin onto the Apple TV by way of Infuse, and the
Apple TV app's real episode numbering onto the Apple TV entity. None adds an
entity. All wrap the seams of the integration they extend, so the players a
household already has gain a source rather than a duplicate that has to be kept
in step with the real one.

They are independent: a household with only one of the players sets up
normally, and a graft that cannot install leaves its integration untouched.

One thing here is not a graft. Scrobbling Apple TV playback to SIMKL only reads
states Home Assistant already publishes, so it patches nothing and hangs off the
entry directly, and it runs only for a household that has linked SIMKL. The
numbering graft is what makes those states right for a show numbered in
production blocks; without it the scrobbler falls back to the content id.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .applemusic import patch as applemusic
from .applemusic.api import AppleMusicClient, UserTokenInvalid
from .applemusic.const import CONF_STOREFRONT, CONF_USER_TOKEN, DEFAULT_STOREFRONT
from .applemusic.dev_token import DeveloperToken, TokenError
from .appletv import patch as numbering
from .const import DOMAIN
from .data import RuntimeData
from .infuse import patch as infuse
from .simkl import watch as simkl
from .simkl.const import CONF_SIMKL_TOKEN

_LOGGER = logging.getLogger(__name__)

# Every graft is a module exposing NAME, async_install(hass) -> bool and
# async_remove(). Adding one is adding it here; nothing below counts them.
GRAFTS = (applemusic, infuse, numbering)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the client and graft Apple Music onto Sonos."""
    developer_token = DeveloperToken(hass)
    try:
        await developer_token.async_get()
    except TokenError as err:
        # Apple has changed the web player. Retrying on a timer will not help
        # until the scrape is fixed, but ConfigEntryNotReady keeps the entry
        # alive so a later HA restart picks up a fixed version.
        raise ConfigEntryNotReady(f"No Apple Music developer token: {err}") from err

    client = AppleMusicClient(
        hass,
        developer_token,
        user_token=entry.data.get(CONF_USER_TOKEN),
        storefront=entry.data.get(CONF_STOREFRONT, DEFAULT_STOREFRONT),
        on_token_invalid=lambda: entry.async_start_reauth(hass),
    )
    try:
        await client.async_resolve_storefront()
    except UserTokenInvalid as err:
        # The library surfaces would render and then fail one by one. Failing the
        # entry instead puts the cookie back in front of the user, which is the
        # only thing that fixes it.
        raise ConfigEntryAuthFailed(str(err)) from err

    runtime = RuntimeData(apple_music=client)
    hass.data[DOMAIN] = runtime

    # Each graft is onto an integration that need not exist. Registering the
    # removal before the check matters: Home Assistant runs these callbacks on
    # the setup-failure path too, so nothing stays patched behind a raise.
    grafted = runtime.grafted
    for graft in GRAFTS:
        grafted[graft.NAME] = graft.async_install(hass)
        entry.async_on_unload(graft.async_remove)

    # Started before the readiness check below: it reads states Home Assistant
    # already publishes and touches no seam, so a household whose grafts all
    # decline still has scrobbling to do.
    if (scrobbler := simkl.async_start(hass, entry)) is not None:
        runtime.scrobbler = scrobbler
        entry.async_on_unload(scrobbler.async_stop)

    # Only a household with nothing at all to set up waits, and that is usually
    # one that has not finished starting, so it retries. A graft that declines
    # while another takes is not retried: adding Sonos to a household that had
    # only an Apple TV needs a reload of this entry, or a restart.
    if not any(grafted.values()) and scrobbler is None:
        hass.data.pop(DOMAIN, None)
        raise ConfigEntryNotReady(
            f"None of {', '.join(grafted)} is ready and SIMKL is not linked; "
            f"the grafts will install once one of them is"
        )

    _LOGGER.info(
        "Grafted onto %s; Apple Music storefront %s, library %s, SIMKL %s",
        ", ".join(f"{name} {'yes' if ok else 'no'}" for name, ok in grafted.items()),
        client.storefront,
        "on" if client.has_user_token else "off",
        "on" if entry.data.get(CONF_SIMKL_TOKEN) else "off",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop the client; the grafts come off through their unload callbacks."""
    hass.data.pop(DOMAIN, None)
    return True


