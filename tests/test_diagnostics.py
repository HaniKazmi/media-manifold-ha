"""What the entry reports when someone presses Download diagnostics."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.sonos_apple_music.applemusic.const import CONF_USER_TOKEN
from custom_components.sonos_apple_music.simkl.const import CONF_SIMKL_TOKEN

from .conftest import (
    APPLE_TV_ATTRIBUTES,
    SHOW_ID,
    STOREFRONT,
    USER_TOKEN,
    add_player,
    scrobble_url,
    simkl_finds,
)


async def loaded(hass, config_entry, **data):
    """An entry that has finished setting up."""
    config_entry.add_to_hass(hass)
    if data:
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, **data}
        )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_neither_token_survives_redaction(
    hass, hass_client, config_entry, resolved_storefront
) -> None:
    """The file exists to be pasted into an issue, so it must be safe to paste."""
    await loaded(hass, config_entry, **{CONF_SIMKL_TOKEN: "simkl-token"})

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["entry"][CONF_USER_TOKEN] == "**REDACTED**"
    assert report["entry"][CONF_SIMKL_TOKEN] == "**REDACTED**"
    assert USER_TOKEN not in str(report)
    assert "simkl-token" not in str(report)


async def test_it_says_which_grafts_installed(
    hass, hass_client, config_entry, resolved_storefront
) -> None:
    """Whether a graft took is otherwise knowable only from a log line at setup."""
    await loaded(hass, config_entry)

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["grafts"] == {"Sonos": True, "Apple TV": True}


async def test_it_reports_the_apple_music_state(
    hass, hass_client, config_entry, resolved_storefront
) -> None:
    await loaded(hass, config_entry)

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["apple_music"]["storefront"] == STOREFRONT
    assert report["apple_music"]["library"] is True
    # Nothing has browsed, so no serial has been read from a favorite yet.
    assert report["apple_music"]["account_serial"] is None


async def test_an_unlinked_household_reports_simkl_as_off(
    hass, hass_client, config_entry, resolved_storefront
) -> None:
    await loaded(hass, config_entry)

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["simkl"] == {"linked": False, "watching": [], "shows": {}}


async def test_it_names_the_players_watched_and_the_ids_resolved(
    hass, hass_client, aioclient_mock, config_entry, resolved_storefront
) -> None:
    """A wrong id is filed under the wrong show, and this is where it is visible.

    The cache is filled by playing something rather than by reaching into the
    client, so what the report shows is what a real scrobble put there.
    """
    simkl_finds(aioclient_mock)
    aioclient_mock.post(scrobble_url("start"), json={"id": SHOW_ID})
    player = add_player(hass)

    await loaded(hass, config_entry, **{CONF_SIMKL_TOKEN: "simkl-token"})
    hass.states.async_set(player, "playing", APPLE_TV_ATTRIBUTES)
    await hass.async_block_till_done(wait_background_tasks=True)

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["simkl"]["linked"] is True
    assert report["simkl"]["watching"] == [player]
    assert report["simkl"]["shows"] == {"Black Bird": SHOW_ID}


async def test_an_entry_that_failed_setup_still_reports(
    hass, hass_client, config_entry, resolved_storefront
) -> None:
    """That entry is the one most worth asking about, and it has no runtime."""
    config_entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.sonos_apple_music.applemusic.patch.async_install",
            return_value=False,
        ),
        patch(
            "custom_components.sonos_apple_music.infuse.patch.async_install",
            return_value=False,
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    report = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert report["state"] == "setup_retry"
    assert report["entry"][CONF_USER_TOKEN] == "**REDACTED**"
    assert "apple_music" not in report
