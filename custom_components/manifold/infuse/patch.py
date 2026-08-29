"""Graft Jellyfin playback onto the core Apple TV integration at runtime.

One seam is enough: ``AppleTvMediaPlayer.async_play_media``, patched on the
*class*, so per-call method lookup means live entities pick it up without being
reloaded.

Browse is deliberately not patched. With an app list present the Apple TV entity
passes no content filter to the media source tree, so Jellyfin's films and
episodes already render in its media browser; the only thing missing is what
happens when one is pressed.

The finished ``infuse://`` URL goes back through the original method as
``MediaType.URL``, which upstream answers with ``apps.launch_app`` — the call
that opens a deep link on tvOS. Re-entering rather than reaching for
``self.atv`` keeps this on Home Assistant's own contract, so a television that
is disconnected declines the launch there as it would any other. The Jellyfin
lookup has already happened by then: what re-entering buys is one caller of
pyatv rather than two, not a round trip saved.

If the seam is missing — an upstream refactor, or apple_tv never set up so pyatv
is absent — nothing is patched and the Apple TV is left exactly as it was. A
broken add-on must not take the television down with it.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.media_player import MediaType
from homeassistant.components.media_source import URI_SCHEME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import link, resolve

_LOGGER = logging.getLogger(__name__)

# Jellyfin's media source names items by bare id, so the id is everything after
# the prefix. Matching the domain too means no other source can be caught by it.
URI_PREFIX = f"{URI_SCHEME}{resolve.JELLYFIN_DOMAIN}/"

# Module-level rather than per-entry because the seam is process-wide: the
# manifest sets `single_config_entry`, so there is exactly one owner for it.
# The integration this grafts onto, as the log line names it.
NAME = "Apple TV"

_originals: dict[str, Any] = {}


def async_install(hass: HomeAssistant) -> bool:
    """Patch the Apple TV integration. Returns False if it was left untouched."""
    if _originals:
        _LOGGER.debug("Apple TV patches already installed")
        return True

    try:
        from homeassistant.components.apple_tv.media_player import AppleTvMediaPlayer

        _originals["play"] = AppleTvMediaPlayer.async_play_media
    except (ImportError, AttributeError) as err:
        _originals.clear()
        _LOGGER.warning(
            "Apple TV is unavailable or has changed shape (%s); Infuse playback "
            "left uninstalled so the Apple TV keeps working",
            err,
        )
        return False

    _originals["entity"] = AppleTvMediaPlayer
    AppleTvMediaPlayer.async_play_media = _make_play(_originals["play"])

    _LOGGER.debug("Jellyfin playback grafted onto Apple TV")
    return True


def async_remove() -> None:
    """Restore the Apple TV integration to its unpatched state."""
    if not _originals:
        return
    _originals["entity"].async_play_media = _originals["play"]
    _originals.clear()
    resolve.forget()
    _LOGGER.debug("Infuse patches removed from Apple TV")


def _make_play(original: Callable[..., Any]) -> Callable[..., Any]:
    async def async_play_media(
        self: Any, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        if not (isinstance(media_id, str) and media_id.startswith(URI_PREFIX)):
            await original(self, media_type, media_id, **kwargs)
            return

        item_id = media_id[len(URI_PREFIX) :]
        if (api := resolve.async_client(self.hass)) is None:
            raise HomeAssistantError(
                "The Jellyfin integration is not loaded, so its items cannot be "
                "resolved for Infuse"
            )

        item = await resolve.async_item(self.hass, api, item_id)

        # An item the server does not know, and audio it plays perfectly well
        # over RAOP already, both belong to the method this one wraps.
        if not item or not link.is_video(item):
            await original(self, media_type, media_id, **kwargs)
            return

        url = await resolve.async_url(self.hass, api, item_id, item)
        await original(self, MediaType.URL, url, **kwargs)

    return async_play_media
