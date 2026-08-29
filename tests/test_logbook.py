"""Describing a watched episode for the logbook.

Home Assistant discovers this platform by module name and by function name, and
a rename would be silent — the events would keep firing and simply stop being
readable. So the platform is loaded through Home Assistant's own loader and the
describer is taken from what it registers, rather than imported directly.

Setting the real `logbook` up would be closer still, but it depends on the
recorder, whose fixture has to be built before `hass` — and this suite creates
`hass` in an autouse fixture, so the ordering cannot be had here.
"""

from __future__ import annotations

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event
from homeassistant.loader import async_get_integration

from custom_components.manifold.const import DOMAIN
from custom_components.manifold.simkl.const import EVENT_WATCHED

WATCHED = {
    "entity_id": "media_player.appletv",
    "show": "Black Bird",
    "season": 1,
    "episode": 4,
    "progress": 98.61,
    "simkl_id": 1624792,
}


async def describer(hass):
    """The describe callback the logbook platform registers, found as HA finds it."""
    integration = await async_get_integration(hass, DOMAIN)
    platform = await integration.async_get_platform("logbook")

    registered: dict[str, tuple[str, object]] = {}
    platform.async_describe_events(
        hass, lambda domain, event, describe: registered.__setitem__(
            event, (domain, describe)
        )
    )

    domain, describe = registered[EVENT_WATCHED]
    assert domain == DOMAIN
    return describe


async def test_the_platform_is_found_and_names_the_episode(hass) -> None:
    describe = await describer(hass)

    described = describe(Event(EVENT_WATCHED, WATCHED))

    assert described[LOGBOOK_ENTRY_NAME] == "SIMKL"
    assert described[LOGBOOK_ENTRY_MESSAGE] == "marked Black Bird S01E04 watched"


async def test_the_line_is_filed_under_the_television(hass) -> None:
    """Asking a player what it has been playing is what its own logbook is for."""
    describe = await describer(hass)

    described = describe(Event(EVENT_WATCHED, WATCHED))

    assert described[LOGBOOK_ENTRY_ENTITY_ID] == "media_player.appletv"


async def test_the_numbering_is_padded(hass) -> None:
    """S1E4 and S01E04 sort differently in a list of a season's episodes."""
    describe = await describer(hass)

    described = describe(Event(EVENT_WATCHED, {**WATCHED, "season": 12, "episode": 3}))

    assert "S12E03" in described[LOGBOOK_ENTRY_MESSAGE]
