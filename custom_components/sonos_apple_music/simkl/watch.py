"""Watching the Apple TV players a household has, and reporting what they play.

This is the one part of the integration that patches nothing. Home Assistant
already publishes every state an Apple TV reaches, so scrobbling needs no seam
in the apple_tv integration — only a subscription, which is why this hangs off
the config entry rather than joining the grafts.

Players are found through the entity registry rather than the loaded config
entries, so the two integrations can start in either order: a registry that
already names an Apple TV is enough to subscribe, and subscribing to a state
that does not exist yet is answered when it appears.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import playing
from .api import SimklClient
from .const import CONF_SIMKL_TOKEN

_LOGGER = logging.getLogger(__name__)

APPLE_TV_DOMAIN = "apple_tv"


def async_start(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], None]:
    """Begin scrobbling, returning the callback that stops it.

    A household with no SIMKL token gets a subscription to nothing, so linking
    SIMKL is what turns this on and the rest of the integration is unaffected by
    never linking it.
    """
    if not (token := entry.data.get(CONF_SIMKL_TOKEN)):
        return lambda: None

    scrobbler = _Scrobbler(hass, entry, SimklClient(hass, token))
    return scrobbler.async_start()


class _Scrobbler:
    """One subscription to every Apple TV, and the client it reports through."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: SimklClient
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._unsub_states: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None

    @callback
    def async_start(self) -> Callable[[], None]:
        """Subscribe, and follow the registry so a new Apple TV is picked up."""
        self._unsub_registry = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed
        )
        self._async_resubscribe()
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        """Stop watching. Called when the entry unloads."""
        for unsub in (self._unsub_states, self._unsub_registry):
            if unsub is not None:
                unsub()
        self._unsub_states = self._unsub_registry = None

    @callback
    def _async_registry_changed(self, event: Event) -> None:
        """Re-subscribe when an entity appears or goes away.

        Only creations and removals can change the set of players; an update is
        a rename or a settings change to one already subscribed.
        """
        if event.data["action"] in ("create", "remove"):
            self._async_resubscribe()

    @callback
    def _async_resubscribe(self) -> None:
        registry = er.async_get(self._hass)
        entity_ids = [
            entity.entity_id
            for entity in registry.entities.values()
            if entity.platform == APPLE_TV_DOMAIN
            and entity.domain == MEDIA_PLAYER_DOMAIN
        ]

        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None

        if not entity_ids:
            _LOGGER.debug("No Apple TV media players to scrobble from yet")
            return

        self._unsub_states = async_track_state_change_event(
            self._hass, entity_ids, self._async_state_changed
        )
        _LOGGER.debug("Scrobbling Apple TV playback from %s", ", ".join(entity_ids))

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Turn one state change into the scrobbles it asks for.

        The requests go to a task rather than being awaited: SIMKL holds a
        20-second lock per user, and a state callback is not the place to wait
        on it.
        """
        scrobbles = playing.events(
            event.data["old_state"], event.data["new_state"], dt_util.utcnow()
        )
        for scrobble in scrobbles:
            self._entry.async_create_background_task(
                self._hass,
                self._client.async_scrobble(scrobble),
                f"simkl {scrobble.act} {scrobble.episode.show}",
            )
