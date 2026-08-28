"""Golden-fixture tests for the Sonos items this integration builds.

The expected strings are the ones a live household accepted and played. A diff
here means the speaker would reject the item, or accept it and then silently
refuse to start — a failure invisible from Home Assistant, since the enqueue
succeeds and nothing happens. They are pinned as literals rather than rebuilt
from the templates the code uses, so a template edit cannot pass its own
mistake.
"""

from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerEnqueue
import pytest
from soco.data_structures import to_didl_string

from custom_components.sonos_apple_music.applemusic.const import CDUDN, SONOS_SERVICE_TYPE
from custom_components.sonos_apple_music.applemusic.play import (
    build_container,
    build_station,
    build_track,
    discover_sn,
    enqueue,
)

from .conftest import (
    ALBUM_ID,
    APPLE_FAVORITE,
    PLAYLIST_ID,
    SONG_ID,
    STATION_ID,
    FakeFavorite,
    FakeSoco,
)


def test_service_type_derives_from_sid() -> None:
    """52231 is what the household's own favorites carry in their cdudn."""
    assert SONOS_SERVICE_TYPE == 52231
    assert CDUDN == "SA_RINCON52231_X_#Svc52231-0-Token"


def test_track_uri_matches_what_played() -> None:
    track = build_track(song_id=SONG_ID, album_id=ALBUM_ID, sn=2, title="Creep")
    assert track.resources[0].uri == (
        f"x-sonosapi-hls-static:song%3a{SONG_ID}?sid=204&flags=8232&sn=2"
    )
    assert track.resources[0].protocol_info == "x-rincon-playlist:*:*:*"


def test_track_ids_use_the_prefixes_sonos_accepts() -> None:
    """The 1…-prefixed variants enqueue cleanly and then never play."""
    track = build_track(song_id=SONG_ID, album_id=ALBUM_ID, sn=2)
    assert track.item_id == f"00032020song%3a{SONG_ID}"
    assert track.parent_id == f"0004206calbum%3a{ALBUM_ID}"


def test_track_didl_carries_the_account_binding() -> None:
    """Without the cdudn the speaker cannot resolve the Apple Music account."""
    didl = to_didl_string(build_track(SONG_ID, ALBUM_ID, sn=2, title="Creep"))
    assert '<desc id="cdudn"' in didl
    assert CDUDN in didl
    # Sonos writes these as <item>; a <container> is rejected.
    assert "<item " in didl
    assert "<container" not in didl


@pytest.mark.parametrize(
    ("kind", "apple_id", "expected"),
    [
        ("album", ALBUM_ID, f"1004206calbum%3A{ALBUM_ID}"),
        ("playlist", PLAYLIST_ID, f"1006206cplaylist%3A{PLAYLIST_ID}"),
    ],
)
def test_container_uri(kind: str, apple_id: str, expected: str) -> None:
    container = build_container(kind, apple_id, sn=2)
    assert container.item_id == expected
    assert container.resources[0].uri == (
        f"x-rincon-cpcontainer:{expected}?sid=204&flags=8300&sn=2"
    )


def test_container_parent_mirrors_item() -> None:
    """Matches what the Sonos app writes into its own favorites."""
    container = build_container("album", ALBUM_ID, sn=2)
    assert container.parent_id == container.item_id


def test_artist_is_not_a_container() -> None:
    """Sonos has no 'play this artist' URI, so this must fail loudly."""
    with pytest.raises(ValueError, match="container kind"):
        build_container("artist", "657515", sn=2)


def test_station_uri_matches_what_sonos_set() -> None:
    """flags=0 here, not the 8232/8300 used for tracks and containers."""
    station = build_station(STATION_ID, sn=2, title="HUNTR/X & Similar Artists")
    assert station.resources[0].uri == (
        f"x-sonosapi-radio:radio%3a{STATION_ID}?sid=204&flags=0&sn=2"
    )


def test_station_didl_matches_what_sonos_wrote() -> None:
    station = build_station(STATION_ID, sn=2, title="HUNTR/X & Similar Artists")
    assert station.item_id == f"000c0000radio%3a{STATION_ID}"
    assert station.parent_id == "-1"
    assert station.item_class == "object.item.audioItem.audioBroadcast"
    assert station.desc == CDUDN


