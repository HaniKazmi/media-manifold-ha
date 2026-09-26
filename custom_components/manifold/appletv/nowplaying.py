"""What the Apple TV app says about an episode, in the one place it says it.

pyatv reads its episode fields — ``series_name``, ``season_number`` and
``episode_number`` — from plain protobuf fields the TV app leaves empty. What the
app does fill is ``nowPlayingInfoData``: an NSKeyedArchiver plist whose
``TVRAdditionalMetadata`` dictionary carries the numbering as strings, the same
data the iOS Remote displays::

    avkt/com.apple.avkit.seasonNumber    "6"
    avkt/com.apple.avkit.episodeNumber   "2"
    mdta/com.apple.hls.episode-title     "Daddy Issues"

This matters because the ``media_content_id`` the app also reports does not
always carry the public season. Slow Horses season 6 arrives as ``A0006403008``:
the two digits Apple reserves for the season count production blocks, and that
show is shot two seasons at a time, so the slice reads season 3, episode 8 — an
episode that does not exist, and one SIMKL refuses. The archive says season 6,
episode 2.

This module imports nothing from Home Assistant or pyatv. It is bytes in,
numbering out, and checkable against a blob captured from a television.
"""

from __future__ import annotations

import plistlib
from typing import Any, NamedTuple

_ADDITIONAL = "TVRAdditionalMetadata"
_SEASON = "avkt/com.apple.avkit.seasonNumber"
_EPISODE = "avkt/com.apple.avkit.episodeNumber"

# An archive is a graph of references and nothing stops one from pointing at
# itself. The real one reaches its numbering strings at depth 5 (root UID,
# dictionary, value UID, nested dictionary, key UID, string); past this it is
# not a now-playing dictionary, whatever else it is.
_MAX_DEPTH = 16

# Everything a malformed archive can raise on the way to an answer. A blob that
# is not the shape expected is the same answer as no blob at all — and so are
# None and the empty bytes protobuf answers with for an unset field, which
# plistlib refuses as invalid files.
_MALFORMED = (
    plistlib.InvalidFileException,
    AttributeError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)


class Numbering(NamedTuple):
    """Which episode of a show, as the app numbers it."""

    season: int
    number: int


def numbering(blob: bytes | None) -> Numbering | None:
    """The season and episode an archive names, or None when it names neither.

    Both numbers are required: a season without an episode does not identify
    anything a scrobble could report.
    """
    try:
        additional = _unarchive(blob)[_ADDITIONAL]
        return Numbering(int(additional[_SEASON]), int(additional[_EPISODE]))
    except _MALFORMED:
        return None


def _unarchive(blob: bytes) -> Any:
    """Resolve an NSKeyedArchiver plist into plain dicts, lists and scalars.

    The archive stores every object once in ``$objects`` and everything else as
    a UID into that table. Dictionaries and arrays are the classes that carry
    ``NS.keys`` and ``NS.objects``; any other class is flattened to its fields,
    which is enough to reach the strings this module reads.

    pyatv ships a reader for these archives, ``read_archive_properties`` in its
    Companion protocol, but it follows UIDs through plain dicts only and answers
    None for a root keyed through ``NS.keys``, which is what this archive is.
    """
    archive = plistlib.loads(blob)
    objects = archive["$objects"]

    def resolve(value: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("archive nests deeper than a now-playing dictionary")
        if isinstance(value, plistlib.UID):
            return resolve(objects[value.data], depth + 1)
        if isinstance(value, dict):
            if "NS.keys" in value:
                pairs = zip(value["NS.keys"], value["NS.objects"], strict=True)
                return {
                    resolve(key, depth + 1): resolve(item, depth + 1)
                    for key, item in pairs
                }
            if "NS.objects" in value:
                return [resolve(item, depth + 1) for item in value["NS.objects"]]
            return {
                key: resolve(item, depth + 1)
                for key, item in value.items()
                if key != "$class"
            }
        if isinstance(value, list):
            return [resolve(item, depth + 1) for item in value]
        return value

    return resolve(archive["$top"]["root"])
