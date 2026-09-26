"""What an Apple TV media player state says about an episode.

The native Apple TV app names what it is playing in two halves. ``media_title``
carries the show, and the numbering arrives one of two ways.

The first is ``media_season`` and ``media_episode``, filled in by this
integration's own graft on the Apple TV entity from the app's now-playing
archive (see ``appletv/nowplaying.py``). These are the public numbers, the ones
the iOS Remote shows, and they are preferred whenever both are present.

The second is inside ``media_content_id``::

    A 00544 01 004   ->  Black Bird, season 1, episode 4
    │ │     │  └── episode, three digits
    │ │     └───── season, two digits
    │ └─────────── a per-show prefix, stable across sessions
    └───────────── literal A

That slice is right for a show shot one season at a time and wrong for one shot
in blocks: Slow Horses season 6 arrives as ``A0006403008``, block 3 and its
eighth episode, which the slice reads as season 3, episode 8. It is the
fallback for a state the graft could not fill — the graft declined to install,
or the app played something without the archive.

The eleven-character id is the gate for both routes, whichever supplies the
numbers. It is what marks the item as one of the app's own episodes, whose
``media_title`` is the show; anything the app plays without such an id could
carry a season and an episode beside a title that names something else, and
SIMKL is searched by that title. Apple Music on the same television
(``com.apple.TVMusic``) shares the ``com.apple.TV`` prefix but reports no id of
that shape, and Infuse (``com.firecore.infuse``) fails the prefix outright —
deliberately, since a Jellyfin webhook scrobbles that side and a second
reporter would race it.

This module imports nothing from Home Assistant: every rule is checkable against
a plain object carrying ``state`` and ``attributes``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re
from typing import Any, NamedTuple, Protocol

# The literal A and ten digits, and nothing longer: the length is what separates
# an episode id from anything else the television might report.
_CONTENT_ID = re.compile(r"^A\d{10}$")

_APP_PREFIX = "com.apple.TV"

PLAYING = "playing"
PAUSED = "paused"
_ACTIVE = frozenset({PLAYING, PAUSED})

# States that say the player stopped answering rather than that playback ended.
# They carry no attributes at all, so anything said about the episode across one
# of them is said about a moment before the television went quiet.
_LOST = frozenset({"unavailable", "unknown"})

# How far a reported position may be carried forward before it stops being a
# measurement. The television pushes a position every few seconds while it
# plays, so a gap this long means the connection went away mid-episode and the
# arithmetic below is extrapolating across time nobody was watching.
_STALE_AFTER = 300


class State(Protocol):
    """The shape of a Home Assistant state, without importing one."""

    state: str
    attributes: Mapping[str, Any]


class Episode(NamedTuple):
    """A show, and which episode of it is playing."""

    show: str
    season: int
    number: int


class Event(NamedTuple):
    """One scrobble to send. ``progress`` is omitted from the request when None."""

    act: str
    episode: Episode
    progress: float | None


def episode(state: State | None) -> Episode | None:
    """The episode a state describes, or None when it describes anything else."""
    if state is None:
        return None

    attributes = state.attributes
    app = attributes.get("app_id") or ""
    content_id = attributes.get("media_content_id") or ""
    if not app.startswith(_APP_PREFIX) or not _CONTENT_ID.match(content_id):
        return None

    # The show has to come from the title; nothing else in the state names it,
    # and SIMKL is searched by that name.
    if not (show := attributes.get("media_title")):
        return None

    season, number = _from_attributes(attributes) or (
        int(content_id[6:8]),
        int(content_id[8:11]),
    )
    return Episode(show, season, number)


def _from_attributes(attributes: Mapping[str, Any]) -> tuple[int, int] | None:
    """The numbering the graft wrote, which Home Assistant carries as strings."""
    try:
        return int(attributes.get("media_season")), int(attributes.get("media_episode"))
    except (TypeError, ValueError):
        # Absent, or not a number: both mean the graft wrote nothing usable.
        return None


def progress(state: State, now: datetime) -> float | None:
    """How far through the episode a state is, as a percentage.

    None means unknown, and an event that cannot say where the viewer got to is
    not sent at all. Both `pause` and `stop` write the number they carry into
    SIMKL's saved position, so a guess there replaces a real resume point with a
    place nobody watched to.
    """
    duration = float(state.attributes.get("media_duration") or 0)
    if duration <= 0:
        return None

    position = float(state.attributes.get("media_position") or 0)
    updated = state.attributes.get("media_position_updated_at")
    # The television reports a position and then stops mentioning it, so while
    # playing the true position is that one plus the time since it was given.
    if state.state == PLAYING and updated is not None:
        elapsed = (now - updated).total_seconds()
        # Past this the state is a leftover, not a measurement. Carrying it
        # forward regardless reaches the end of any episode given enough hours
        # and reports a confident 100%, which SIMKL files as watched — a verdict
        # about an episode the television stopped describing long before.
        if elapsed > _STALE_AFTER:
            return None
        position += elapsed

    return round(min(position / duration * 100, 100), 2)


def events(old: State | None, new: State | None, now: datetime) -> list[Event]:
    """The scrobbles one state change asks for, in the order to send them.

    A change can ask for two. Playing on into the next episode ends one and
    begins another in a single step, and SIMKL is told about both — a `start`
    alone would leave the finished episode short of the 80% that marks it
    watched.
    """
    was, is_ = episode(old), episode(new)
    was_state = old.state if old is not None else None
    is_state = new.state if new is not None else None

    # A player that has gone quiet has not said anything about the episode, so
    # nothing is said about it either. What it was doing is still true, and the
    # transition that eventually describes the end can report it.
    if is_state in _LOST:
        return []

    out: list[Event | None] = []

    # Ended: something that was on screen is not, either replaced by another
    # episode or left behind entirely. Its progress comes from the state it had,
    # since the new one describes a different moment.
    left = was != is_ or is_state not in _ACTIVE
    if was is not None and was_state in _ACTIVE and left:
        out.append(_event("stop", was, old, now))

    if is_ is not None:
        if is_state == PLAYING and (was_state != PLAYING or was != is_):
            out.append(_event("start", is_, new, now))
        elif is_state == PAUSED and was_state == PLAYING and was == is_:
            out.append(_event("pause", is_, new, now))

    return [event for event in out if event is not None]


def _event(act: str, of: Episode, state: State, now: datetime) -> Event | None:
    """One event, or None when it carries a progress it cannot know."""
    percent = progress(state, now)
    if percent is None and act != "start":
        return None
    return Event(act, of, percent)
