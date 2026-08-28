"""Watching the Apple TV players a household has, and reporting what they play.

This is the one part of the integration that patches nothing. Home Assistant
already publishes every state an Apple TV reaches, so scrobbling needs no seam
in the apple_tv integration — only a subscription, which is why this hangs off
the config entry rather than joining the grafts.

Players are found through the entity registry rather than the loaded config
entries, so the two integrations can start in either order: a registry that
already names an Apple TV is enough to subscribe, and subscribing to a state
that does not exist yet is answered when it appears.

Everything this integration tells a household about scrobbling is decided here
— the watched event and the repair issue both — so that `api.py` below can stay
a thing that speaks HTTP and knows nothing about Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from . import playing
from .api import Result, SimklClient
from .const import CONF_SIMKL_TOKEN, EVENT_WATCHED, TOKEN_ISSUE, WATCHED_AT

_LOGGER = logging.getLogger(__name__)

APPLE_TV_DOMAIN = "apple_tv"


def marks_watched(scrobble: playing.Event) -> bool:
    """Whether SIMKL counts this scrobble as having watched the episode.

    The rule is SIMKL's and is applied on their side; this is the local mirror
    of it, kept in one place so there is one line to correct if their threshold
    moves. Their answer is not readable from the response, so it cannot simply
    be asked for.
    """
    return scrobble.act == "stop" and (scrobble.progress or 0) >= WATCHED_AT


def _is_apple_tv(entity: er.RegistryEntry | None) -> bool:
    """Whether a registry entry is a player worth subscribing to."""
    return (
        entity is not None
        and entity.platform == APPLE_TV_DOMAIN
        and entity.domain == MEDIA_PLAYER_DOMAIN
    )


def async_start(hass: HomeAssistant, entry: ConfigEntry) -> Scrobbler | None:
    """Begin scrobbling, or return None for a household that has not linked SIMKL.

    Linking is what turns this on, and the rest of the integration is unaffected
    by never linking it: without a token nothing subscribes to anything.
    """
    if not (token := entry.data.get(CONF_SIMKL_TOKEN)):
        return None

    scrobbler = Scrobbler(hass, entry, SimklClient(hass, token))
    scrobbler.async_start()
    return scrobbler


class Scrobbler:
    """One subscription to every Apple TV, and the client it reports through."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: SimklClient
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._unsub_states: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._watching: list[str] = []

    @property
    def shows(self) -> dict[str, int]:
        """The show ids resolved so far, for diagnostics."""
        return self._client.shows

    @property
    def watching(self) -> list[str]:
        """The players subscribed to. Empty is the answer to most of the
        questions someone asks when nothing is being scrobbled."""
        return list(self._watching)

    @callback
    def async_start(self) -> None:
        """Subscribe, and follow the registry so a new Apple TV is picked up."""
        self._unsub_registry = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed
        )
        self._async_resubscribe()

    @callback
    def async_stop(self) -> None:
        """Stop watching. Called when the entry unloads."""
        for unsub in (self._unsub_states, self._unsub_registry):
            if unsub is not None:
                unsub()
        self._unsub_states = self._unsub_registry = None

    @callback
    def _async_registry_changed(self, event: Event) -> None:
        """Re-subscribe when an Apple TV appears or goes away.

        Every entity in Home Assistant passes through here, and a household has
        thousands, so whether this one matters is settled by a dict lookup
        before the registry is scanned. Only creations and removals can change
        the set: an update is a rename or a setting on one already known.
        """
        action, entity_id = event.data["action"], event.data["entity_id"]
        if action == "remove":
            ours = entity_id in self._watching
        elif action == "create":
            ours = _is_apple_tv(er.async_get(self._hass).async_get(entity_id))
        else:
            ours = False

        if ours:
            self._async_resubscribe()

    @callback
    def _async_resubscribe(self) -> None:
        registry = er.async_get(self._hass)
        entity_ids = [
            entity.entity_id
            for entity in registry.entities.values()
            if _is_apple_tv(entity)
        ]

        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None

        self._watching = entity_ids
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
                self._async_report(scrobble, event.data["entity_id"]),
                f"simkl {scrobble.act} {scrobble.episode.show}",
            )

    async def _async_report(self, scrobble: playing.Event, entity_id: str) -> None:
        """Send one scrobble, and say what came of it.

        Both ways of saying it are Home Assistant's, not the client's: an event
        for an episode that reached the history, a repair for a token SIMKL has
        stopped accepting. The player it was watched on is known here and
        nowhere below.
        """
        result = await self._client.async_scrobble(scrobble)

        if result is Result.TOKEN_REJECTED:
            self._async_token_rejected()
            return

        if result is not Result.RECORDED:
            return

        # A scrobble that lands proves the token, whatever it was doing before.
        ir.async_delete_issue(self._hass, DOMAIN, TOKEN_ISSUE)

        if not marks_watched(scrobble):
            return

        self._hass.bus.async_fire(
            EVENT_WATCHED,
            {
                "entity_id": entity_id,
                "show": scrobble.episode.show,
                "season": scrobble.episode.season,
                "episode": scrobble.episode.number,
                "progress": scrobble.progress,
                "simkl_id": self._client.show_id(scrobble.episode.show),
            },
        )

    @callback
    def _async_token_rejected(self) -> None:
        """Raise a repair, rather than putting the whole entry into reauth.

        A SIMKL token lasts five years, and the entry's reauth flow asks for the
        Apple Music cookie. Sending someone there to fix scrobbling would make
        the common path stranger to serve the rare one.
        """
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            TOKEN_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=TOKEN_ISSUE,
        )
        _LOGGER.warning(
            "SIMKL rejected the stored token; Apple TV playback is not being "
            "scrobbled. Reconfigure the integration to link SIMKL again"
        )
