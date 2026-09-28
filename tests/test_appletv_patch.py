"""Grafting the app's real episode numbering onto the Apple TV entity.

The entity under test is a real ``AppleTvMediaPlayer``, so the wrapped
properties call upstream's own accessors first: an upstream change to how
``media_season`` is gated fails here, not in someone's living room. What stands
in is the pyatv object, built to the three-link shape the graft reads through.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from homeassistant.components.apple_tv import media_player as apple_media_player
from homeassistant.components.apple_tv.media_player import AppleTvMediaPlayer
from pyatv.const import DeviceState, FeatureName, MediaType, Protocol
from pyatv.interface import Playing
import pytest

from custom_components.manifold.appletv.patch import async_install, async_remove

from .conftest import SLOW_HORSES_ARCHIVE

# pyatv's own answer for a show it numbers itself, and the features it marks
# available when it does.
NUMBERED = {FeatureName.SeasonNumber, FeatureName.EpisodeNumber}


def playing(blob: bytes | None = SLOW_HORSES_ARCHIVE) -> Any:
    """An MRP metadata instance whose player state describes one item.

    `metadata_field` is pyatv's accessor, and None is what it answers for an
    empty queue and for a field the app never set.
    """
    state = SimpleNamespace(metadata_field=lambda field: blob)
    return SimpleNamespace(psm=SimpleNamespace(playing=state))


def renamed_field() -> Any:
    """An MRP instance whose protobuf has no field by the name asked for.

    protobuf reports that as a ValueError from `HasField`, which is what pyatv's
    accessor raises through.
    """

    def metadata_field(field: str) -> Any:
        raise ValueError(f"Protocol message has no field {field}")

    state = SimpleNamespace(metadata_field=metadata_field)
    return SimpleNamespace(psm=SimpleNamespace(playing=state))


def entity(
    mrp: Any = None,
    *,
    title: str | None = "Slow Horses",
    season: int | None = None,
    episode: int | None = None,
    features: set[FeatureName] = frozenset(),
) -> AppleTvMediaPlayer:
    player = AppleTvMediaPlayer("Living Room", "atv-1", Mock())
    # pyatv's metadata facade is a relayer: instances by protocol, looked up
    # with `get`, and None for a protocol the television does not speak.
    player.atv = SimpleNamespace(
        metadata=SimpleNamespace(get=lambda p: mrp if p is Protocol.MRP else None),
        features=SimpleNamespace(in_state=lambda state, feature: feature in features),
    )
    # A real pyatv snapshot, so every property the entity writes into state
    # finds what it reads.
    player._playing = Playing(
        media_type=MediaType.TV,
        device_state=DeviceState.Playing,
        title=title,
        season_number=season,
        episode_number=episode,
    )
    return player


@pytest.fixture
def grafted(hass):
    assert async_install(hass) is True
    yield
    async_remove()


def numbering(player: AppleTvMediaPlayer) -> tuple[str | None, str | None]:
    return player.media_season, player.media_episode


def test_the_archive_fills_what_pyatv_leaves_empty(grafted) -> None:
    """Season 6, episode 2, as strings: the type upstream's accessors answer with."""
    assert numbering(entity(playing())) == ("6", "2")


def test_pyatv_own_numbering_wins(grafted) -> None:
    """A pyatv that learns to read the archive itself needs no change here, and
    an app that fills the plain fields is believed over one that does not."""
    player = entity(playing(), season=4, episode=8, features=NUMBERED)

    assert numbering(player) == ("4", "8")


def test_nothing_on_screen_is_given_no_season(grafted) -> None:
    """After a push error the snapshot is cleared while the pyatv object stays
    and the player state may still describe the last item. A season on a state
    with no show would be a claim about nothing."""
    assert numbering(entity(playing(), title=None)) == (None, None)


def test_a_television_that_has_gone_is_left_to_upstream(grafted) -> None:
    player = entity(playing())
    player.atv = None

    assert numbering(player) == (None, None)


@pytest.mark.parametrize(
    "mrp",
    [None, playing(None), playing(b"not an archive"), renamed_field()],
    ids=["no MRP instance", "nothing queued or unset", "malformed", "renamed field"],
)
def test_a_link_that_is_missing_answers_upstream_way(grafted, mrp: Any) -> None:
    """Each is the shape of a moved seam, or of an app that never wrote the
    archive; none may raise inside a state write."""
    assert numbering(entity(mrp)) == (None, None)


async def test_the_numbering_reaches_the_state_machine(hass, grafted) -> None:
    """The graft exists so the scrobbler can read the numbering back from a
    recorded state, which is only true if Home Assistant's own state write
    collects the wrapped properties."""
    player = entity(playing())
    player.hass = hass
    player.entity_id = "media_player.living_room"

    player.async_write_ha_state()

    attributes = hass.states.get("media_player.living_room").attributes
    assert (attributes["media_season"], attributes["media_episode"]) == ("6", "2")


def test_installing_twice_does_not_double_wrap(hass, grafted) -> None:
    """A second install capturing the first wrapper as its original would
    decode every archive twice, and removal would restore the wrapper."""
    wrapped = AppleTvMediaPlayer.media_season
    assert async_install(hass) is True

    assert AppleTvMediaPlayer.media_season is wrapped


def test_removing_restores_the_apple_tv_completely(hass) -> None:
    """A removed add-on must leave no trace in an integration it does not own."""
    season, episode = AppleTvMediaPlayer.media_season, AppleTvMediaPlayer.media_episode
    assert async_install(hass) is True
    async_remove()

    assert AppleTvMediaPlayer.media_season is season
    assert AppleTvMediaPlayer.media_episode is episode


def test_removing_what_was_never_installed_is_harmless(hass) -> None:
    season = AppleTvMediaPlayer.media_season
    async_remove()

    assert AppleTvMediaPlayer.media_season is season


@pytest.mark.parametrize(
    "shape",
    [SimpleNamespace(), SimpleNamespace(media_season="4", media_episode="8")],
    ids=["no such properties", "plain attributes"],
)
def test_a_moved_seam_leaves_the_apple_tv_alone(hass, monkeypatch, shape) -> None:
    """Breaking the television would be worse than not numbering an episode.

    Upstream turning the accessors into plain attributes is the change that
    would otherwise make `fget` fail inside every state write.
    """
    season = AppleTvMediaPlayer.media_season
    monkeypatch.setattr(apple_media_player, "AppleTvMediaPlayer", shape, raising=True)

    assert async_install(hass) is False
    assert AppleTvMediaPlayer.media_season is season
