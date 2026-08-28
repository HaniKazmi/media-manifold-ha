"""What an Apple TV media player state says about an episode.

The native Apple TV app names what it is playing in two halves. ``media_title``
carries the show, and the numbering is inside ``media_content_id``:

    A 00544 01 004   ->  Black Bird, season 1, episode 4
    │ │     │  └── episode, three digits
    │ │     └───── season, two digits
    │ └─────────── a per-show prefix, stable across sessions
    └───────────── literal A

pyatv reports none of ``media_series_title``, ``media_season`` or
``media_episode`` for this app, so the id is the only place the numbering exists.

That eleven-character shape is also the gate. Apple Music on the same television
(``com.apple.TVMusic``) and Infuse (``com.firecore.infuse``) report no content id
at all, so requiring one keeps both out — Infuse deliberately, since a Jellyfin
webhook scrobbles that side and a second reporter would race it.

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

    return Episode(show, int(content_id[6:8]), int(content_id[8:11]))


def progress(state: State, now: datetime) -> float | None:
    """How far through the episode a state is, as a percentage.

    None when the duration is missing, which is what keeps a `pause` or `stop`
    from being sent at all: both write the number they carry into SIMKL's saved
    position, so reporting 0 would replace a real resume point with a place
    nobody watched to.
    """
    duration = float(state.attributes.get("media_duration") or 0)
    if duration <= 0:
        return None

    position = float(state.attributes.get("media_position") or 0)
    updated = state.attributes.get("media_position_updated_at")
    # The television reports a position and then stops mentioning it, so while
    # playing the true position is that one plus the time since it was given.
    if state.state == PLAYING and updated is not None:
        position += (now - updated).total_seconds()

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
