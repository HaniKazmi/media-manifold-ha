"""Turning a browse content id into something Sonos will play."""

from __future__ import annotations

import pytest

from custom_components.sonos_apple_music.applemusic.const import URI_PREFIX, is_library_id
from custom_components.sonos_apple_music.applemusic.resolve import (
    NotPlayable,
    async_build_item,
)

from .conftest import ALBUM_ID, ARTIST_ID, PLAYLIST_ID, SONG_ID, STATION_ID, song


@pytest.mark.parametrize(
    ("value", "library"),
    [
        ("i.mmpeNQEtbP3ozq", True),
        ("l.Ye8TlCg", True),
        ("r.DVITsJu", True),
        ("p.rXAJKVahZNRX4g", True),
        ("pl.u-06oxDj6to1qMmx", False),
        (SONG_ID, False),
        (PLAYLIST_ID, False),
        (STATION_ID, False),
    ],
)
def test_library_ids_are_distinguished_from_catalog_ids(
    value: str, library: bool
) -> None:
    """Sonos rejects library ids outright, so the two must not be confused.

    The ids are real ones from a live account. `p.` is a library playlist while
    `pl.u-` is the catalog globalId that same playlist carries — reading `pl.u-`
    as library sends a translated id back to the endpoint it came from, and
    reading `p.` as catalog 404s every library playlist.
    """
    assert is_library_id(value) is library


async def test_a_song_resolves_with_its_album(client) -> None:
    """The DIDL parent must be a real album id.

    A placeholder parent is accepted into the queue and then silently refuses to
    play, so this is the difference between working and a dead queue entry.
    """
    item = await async_build_item(client, f"{URI_PREFIX}song/{SONG_ID}", sn=2)
    assert item.item_id == f"00032020song%3a{SONG_ID}"
    assert item.parent_id == f"0004206calbum%3a{ALBUM_ID}"


async def test_a_song_with_no_catalog_album_is_refused(client) -> None:
    """Better a clear error than an item that enqueues and never starts."""
    orphan = song()
    orphan["relationships"] = {"albums": {"data": []}}
    client.responses[f"catalog/gb/songs/{SONG_ID}"] = {"data": [orphan]}

    with pytest.raises(NotPlayable, match="no catalog album"):
        await async_build_item(client, f"{URI_PREFIX}song/{SONG_ID}", sn=2)


async def test_a_library_song_resolves_through_play_params(client) -> None:
    """Apple hands over the catalog id directly when it has one."""
    client.responses["me/library/songs/i.abc"] = {
        "data": [{"attributes": {"name": "Creep", "playParams": {"catalogId": SONG_ID}}}]
    }
    item = await async_build_item(client, f"{URI_PREFIX}song/i.abc", sn=2)
    assert item.item_id == f"00032020song%3a{SONG_ID}"


async def test_a_library_song_falls_back_to_search(client) -> None:
    """Some library entries carry no catalog id even when a match exists."""
    client.responses["me/library/songs/i.abc"] = {
        "data": [{"attributes": {"name": "Creep", "artistName": "Radiohead"}}]
    }
    item = await async_build_item(client, f"{URI_PREFIX}song/i.abc", sn=2)
    assert item.item_id == f"00032020song%3a{SONG_ID}"


async def test_a_song_with_no_artist_searches_on_its_title_alone(client) -> None:
    """Interpolating a missing artistName searches for the word "None", which
    misses the very tracks that reach this fallback."""
    client.responses["me/library/songs/i.abc"] = {
        "data": [{"attributes": {"name": "Creep"}}]
    }
    await async_build_item(client, f"{URI_PREFIX}song/i.abc", sn=2)

    term = next(p["term"] for e, p in client.requested if e.endswith("search"))
    assert term == "Creep"


async def test_a_personal_upload_is_refused_with_an_explanation(client) -> None:
    """Uploads have no catalog equivalent; Sonos can never stream them."""
    client.responses["me/library/songs/i.abc"] = {
        "data": [{"attributes": {"name": "Voice Memo", "artistName": "Me"}}]
    }
    client.responses["catalog/gb/search"] = {"results": {"songs": {"data": []}}}

    with pytest.raises(NotPlayable, match="not in the Apple Music catalog"):
        await async_build_item(client, f"{URI_PREFIX}song/i.abc", sn=2)


async def test_a_catalog_album_becomes_a_container(client) -> None:
    item = await async_build_item(client, f"{URI_PREFIX}album/{ALBUM_ID}", sn=2)
    assert item.item_id == f"1004206calbum%3A{ALBUM_ID}"


async def test_a_library_album_resolves_to_its_catalog_id(client) -> None:
    client.responses["me/library/albums/l.abc"] = {
        "data": [
            {"attributes": {"name": "Pablo Honey", "playParams": {"globalId": ALBUM_ID}}}
        ]
    }
    item = await async_build_item(client, f"{URI_PREFIX}album/l.abc", sn=2)
    assert item.item_id == f"1004206calbum%3A{ALBUM_ID}"


async def test_a_library_only_playlist_is_refused_with_a_way_forward(client) -> None:
    """A playlist that exists only in the library has no container Sonos can take."""
    client.responses["me/library/playlists/p.mZUbP3ozq"] = {
        "data": [{"attributes": {"name": "My Mix"}}]
    }
    with pytest.raises(NotPlayable, match="play its tracks individually"):
        await async_build_item(client, f"{URI_PREFIX}playlist/p.mZUbP3ozq", sn=2)


async def test_a_station_needs_no_translation(client) -> None:
    """The `ra.` id is already what Sonos wants."""
    item = await async_build_item(client, f"{URI_PREFIX}station/{STATION_ID}", sn=2)
    assert item.item_id == f"000c0000radio%3a{STATION_ID}"


async def test_a_station_plays_even_if_its_title_cannot_be_read(client) -> None:
    """The title is cosmetic; failing to fetch it must not block playback."""
    from custom_components.sonos_apple_music.applemusic.api import AppleMusicError

    client.responses[f"catalog/gb/stations/{STATION_ID}"] = AppleMusicError("nope", 500)
    item = await async_build_item(client, f"{URI_PREFIX}station/{STATION_ID}", sn=2)
    assert item.item_id == f"000c0000radio%3a{STATION_ID}"


async def test_an_artist_is_not_playable(client) -> None:
    """Sonos has no URI meaning 'this artist'."""
    with pytest.raises(NotPlayable):
        await async_build_item(client, f"{URI_PREFIX}artist/{ARTIST_ID}", sn=2)
