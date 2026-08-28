"""What a loaded entry knows, for the entry's Download diagnostics button.

Every value here answers a question that otherwise takes a shell on the Home
Assistant host: whether each graft actually installed, whether the developer
token scrape is holding, which account serial the last play used, which
players are being watched, and which SIMKL id a show resolved to.

That last one is the failure this integration cannot see from outside. A wrong
id is accepted by SIMKL and the episode is filed under whichever show it names,
so the only place the mistake is visible is the cache it came from.

Credentials are redacted; the resolved show titles are not, because they are the
point of the report. That makes the file a record of what the household has been
watching, which is what the README warns before telling anyone to attach it to an
issue.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .applemusic.const import CONF_USER_TOKEN
from .const import DOMAIN
from .data import RuntimeData
from .simkl.const import CONF_SIMKL_TOKEN

TO_REDACT = {CONF_USER_TOKEN, CONF_SIMKL_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Describe one config entry, loaded or not."""
    runtime: RuntimeData | None = hass.data.get(DOMAIN)

    payload: dict[str, Any] = {
        # Home Assistant's own verdict rather than one derived from hass.data,
        # and it distinguishes a retrying entry from a failed one.
        "state": entry.state.value,
        "entry": async_redact_data(entry.data, TO_REDACT),
    }

    # An entry that failed setup is the one most worth asking about, and it has
    # no runtime to describe — so what it was configured with is the answer.
    if runtime is None:
        return payload

    client = runtime.apple_music
    expires = client.developer_token_expires_at

    payload["grafts"] = dict(runtime.grafted)
    payload["apple_music"] = {
        "storefront": client.storefront,
        "library": client.has_user_token,
        "developer_token_expires": expires.isoformat() if expires else None,
        "account_serial": runtime.account_serial,
    }
    payload["simkl"] = {
        "linked": runtime.scrobbler is not None,
        "watching": runtime.scrobbler.watching if runtime.scrobbler else [],
        "shows": runtime.scrobbler.shows if runtime.scrobbler else {},
    }
    return payload
