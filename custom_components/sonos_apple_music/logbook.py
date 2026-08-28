"""Describe the watched event for the logbook.

Home Assistant finds this module on its own, and only when `logbook` is set up,
so a household without it keeps the event and loses only the line.

The description carries an ``entity_id``, which is what files the line under the
Apple TV that played the episode. Asking a television what it has been playing
is what its own logbook is for, and it means this integration surfaces its work
without adding an entity to hold it.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN
from .simkl.const import EVENT_WATCHED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Teach the logbook to read a watched episode."""

    @callback
    def async_describe_watched(event: Event) -> dict[str, str]:
        data = event.data
        episode = f"S{data['season']:02d}E{data['episode']:02d}"
        return {
            LOGBOOK_ENTRY_NAME: "SIMKL",
            LOGBOOK_ENTRY_MESSAGE: f"marked {data['show']} {episode} watched",
            LOGBOOK_ENTRY_ENTITY_ID: data["entity_id"],
        }

    async_describe_event(DOMAIN, EVENT_WATCHED, async_describe_watched)
