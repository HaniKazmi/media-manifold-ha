"""Reading an Apple TV state as an episode, and as a scrobble.

Everything here is a plain object with `state` and `attributes`, which is the
point of keeping these rules out of Home Assistant: the television's own
vocabulary is what is under test, not Home Assistant's.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.manifold.simkl.playing import Episode, episode, events, progress

NOW = datetime(2026, 8, 26, 22, 20, 0, tzinfo=timezone.utc)

# Black Bird, season 1, episode 4, as the Apple TV app numbers it.
BLACK_BIRD = "A0054401004"


def state(
    value: str = "playing",
    *,
    content_id: str | None = BLACK_BIRD,
    title: str | None = "Black Bird",
    app: str | None = "com.apple.TVWatchList",
    duration: int | None = 3600,
    position: int | None = None,
    updated: datetime | None = None,
    season: str | None = None,
    number: str | None = None,
):
    """One media player state, carrying only what the television reports.

    `season` and `number` are what the numbering graft writes, as the strings
    Home Assistant carries them.
    """
    attributes = {}
    for key, attribute in (
        ("app_id", app),
        ("media_content_id", content_id),
        ("media_title", title),
        ("media_duration", duration),
        ("media_position", position),
        ("media_position_updated_at", updated),
        ("media_season", season),
        ("media_episode", number),
    ):
        if attribute is not None:
            attributes[key] = attribute
    return SimpleNamespace(state=value, attributes=attributes)


def test_the_content_id_carries_the_numbering() -> None:
    """Without the graft's attributes it is the only place the numbering exists:
    pyatv reports no season or episode for this app."""
    assert episode(state()) == Episode("Black Bird", 1, 4)


@pytest.mark.parametrize(
    ("content_id", "season", "number"),
    [("A0026503008", 3, 8), ("A0007105008", 5, 8), ("A0006504003", 4, 3)],
)
def test_the_numbering_is_read_by_position(content_id, season, number) -> None:
    """Two digits of season and three of episode, regardless of the show."""
    assert episode(state(content_id=content_id))[1:] == (season, number)


@pytest.mark.parametrize("content_id", ["A005440100", "A00544010040", "0054401004", ""])
def test_a_content_id_of_another_shape_is_not_an_episode(content_id) -> None:
    """Length is the whole of the check, so a near miss must not be sliced."""
    assert episode(state(content_id=content_id)) is None


def test_apple_music_on_the_television_is_not_an_episode() -> None:
    """It shares the com.apple.TV prefix and reports no content id."""
    assert episode(state(app="com.apple.TVMusic", content_id=None)) is None


def test_infuse_is_left_to_the_jellyfin_webhook() -> None:
    """A second reporter would race the webhook for the same episode."""
    assert episode(state(app="com.firecore.infuse", content_id=None)) is None


def test_a_state_with_no_title_names_no_show() -> None:
    """The title is what SIMKL is searched by; without it there is no request."""
    assert episode(state(title=None)) is None


def test_nothing_playing_is_not_an_episode() -> None:
    assert episode(None) is None


def test_progress_counts_on_from_the_last_reported_position() -> None:
    """The television reports a position once and then stops mentioning it."""
    playing = state(position=1740, updated=NOW - timedelta(seconds=60))
    assert progress(playing, NOW) == 50.0


def test_a_paused_position_is_taken_as_it_stands() -> None:
    """Nothing has advanced since, so adding the wait would overstate it."""
    paused = state("paused", position=1200, updated=NOW - timedelta(seconds=600))
    assert progress(paused, NOW) == pytest.approx(33.33)


def test_progress_stops_at_a_hundred() -> None:
    """A few seconds past the end is still the end, not 101%."""
    over = state(position=3600, updated=NOW - timedelta(seconds=60))
    assert progress(over, NOW) == 100.0


def test_a_position_carried_too_far_is_unknown_rather_than_complete() -> None:
    """Extrapolating across hours reaches the end of anything.

    A television that goes quiet mid-episode keeps its last `playing` state, so
    the arithmetic alone would report a confident 100% and SIMKL would file an
    episode nobody finished as watched.
    """
    stale = state(position=300, updated=NOW - timedelta(hours=2))
    assert progress(stale, NOW) is None


def test_a_paused_position_is_not_aged_out() -> None:
    """Nothing advances while paused, so an old reading is still the truth."""
    paused = state("paused", position=3300, updated=NOW - timedelta(hours=2))
    assert progress(paused, NOW) == pytest.approx(91.67)


def test_a_stale_episode_is_not_reported_as_finished() -> None:
    """The whole point of the bound: no progress, so no stop is sent."""
    stale = state(position=300, updated=NOW - timedelta(hours=2))
    assert events(stale, state("idle", content_id=None, title=None), NOW) == []


@pytest.mark.parametrize("gone", ["unavailable", "unknown"])
def test_a_player_that_goes_quiet_says_nothing_about_the_episode(gone) -> None:
    """These carry no attributes, so any verdict would describe an earlier moment."""
    playing = state(position=3550, updated=NOW)
    assert events(playing, state(gone, content_id=None, title=None), NOW) == []


def test_an_app_that_is_not_the_apple_tv_app_is_not_an_episode() -> None:
    """The content-id shape alone is not the gate; the app has to match too."""
    assert episode(state(app="com.netflix.Netflix")) is None


def test_progress_without_a_duration_is_unknown_rather_than_zero() -> None:
    """Zero is a claim about where the viewer stopped, and it would be wrong."""
    assert progress(state(duration=None), NOW) is None


def test_pressing_play_starts_a_scrobble() -> None:
    scrobbles = events(state("idle", content_id=None, title=None), state(), NOW)
    assert [(e.act, e.episode) for e in scrobbles] == [
        ("start", Episode("Black Bird", 1, 4))
    ]


def test_pausing_reports_the_position_it_paused_at() -> None:
    paused = state("paused", position=1800, updated=NOW)
    scrobbles = events(state(position=1800, updated=NOW), paused, NOW)
    assert [(e.act, e.progress) for e in scrobbles] == [("pause", 50.0)]


def test_stopping_reports_the_progress_of_what_was_playing() -> None:
    """The new state describes a different moment, or nothing at all."""
    was = state(position=3400, updated=NOW)
    scrobbles = events(was, state("idle", content_id=None, title=None), NOW)
    assert [(e.act, e.progress) for e in scrobbles] == [("stop", pytest.approx(94.44))]


def test_playing_on_into_the_next_episode_ends_the_first_one() -> None:
    """A start alone would leave the finished episode short of being watched."""
    scrobbles = events(
        state(position=3500, updated=NOW),
        state(content_id="A0054401005", position=0, updated=NOW),
        NOW,
    )

    assert [(e.act, e.episode.number) for e in scrobbles] == [("stop", 4), ("start", 5)]


def test_the_ending_episode_is_reported_before_the_starting_one() -> None:
    """SIMKL keeps one session per user, and the last one sent is the one kept."""
    scrobbles = events(
        state(position=3500, updated=NOW),
        state(content_id="A0054401005", position=0, updated=NOW),
        NOW,
    )
    assert scrobbles[0].act == "stop"


def test_a_pause_that_cannot_be_measured_is_not_sent() -> None:
    """It would write a position of zero over a real resume point."""
    playing = state(duration=None)
    assert events(playing, state("paused", duration=None), NOW) == []


def test_a_start_still_goes_without_a_duration() -> None:
    """Start carries no position, so there is nothing to get wrong."""
    idle = state("idle", content_id=None, title=None)
    scrobbles = events(idle, state(duration=None), NOW)
    assert [(e.act, e.progress) for e in scrobbles] == [("start", None)]


def test_an_unrelated_attribute_change_asks_for_nothing() -> None:
    """The television republishes its state constantly; only changes matter."""
    playing = state(position=1200, updated=NOW)
    assert events(playing, playing, NOW) == []


def test_a_change_while_already_paused_reports_nothing() -> None:
    """Only the moment of pausing is a pause; the state then repeats itself."""
    paused = state("paused", position=1800, updated=NOW)
    assert events(paused, state("paused", position=1800, updated=NOW), NOW) == []


def test_arriving_at_paused_from_a_standstill_is_not_a_pause() -> None:
    """Nothing was playing, so nothing was paused."""
    idle = state("idle", content_id=None, title=None)
    assert events(idle, state("paused", position=1800, updated=NOW), NOW) == []


def test_the_numbering_the_graft_wrote_is_preferred() -> None:
    """Slow Horses season 6 arrives as A0006403008: the id counts production
    blocks and the show is shot two seasons at a time, so the slice says season
    3, episode 8, an episode SIMKL has no record of. The graft's attributes say
    what the iOS Remote says."""
    playing = state(content_id="A0006403008", title="Slow Horses", season="6", number="2")

    assert episode(playing) == Episode("Slow Horses", 6, 2)


@pytest.mark.parametrize(
    ("season", "number"),
    [("6", None), (None, "2"), ("six", "2")],
    ids=["no episode", "no season", "not a number"],
)
def test_half_a_numbering_falls_back_to_the_id(season, number) -> None:
    assert episode(state(season=season, number=number)) == Episode("Black Bird", 1, 4)


def test_the_numbering_alone_is_not_enough_without_the_app() -> None:
    """Infuse could report a season and an episode of its own; a Jellyfin
    webhook already scrobbles that side."""
    playing = state(app="com.firecore.infuse", season="6", number="2")

    assert episode(playing) is None


def test_the_numbering_alone_is_not_enough_without_the_id() -> None:
    """The id is what marks the item as one of the app's own episodes, whose
    title is the show. Something else the app plays could carry a season and
    an episode beside a title naming an episode, and SIMKL would be searched
    for that."""
    playing = state(content_id=None, title="Daddy Issues", season="6", number="2")

    assert episode(playing) is None
