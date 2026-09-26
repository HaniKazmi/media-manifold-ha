"""Graft the Apple TV app's real episode numbering onto the core integration.

Two properties on ``AppleTvMediaPlayer`` are wrapped on the class:
``media_season`` and ``media_episode``. Each answers with pyatv's value when
pyatv has one, so a pyatv that learns to read the numbering itself wins without
a change here; only when pyatv reports nothing is the now-playing archive
decoded. The archive is reached through the pyatv object the entity already
holds: the metadata relayer's MRP instance, its player state manager, and the
metadata of the item that manager is playing.

That is three links into pyatv internals, and any of them can move. A moved link
answers None rather than raising, which leaves the entity exactly as upstream
ships it; the scrobbler then falls back to the content id, which is right for
every show numbered the ordinary way.

The properties are the seam, not the push callback. Upstream gates each on a
pyatv feature, and pyatv marks the season and episode features available only
when the plain protobuf fields are set — the fields the app leaves empty. A
callback that handed the entity a numbered ``Playing`` would still be masked by
that gate.

The fill is gated on the entity reporting a title, a proxy for it holding a
snapshot at all. After a push error the snapshot is cleared while the pyatv
object stays, and the player state may still describe the last item; a season
on a state with no show would be a claim about nothing.

If the seam is missing — apple_tv never set up so pyatv is absent, or an upstream
refactor — nothing is patched and the Apple TV is left exactly as it was.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from . import nowplaying

_LOGGER = logging.getLogger(__name__)

# The integration this grafts onto, as the log line names it. Distinct from the
# Infuse graft's name because the two are reported side by side.
NAME = "Apple TV numbering"

# Module-level rather than per-entry because the seam is process-wide: the
# manifest sets `single_config_entry`, so there is exactly one owner for it.
_originals: dict[str, Any] = {}

# pyatv's key for the MRP instance in the metadata relayer, taken at install
# because pyatv cannot be imported at module scope: it belongs to apple_tv and
# is absent until that integration has been set up.
_mrp: Any = None


def async_install(hass: HomeAssistant) -> bool:
    """Patch the Apple TV integration. Returns False if it was left untouched."""
    global _mrp
    if _originals:
        _LOGGER.debug("Apple TV numbering patches already installed")
        return True

    try:
        from homeassistant.components.apple_tv.media_player import AppleTvMediaPlayer
        from pyatv.const import Protocol

        season = AppleTvMediaPlayer.media_season
        episode = AppleTvMediaPlayer.media_episode
        if not isinstance(season, property) or not isinstance(episode, property):
            raise AttributeError("media_season and media_episode are not properties")
    except (ImportError, AttributeError) as err:
        _LOGGER.warning(
            "Apple TV is unavailable or has changed shape (%s); episode numbering "
            "left uninstalled so the Apple TV keeps working",
            err,
        )
        return False

    _mrp = Protocol.MRP
    _originals["entity"] = AppleTvMediaPlayer
    _originals["season"] = season
    _originals["episode"] = episode
    AppleTvMediaPlayer.media_season = _wrap(season, "season")
    AppleTvMediaPlayer.media_episode = _wrap(episode, "number")

    _LOGGER.debug("Episode numbering grafted onto Apple TV")
    return True


def async_remove() -> None:
    """Restore the Apple TV integration to its unpatched state."""
    if not _originals:
        return
    _originals["entity"].media_season = _originals["season"]
    _originals["entity"].media_episode = _originals["episode"]
    _originals.clear()
    _LOGGER.debug("Episode numbering patches removed from Apple TV")


def _wrap(original: property, field: str) -> property:
    def fget(self: Any) -> str | None:
        if (value := original.fget(self)) is not None:
            return value
        if (numbering := _numbering(self)) is None:
            return None
        # A string, because that is what the original answers with.
        return str(getattr(numbering, field))

    return property(fget, doc=original.__doc__)


def _numbering(entity: Any) -> nowplaying.Numbering | None:
    """What the archive says about the item the entity is describing.

    Decoded on every read: the archive is 840 bytes and a decode is tens of
    microseconds, inside a state write that costs far more.
    """
    if entity.media_title is None or (atv := getattr(entity, "atv", None)) is None:
        return None
    try:
        # pyatv's own accessor, which answers None for an empty queue and for a
        # field the app never set.
        blob = atv.metadata.get(_mrp).psm.playing.metadata_field("nowPlayingInfoData")
    except (AttributeError, ValueError):
        # A link has moved, there is no MRP instance, or the protobuf no longer
        # has a field by that name — which protobuf reports as a ValueError from
        # HasField. Each is answered upstream's way.
        return None
    return nowplaying.numbering(blob)
