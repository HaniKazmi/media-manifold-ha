"""Reading the Apple TV app's now-playing archive as an episode.

The real archive is the fixture: 840 bytes captured from a television playing
Slow Horses season 6, episode 2, whose content id slices to season 3, episode 8.
The malformed cases are built here, since each is one departure from that shape.
"""

from __future__ import annotations

import plistlib
from typing import Any

import pytest

from custom_components.manifold.appletv.nowplaying import Numbering, numbering

from .conftest import SLOW_HORSES_ARCHIVE

SEASON = "avkt/com.apple.avkit.seasonNumber"
EPISODE = "avkt/com.apple.avkit.episodeNumber"


def plist(objects: list[Any]) -> bytes:
    """An NSKeyedArchiver plist whose root is object 1, as Foundation writes it."""
    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def archive(additional: dict[str, Any] | None) -> bytes:
    """A now-playing archive shaped like the television's.

    A dictionary is its keys and values as UIDs into the object table, and the
    decoder reads nothing else of it, so no class descriptions are written.
    """
    objects: list[Any] = ["$null", None]

    def add(value: Any) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    def dictionary(items: dict[str, Any]) -> dict[str, Any]:
        keys = [add(key) for key in items]
        values = [
            add(dictionary(value) if isinstance(value, dict) else value)
            for value in items.values()
        ]
        return {"NS.keys": keys, "NS.objects": values}

    info: dict[str, Any] = {"AVMediaRemoteManagerNowPlayingInfoHasDescription": 1}
    if additional is not None:
        info["TVRAdditionalMetadata"] = additional
    objects[1] = dictionary(info)
    return plist(objects)


def test_the_television_archive_names_the_public_numbering() -> None:
    """Season 6, episode 2: what the iOS Remote shows, and what SIMKL lists for
    the air date. The content id on the same state slices to 3 and 8."""
    assert numbering(SLOW_HORSES_ARCHIVE) == Numbering(6, 2)


def test_a_built_archive_reads_the_same_way() -> None:
    """The builder has to produce what the decoder reads, or the malformed cases
    below pass for the wrong reason."""
    assert numbering(archive({SEASON: "4", EPISODE: "12"})) == Numbering(4, 12)


def test_numbers_already_numeric_are_taken_too() -> None:
    assert numbering(archive({SEASON: 4, EPISODE: 12})) == Numbering(4, 12)


@pytest.mark.parametrize(
    "additional",
    [
        {SEASON: "6"},
        {EPISODE: "2"},
        {SEASON: "six", EPISODE: "2"},
        {SEASON: "6", EPISODE: ""},
        {},
    ],
    ids=["no episode", "no season", "not a number", "empty", "nothing"],
)
def test_half_a_numbering_identifies_nothing(additional: dict[str, Any]) -> None:
    assert numbering(archive(additional)) is None


def test_an_archive_without_the_dictionary_is_not_an_episode() -> None:
    assert numbering(archive(None)) is None


def test_a_root_that_is_not_a_dictionary_is_not_an_episode() -> None:
    assert numbering(plist(["$null", "not a dictionary"])) is None


@pytest.mark.parametrize(
    "blob",
    [None, b"", b"bplist00 but not really", b"\x00\x01\x02", "not bytes at all"],
    ids=["none", "empty", "truncated", "binary noise", "wrong type"],
)
def test_what_is_not_an_archive_is_not_an_episode(blob: Any) -> None:
    """protobuf hands back b"" for an unset field, and a moved seam could hand
    back anything; neither may raise inside a state write."""
    assert numbering(blob) is None


def test_a_self_referential_archive_ends() -> None:
    """A UID that points at its own container would otherwise recurse forever,
    inside the entity's state write."""
    loop = {"NS.keys": [plistlib.UID(1)], "NS.objects": [plistlib.UID(1)]}

    assert numbering(plist(["$null", loop])) is None
