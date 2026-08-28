"""Setting the integration up and tearing it down."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap
from unittest.mock import patch

from homeassistant.components.apple_tv.media_player import AppleTvMediaPlayer
from homeassistant.components.sonos import media_browser
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
import pytest

from custom_components.sonos_apple_music.applemusic.api import UserTokenInvalid
from custom_components.sonos_apple_music.applemusic.dev_token import TokenError
from custom_components.sonos_apple_music.const import DOMAIN

SONOS_GRAFT = "custom_components.sonos_apple_music.applemusic.patch.async_install"
INFUSE_GRAFT = "custom_components.sonos_apple_music.infuse.patch.async_install"


async def setup(hass, config_entry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_installs_the_patches(
    hass, config_entry, resolved_storefront
) -> None:
    original = media_browser.async_browse_media
    await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert media_browser.async_browse_media is not original
    assert DOMAIN in hass.data

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_restores_sonos_completely(
    hass, config_entry, resolved_storefront
) -> None:
    """A removed add-on must leave no trace in an integration it does not own."""
    original = media_browser.async_browse_media
    await setup(hass, config_entry)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert media_browser.async_browse_media is original
    assert DOMAIN not in hass.data


async def test_a_failed_scrape_retries_rather_than_failing_outright(
    hass, config_entry, no_token_scraping
) -> None:
    """Apple being unreachable is transient; a fixed scrape arrives on restart."""
    no_token_scraping.side_effect = TokenError("unreachable")
    await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_restores_the_apple_tv_completely(
    hass, config_entry, resolved_storefront
) -> None:
    original = AppleTvMediaPlayer.async_play_media
    await setup(hass, config_entry)
    assert AppleTvMediaPlayer.async_play_media is not original

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert AppleTvMediaPlayer.async_play_media is original


async def test_sonos_is_left_alone_when_it_cannot_be_patched(
    hass, config_entry, resolved_storefront
) -> None:
    """Breaking Sonos would be worse than not adding Apple Music, and the two
    grafts answer for different rooms: one absent speaker is no reason to
    withhold the other player."""
    original = media_browser.async_browse_media
    with patch(SONOS_GRAFT, return_value=False):
        await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert media_browser.async_browse_media is original
    assert AppleTvMediaPlayer.async_play_media is not original

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_nothing_to_graft_onto_waits_rather_than_failing(
    hass, config_entry, resolved_storefront
) -> None:
    """A household with neither player is usually one that has not finished
    starting, and a retry costs nothing where a failure needs a restart."""
    with (
        patch(SONOS_GRAFT, return_value=False),
        patch(INFUSE_GRAFT, return_value=False),
    ):
        await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert DOMAIN not in hass.data


async def test_an_expired_cookie_asks_for_a_new_one(
    hass, config_entry, resolved_storefront
) -> None:
    """Every library surface would render and then fail one by one otherwise.

    Only the cookie fixes it, so the cookie is what the user is put in front of.
    """
    resolved_storefront.side_effect = UserTokenInvalid("expired")
    await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def test_a_cookie_rejected_later_asks_for_a_new_one(
    hass, config_entry, resolved_storefront
) -> None:
    """A cookie expires while Home Assistant runs, not while it starts.

    A check that only runs at setup never fires for the case that matters.
    """
    await setup(hass, config_entry)

    hass.data[DOMAIN]._on_token_invalid()
    await hass.async_block_till_done()

    assert [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


@pytest.mark.parametrize("absent", ["soco", "pyatv"])
def test_the_package_imports_without_what_it_grafts_onto(absent: str) -> None:
    """soco and pyatv belong to Sonos and Apple TV; this integration declares no
    requirements of its own, so each is absent until its own integration has
    been set up.

    Home Assistant imports the package to reach the config flow, which happens
    long before either `async_install` can decline to patch anything. An import
    at module scope therefore turns "Sonos is not set up yet" into an
    integration that cannot even be added.

    Run in a subprocess: blocking a module already imported by the test session
    would not reproduce a cold import.
    """
    script = textwrap.dedent(
        f"""
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "{absent}" or name.startswith("{absent}."):
                    raise ImportError("not set up, so {absent} is absent")
                return None

        sys.meta_path.insert(0, Blocker())
        import custom_components.sonos_apple_music.config_flow  # noqa: F401
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
