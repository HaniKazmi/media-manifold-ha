"""Infuse URL shapes for Jellyfin items.

Pure data in, string out. The shapes are fixed by Infuse's third-party API, so
these tests are the record of what that API says; a change here means Firecore
changed something, not that a refactor moved.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.sonos_apple_music.infuse.link import (
    TICKS_PER_SECOND,
    deep_link,
    direct_play,
    display_name,
    is_video,
    resume_position,
    series_id,
    tmdb_id,
)

SERIES_TMDB = "1396"


def item(item_type: str, tmdb: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"Type": item_type, "MediaType": "Video", **extra}
    if tmdb is not None:
        body["ProviderIds"] = {"Tmdb": tmdb}
    return body


def test_only_video_belongs_to_infuse() -> None:
    """The Apple TV plays Jellyfin audio over RAOP already, and a track handed to
    Infuse opens the app on nothing."""
    assert is_video(item("Movie", "27205")) is True
    assert is_video({"Type": "Audio", "MediaType": "Audio"}) is False
    assert is_video({"Type": "Movie"}) is False


def test_a_name_is_read_where_jellyfin_keeps_it() -> None:
    assert display_name({"Name": "Inception"}) == "Inception"
    assert display_name({}) is None


def test_a_movie_links_by_tmdb_id() -> None:
    assert deep_link(item("Movie", "27205")) == "infuse://movie/27205?play"


def test_a_series_links_by_tmdb_id() -> None:
    assert deep_link(item("Series", SERIES_TMDB)) == f"infuse://series/{SERIES_TMDB}?play"


def test_a_season_appends_its_number_to_the_series() -> None:
    season = item("Season", IndexNumber=2, SeriesId="abc")
    assert deep_link(season, SERIES_TMDB) == f"infuse://series/{SERIES_TMDB}-2?play"


def test_an_episode_appends_season_then_episode() -> None:
    episode = item("Episode", ParentIndexNumber=4, IndexNumber=7, SeriesId="abc")
    assert deep_link(episode, SERIES_TMDB) == f"infuse://series/{SERIES_TMDB}-4-7?play"


def test_an_episode_carries_its_parent_rather_than_its_own_tmdb_id() -> None:
    """Jellyfin gives episodes their own TMDB ids, and Infuse addresses none of
    them: a season and episode number are only meaningful against the series."""
    episode = item(
        "Episode", "62085", ParentIndexNumber=4, IndexNumber=7, SeriesId="abc"
    )
    assert deep_link(episode, SERIES_TMDB) == f"infuse://series/{SERIES_TMDB}-4-7?play"


@pytest.mark.parametrize(
    "media",
    [
        item("Movie"),
        item("Series"),
        item("Episode", ParentIndexNumber=1, IndexNumber=1),
        item("MusicAlbum", "27205"),
    ],
    ids=["movie", "series", "episode-without-series", "unknown-type"],
)
def test_an_item_infuse_cannot_name_has_no_deep_link(media: dict[str, Any]) -> None:
    """None rather than a raise: an unmatched file is routine, and it still has
    a stream URL to fall back to."""
    assert deep_link(media) is None


def test_a_season_with_no_number_has_no_deep_link() -> None:
    """Specials and extras carry no IndexNumber, and `series-None` addresses
    nothing."""
    assert deep_link(item("Season", SeriesId="abc"), SERIES_TMDB) is None


def test_only_children_report_a_series_to_look_up() -> None:
    assert series_id(item("Episode", SeriesId="abc")) == "abc"
    assert series_id(item("Season", SeriesId="abc")) == "abc"
    assert series_id(item("Movie", "27205")) is None


def test_a_missing_provider_block_reads_as_no_id() -> None:
    assert tmdb_id({"Type": "Movie"}) is None
    assert tmdb_id({"Type": "Movie", "ProviderIds": None}) is None


def test_direct_play_encodes_the_stream_url_whole() -> None:
    """The value is itself a URL with its own query string, so its separators
    have to survive as data rather than being read as this URL's."""
    url = direct_play("http://jelly:8096/Videos/1/stream?static=true&api_key=k")

    assert url.startswith("infuse://x-callback-url/play?url=")
    assert "http%3A%2F%2Fjelly%3A8096" in url
    assert "static%3Dtrue%26api_key%3Dk" in url


def test_direct_play_spaces_survive_as_percent_twenty() -> None:
    """A `+` would be read back as a literal plus by a strict unquote, and
    filenames are full of spaces."""
    assert "Blade%20Runner" in direct_play("http://x/1", filename="Blade Runner")


def test_direct_play_carries_a_resume_point() -> None:
    assert "position=90" in direct_play("http://x/1", position=90)


def test_direct_play_omits_what_it_was_not_given() -> None:
    assert direct_play("http://x/1") == "infuse://x-callback-url/play?url=http%3A%2F%2Fx%2F1"


def test_a_resume_point_is_whole_seconds() -> None:
    watched = {"UserData": {"PlaybackPositionTicks": 90 * TICKS_PER_SECOND}}
    assert resume_position(watched) == 90


@pytest.mark.parametrize(
    "media",
    [
        {},
        {"UserData": None},
        {"UserData": {"PlaybackPositionTicks": 0}},
        {"UserData": {"PlaybackPositionTicks": TICKS_PER_SECOND - 1}},
    ],
    ids=["never-played", "null-userdata", "zero", "under-a-second"],
)
def test_nothing_worth_resuming_reads_as_the_start(media: dict[str, Any]) -> None:
    """Under a second is a film opened and closed, and seeking there costs a
    parameter that does nothing."""
    assert resume_position(media) is None