def test_replace_clears_then_plays_from_the_top() -> None:
    """A container expands into several entries, so REPLACE starts at 0.

    Starting at the position add_to_queue reports would begin partway through an
    album.
    """
    soco = FakeSoco()
    album = build_container("album", ALBUM_ID, sn=2)
    enqueue(soco, album, MediaPlayerEnqueue.REPLACE, 7, 30.0)
    assert soco.names == ["clear_queue", "add_to_queue", "play_from_queue"]
    assert soco.calls[-1][1]["index"] == 0


def test_add_appends_without_touching_playback() -> None:
    soco = FakeSoco()
    enqueue(soco, build_track(SONG_ID, ALBUM_ID, sn=2), MediaPlayerEnqueue.ADD, 7, 30.0)
    assert soco.names == ["add_to_queue"]
    assert soco.calls[0][1]["position"] == 0


def test_next_inserts_after_the_current_track() -> None:
    soco = FakeSoco()
    enqueue(soco, build_track(SONG_ID, ALBUM_ID, sn=2), MediaPlayerEnqueue.NEXT, 7, 30.0)
    assert soco.names == ["add_to_queue"]
    assert soco.calls[0][1]["position"] == 8


def test_play_inserts_next_then_jumps_to_it() -> None:
    soco = FakeSoco()
    enqueue(soco, build_track(SONG_ID, ALBUM_ID, sn=2), MediaPlayerEnqueue.PLAY, 7, 30.0)
    assert soco.names == ["add_to_queue", "play_from_queue"]
    # play_from_queue is 0-based, add_to_queue's return is 1-based.
    assert soco.calls[-1][1]["index"] == soco.next_position - 1


def test_an_empty_queue_position_is_treated_as_the_start() -> None:
    """`media.queue_position` is None before anything has played."""
    soco = FakeSoco()
    track = build_track(SONG_ID, ALBUM_ID, sn=2)
    enqueue(soco, track, MediaPlayerEnqueue.NEXT, None, 30.0)
    assert soco.calls[0][1]["position"] == 1


def test_station_plays_rather_than_queues() -> None:
    """Radio has no queue entries to sit beside, so it replaces the transport.

    Queueing a station would either fault or bury a stream mid-tracklist.
    """
    soco = FakeSoco()
    enqueue(soco, build_station(STATION_ID, sn=2), MediaPlayerEnqueue.REPLACE, None, 30.0)
    assert soco.names == ["play_uri"]
    assert soco.calls[0][1]["uri"].startswith("x-sonosapi-radio:")
    assert "<desc" in soco.calls[0][1]["meta"]


@pytest.mark.parametrize(
    "mode", [MediaPlayerEnqueue.ADD, MediaPlayerEnqueue.NEXT, MediaPlayerEnqueue.PLAY]
)
def test_station_ignores_enqueue_modes(mode: MediaPlayerEnqueue) -> None:
    soco = FakeSoco()
    enqueue(soco, build_station(STATION_ID, sn=2), mode, None, 30.0)
    assert soco.names == ["play_uri"]


def test_discover_sn_reads_the_account_serial() -> None:
    assert discover_sn([FakeFavorite(APPLE_FAVORITE)]) == 2


def test_discover_sn_ignores_other_services() -> None:
    """A Spotify favorite's serial is not Apple Music's."""
    spotify = "x-sonos-http:tr%3a1.mp3?sid=9&flags=8224&sn=7"
    assert discover_sn([FakeFavorite(spotify)]) is None


def test_discover_sn_survives_a_favorite_without_resources() -> None:
    """soco 0.31.1 lets errors escape DidlFavorite.reference, so we read `resources`.

    One malformed favorite must not end the scan for all the others.
    """
    assert discover_sn([FakeFavorite(None), FakeFavorite(APPLE_FAVORITE)]) == 2


@pytest.mark.parametrize("favorites", [[], None])
def test_discover_sn_reports_that_it_found_nothing(favorites: list | None) -> None:
    """A read serial and a guessed one fail differently, so they stay apart.

    Substituting the fallback here would leave the caller unable to tell the
    household apart from one whose serial really is the fallback.
    """
    assert discover_sn(favorites) is None
