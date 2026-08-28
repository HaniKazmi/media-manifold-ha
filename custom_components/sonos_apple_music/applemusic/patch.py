"""Graft Apple Music onto the core Sonos integration at runtime.

Two seams, chosen because they are the narrowest points that cover browse,
search and play:

- ``sonos.media_browser.async_browse_media`` is patched on the *module*. The
  entity looks it up as a module attribute on every call, and ``root_payload``
  recurses through the same global, so one patch covers both.
- ``SonosMediaPlayerEntity.async_play_media`` / ``async_search_media`` are
  patched on the class. Method lookup is per call, so live entities pick these
  up without being reloaded.

Browse is deliberately *not* also patched on the entity: both seams reach the
same call, and patching both grafts the Apple Music node twice.

If any expected attribute is missing — an upstream refactor, or Sonos never set
up so soco is not installed — nothing is patched and Sonos is left exactly as it
was. A broken add-on must not take the speakers down with it.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_ENQUEUE,
    BrowseMedia,
    MediaClass,
    MediaPlayerEnqueue,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_player.errors import SearchError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from ..const import DOMAIN
from . import browse as apple_browse
from .api import AppleMusicError
from .const import DEFAULT_SN, URI_PREFIX
from .play import discover_sn, enqueue
from .resolve import NotPlayable, async_build_item

if TYPE_CHECKING:
    from .api import AppleMusicClient

_LOGGER = logging.getLogger(__name__)

# The integration this grafts onto, as the log line names it.
NAME = "Sonos"

_originals: dict[str, Any] = {}

_NO_FAVORITE_ISSUE = "no_apple_music_favorite"


def _client(hass: HomeAssistant) -> AppleMusicClient | None:
    return hass.data.get(DOMAIN)


def async_install(hass: HomeAssistant) -> bool:
    """Patch the Sonos integration. Returns False if it was left untouched."""
    if _originals:
        _LOGGER.debug("Sonos patches already installed")
        return True

    # ImportError means Sonos was never set up, so its requirements (soco) are
    # absent; AttributeError means an upstream refactor moved a seam, and names
    # the one that moved. Either way nothing has been replaced yet, so Sonos is
    # left exactly as it was.
    try:
        from homeassistant.components.sonos import media_browser
        from homeassistant.components.sonos.media_player import SonosMediaPlayerEntity

        _originals["browse"] = media_browser.async_browse_media
        _originals["play"] = SonosMediaPlayerEntity.async_play_media
        _originals["search"] = SonosMediaPlayerEntity.async_search_media
    except (ImportError, AttributeError) as err:
        _originals.clear()
        _LOGGER.warning(
            "Sonos is unavailable or has changed shape (%s); Apple Music left "
            "uninstalled so Sonos keeps working",
            err,
        )
        return False

    _originals["media_browser"] = media_browser
    _originals["entity"] = SonosMediaPlayerEntity

    media_browser.async_browse_media = partial(_browse, hass, _originals["browse"])
    SonosMediaPlayerEntity.async_play_media = _make_play(_originals["play"])
    SonosMediaPlayerEntity.async_search_media = _make_search(_originals["search"])

    _LOGGER.debug("Apple Music grafted onto Sonos")
    return True


def async_remove() -> None:
    """Restore the Sonos integration to its unpatched state."""
    if not _originals:
        return
    _originals["media_browser"].async_browse_media = _originals["browse"]
    _originals["entity"].async_play_media = _originals["play"]
    _originals["entity"].async_search_media = _originals["search"]
    _originals.clear()
    _LOGGER.debug("Apple Music patches removed from Sonos")


async def _browse(
    hass: HomeAssistant,
    original: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> BrowseMedia:
    """Serve Apple Music ids, and graft the Apple Music node onto the root.

    Args are forwarded verbatim rather than unpacked, so a change to any
    parameter this wrapper does not read passes straight through. The upstream
    order is (hass, speaker, media, get_browse_image_url, media_content_id,
    media_content_type) — note the id precedes the type.

    Read positionally rather than from ``inspect.signature``: the callable being
    wrapped may itself be another integration's ``*args`` wrapper, whose
    signature names nothing.
    """
    content_id = kwargs.get("media_content_id", args[4] if len(args) > 4 else None)

    if content_id and content_id.startswith(URI_PREFIX):
        if (client := _client(hass)) is None:
            raise HomeAssistantError("Apple Music is not configured")
        return await apple_browse.async_browse(client, content_id)

    result = await original(*args, **kwargs)

    if content_id is not None:
        return result
    if (client := _client(hass)) is None:
        return result

    return _graft(result, apple_browse.root_payload(client))


def _graft(root: BrowseMedia, apple: BrowseMedia) -> BrowseMedia:
    """Add the Apple Music node to the Sonos root.

    ``root_payload`` returns its single child directly when only one source
    exists, so the result is not always a root container; in that case a fresh
    root is built rather than appending to whatever that child happened to be.
    """
    if root.media_content_type == "root":
        root.children = [*(root.children or []), apple]
        return root

    return BrowseMedia(
        title="Sonos",
        media_class=MediaClass.DIRECTORY,
        media_content_id="",
        media_content_type="root",
        can_play=False,
        can_expand=True,
        children=[root, apple],
        children_media_class=MediaClass.DIRECTORY,
    )


def _account_serial(hass: HomeAssistant, favorites: Any) -> int:
    """The household's Apple Music serial, raising a repair when it is a guess.

    DEFAULT_SN is right for plenty of households, so a failed scan still plays:
    a wrong guess costs an enqueue that starts no audio, while refusing outright
    costs playback that would have worked. What is not affordable is the silence
    in between — an item that queues cleanly and never sounds, with nothing
    naming the reason. The repair issue is that name.
    """
    if (serial := discover_sn(favorites)) is not None:
        ir.async_delete_issue(hass, DOMAIN, _NO_FAVORITE_ISSUE)
        return serial

    ir.async_create_issue(
        hass,
        DOMAIN,
        _NO_FAVORITE_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_NO_FAVORITE_ISSUE,
    )
    _LOGGER.warning(
        "No Apple Music favorite found on this household, so the account serial "
        "is a guess (sn=%s); playback may enqueue and stay silent. Add any Apple "
        "Music favorite in the Sonos app to fix this",
        DEFAULT_SN,
    )
    return DEFAULT_SN


def _make_play(original: Callable[..., Any]) -> Callable[..., Any]:
    async def async_play_media(
        self: Any, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        if not (isinstance(media_id, str) and media_id.startswith(URI_PREFIX)):
            await original(self, media_type, media_id, **kwargs)
            return

        if (client := _client(self.hass)) is None:
            raise HomeAssistantError("Apple Music is not configured")

        # Sonos's own budget for a service call rather than a copy of the
        # number: container enqueues expand server-side and a large playlist
        # takes seconds. Imported here, not at module scope, because reaching
        # sonos.const executes sonos/__init__.py and with it soco.
        from homeassistant.components.sonos.const import LONG_SERVICE_TIMEOUT

        sn = _account_serial(self.hass, self.speaker.favorites)
        try:
            item = await async_build_item(client, media_id, sn)
        except (NotPlayable, AppleMusicError) as err:
            raise HomeAssistantError(str(err)) from err

        mode = kwargs.get(ATTR_MEDIA_ENQUEUE) or MediaPlayerEnqueue.REPLACE
        # Commands go to the group coordinator, not to whichever member was
        # addressed, matching what the rest of the Sonos integration does.
        await self.hass.async_add_executor_job(
            partial(
                enqueue,
                self.coordinator.soco,
                item,
                mode,
                self.media.queue_position,
                LONG_SERVICE_TIMEOUT,
            )
        )

    return async_play_media


def _make_search(original: Callable[..., Any]) -> Callable[..., Any]:
    async def async_search_media(self: Any, query: SearchMediaQuery) -> SearchMedia:
        content_id = query.media_content_id or ""
        if not content_id.startswith(URI_PREFIX):
            return await original(self, query)

        if (client := _client(self.hass)) is None:
            raise HomeAssistantError("Apple Music is not configured")

        media_class = None
        if query.media_filter_classes:
            media_class = next(iter(query.media_filter_classes), None)

        try:
            results = await apple_browse.async_search(
                client, query.search_query, media_class
            )
        except AppleMusicError as err:
            raise SearchError(f"Apple Music: {err}") from err
        return SearchMedia(result=results)

    return async_search_media
