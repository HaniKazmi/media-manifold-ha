"""The Apple Music browse tree."""

from __future__ import annotations

from homeassistant.components.media_player import BrowseError, MediaClass
import pytest

from custom_components.sonos_apple_music.applemusic.api import AppleMusicError
from custom_components.sonos_apple_music.applemusic.browse import (
    ROOT_ID,
    async_browse,
    async_search,
    parse_content_id,
    root_payload,
)
from custom_components.sonos_apple_music.applemusic.const import URI_PREFIX

from .conftest import (
    ALBUM_ID,
    ARTIST_ID,
    GENRE_ID,
    GROUP_RECOMMENDATION_ID,
    PLAYLIST_ID,
    RECOMMENDATION_ID,
    artist_with_views,
    recommendation,
    song,
    station_genre,
)


@pytest.mark.parametrize(
    ("content_id", "expected"),
    [
        (f"{URI_PREFIX}root", ("root", "")),
        (f"{URI_PREFIX}charts/songs", ("charts", "songs")),
        (f"{URI_PREFIX}album/{ALBUM_ID}", ("album", ALBUM_ID)),
        (f"{URI_PREFIX}playlist/p.abc%2Fdef", ("playlist", "p.abc/def")),
    ],
)
def test_content_ids_round_trip(content_id: str, expected: tuple[str, str]) -> None:
    """Apple ids contain characters that must survive the URL encoding."""
    assert parse_content_id(content_id) == expected


def test_the_root_offers_search(client) -> None:
    """Sonos sets can_search nowhere, so its browser has no search box at all.

    Without opting in here, the search this integration implements is
    unreachable from the UI.
    """
    assert root_payload(client).can_search is True


def test_library_entries_need_a_user_token(catalog_only_client, client) -> None:
    """Showing them without a token would offer surfaces that only fail."""
    without = [child.title for child in root_payload(catalog_only_client).children]
    assert "Your Library" not in without
    assert "Charts" in without

    with_token = [child.title for child in root_payload(client).children]
    assert "Your Library" in with_token


async def test_charts_lists_its_types(client) -> None:
    payload = await async_browse(client, f"{URI_PREFIX}charts")
    assert [child.title for child in payload.children] == [
        "Top Songs",
        "Top Albums",
        "Top Playlists",
    ]


async def test_a_chart_section_is_browsable(client) -> None:
    """Each section is a real content id, not just inlined children.

    The frontend re-requests a node by id when it is opened, so a section that
    exists only as children dead-ends.
    """
    payload = await async_browse(client, f"{URI_PREFIX}charts/songs")
    assert payload.children
    assert payload.children[0].media_class == MediaClass.TRACK


async def test_an_unknown_chart_is_rejected(client) -> None:
    with pytest.raises(BrowseError):
        await async_browse(client, f"{URI_PREFIX}charts/nonsense")


async def test_an_album_lists_tracks_and_stays_playable(client) -> None:
    """Both matter: a whole album plays as one container, and its tracks list."""
    payload = await async_browse(client, f"{URI_PREFIX}album/{ALBUM_ID}")
    assert payload.can_play is True
    assert payload.can_expand is True
    assert payload.children[0].media_class == MediaClass.TRACK


async def test_a_playlist_lists_tracks(client) -> None:
    payload = await async_browse(client, f"{URI_PREFIX}playlist/{PLAYLIST_ID}")
    assert payload.can_play is True
    assert payload.children


async def test_a_library_id_routes_to_the_library_endpoint(client) -> None:
    """Library ids 404 against the catalog endpoints, so the split matters."""
    await async_browse(client, f"{URI_PREFIX}playlist/p.rXAJKVahZNRX4g")
    endpoint = client.requested[-1][0]
    assert endpoint.startswith("me/library/")


async def test_a_catalog_id_routes_to_the_catalog_endpoint(client) -> None:
    await async_browse(client, f"{URI_PREFIX}playlist/{PLAYLIST_ID}")
    assert client.requested[-1][0].startswith("catalog/")


async def test_recently_played_asks_for_tracks(client) -> None:
    """The bare me/recent/played returns stations and albums as well."""
    await async_browse(client, f"{URI_PREFIX}recent")
    assert client.requested[-1][0] == "me/recent/played/tracks"


async def test_an_api_failure_becomes_a_browse_error(client) -> None:
    """BrowseError is what the frontend can render; anything else is a traceback."""
    client.responses["me/recent/played/tracks"] = AppleMusicError("boom", status=500)
    with pytest.raises(BrowseError):
        await async_browse(client, f"{URI_PREFIX}recent")


async def test_an_unknown_kind_is_rejected(client) -> None:
    with pytest.raises(BrowseError):
        await async_browse(client, f"{URI_PREFIX}nonsense/1")


async def test_stations_are_playable_leaves(client) -> None:
    """A station has no track list; Sonos decides what plays next."""
    results = await async_search(client, "huntr/x")
    prefix = f"{URI_PREFIX}station/"
    stations = [r for r in results if r.media_content_id.startswith(prefix)]
    assert stations
    assert stations[0].can_play is True
    assert stations[0].can_expand is False


async def test_search_covers_every_playable_type(client) -> None:
    results = await async_search(client, "radiohead")
    kinds = {parse_content_id(r.media_content_id)[0] for r in results}
    assert kinds == {"song", "album", "artist", "playlist", "station"}


async def test_search_can_be_narrowed_to_one_type(client) -> None:
    await async_search(client, "radiohead", MediaClass.ALBUM)
    assert client.requested[-1][1]["types"] == "albums"


async def test_a_capped_level_reports_what_it_hid(client) -> None:
    """Without this a library of 2000 songs shows 48 and claims that is all."""
    client.responses["me/library/songs"] = {
        "data": [song(str(i)) for i in range(48)],
        "meta": {"total": 2000},
    }
    payload = await async_browse(client, f"{URI_PREFIX}library_songs")

    assert len(payload.children) == 48
    assert payload.not_shown == 1952


async def test_charts_backs_off_when_apple_rejects_the_page_size(client) -> None:
    """A bare 400 on the charts limit otherwise takes out the whole Charts node.

    Every other level retries at a size Apple accepts; charts asking for its own
    page is what put it outside that.
    """
    attempts: list[int] = []
    real_get = client.get

    async def get(endpoint, **params):
        if endpoint.endswith("charts"):
            attempts.append(params["limit"])
            if len(attempts) == 1:
                raise AppleMusicError("catalog/gb/charts failed: 400", status=400)
        return await real_get(endpoint, **params)

    client.get = get
    payload = await async_browse(client, f"{URI_PREFIX}charts/songs")

    assert len(attempts) == 2
    assert attempts[1] < attempts[0]
    assert payload.children


async def test_made_for_you_lists_its_shelves(client) -> None:
    """The shelf labels are what give the items their meaning.

    Flattened together, "Heavy Rotation" and "Made for You" are the same albums
    with nothing to tell them apart.
    """
    payload = await async_browse(client, f"{URI_PREFIX}recommendations")

    assert [child.title for child in payload.children] == [
        "Heavy Rotation",
        "Made for You",
    ]
    assert all(child.can_expand for child in payload.children)
    assert payload.not_shown == 0


async def test_a_shelf_is_browsable(client) -> None:
    """Each shelf is a real content id, not just inlined children.

    The frontend re-requests a node by id when it is opened, so a shelf that
    exists only as children dead-ends.
    """
    listing = await async_browse(client, f"{URI_PREFIX}recommendations")
    payload = await async_browse(client, listing.children[0].media_content_id)

    assert client.requested[-1][0] == f"me/recommendations/{RECOMMENDATION_ID}"
    assert payload.title == "Heavy Rotation"
    assert payload.children[0].media_class == MediaClass.ALBUM


async def test_a_group_shelf_lists_the_shelves_inside_it(client) -> None:
    """A group holds shelves under a different relationship than resources.

    Reading only `contents` renders it as empty, which is how a whole branch of
    the recommendations goes missing.
    """
    payload = await async_browse(
        client, f"{URI_PREFIX}recommendation/{GROUP_RECOMMENDATION_ID}"
    )

    kinds = {parse_content_id(c.media_content_id)[0] for c in payload.children}
    assert kinds == {"recommendation"}
    assert payload.children[0].can_expand is True


async def test_a_shelf_without_a_title_still_appears(client) -> None:
    """Apple does not always name a shelf; dropping it hides its contents."""
    client.responses["me/recommendations"] = {"data": [recommendation(title=None)]}
    payload = await async_browse(client, f"{URI_PREFIX}recommendations")

    assert [child.title for child in payload.children] == ["Because you like"]


async def test_a_shelf_reports_contents_it_could_not_render(client) -> None:
    """Music videos and uploaded tracks have no Sonos URI, so they are dropped."""
    client.responses[f"me/recommendations/{RECOMMENDATION_ID}"] = {
        "data": [recommendation(contents=[song(), {"id": "1", "type": "music-videos"}])]
    }
    payload = await async_browse(
        client, f"{URI_PREFIX}recommendation/{RECOMMENDATION_ID}"
    )

    assert len(payload.children) == 1
    assert payload.not_shown == 1


async def test_a_stale_shelf_id_is_rejected(client) -> None:
    """Apple rotates recommendation ids, so a held id resolves to nothing."""
    client.responses[f"me/recommendations/{RECOMMENDATION_ID}"] = {"data": []}
    with pytest.raises(BrowseError):
        await async_browse(client, f"{URI_PREFIX}recommendation/{RECOMMENDATION_ID}")


async def test_an_artist_offers_its_sections(client) -> None:
    """Albums alone leave an artist with nothing to press play on.

    Top Songs are songs, so they play through the track path an artist itself
    has no URI for.
    """
    payload = await async_browse(client, f"{URI_PREFIX}artist/{ARTIST_ID}")

    assert payload.title == "Radiohead"
    assert [child.title for child in payload.children] == [
        "Top Songs",
        "Albums",
        "Singles & EPs",
        "Appears On",
        "Similar Artists",
    ]


async def test_an_artist_omits_the_sections_apple_has_nothing_for(client) -> None:
    """Plenty of artists appear on nothing and resemble nobody.

    Offering those sections anyway spends a request each to render empty.
    """
    client.responses[f"catalog/gb/artists/{ARTIST_ID}"] = {
        "data": [artist_with_views(views=("top-songs", "full-albums"))]
    }
    payload = await async_browse(client, f"{URI_PREFIX}artist/{ARTIST_ID}")

    assert [child.title for child in payload.children] == ["Top Songs", "Albums"]


async def test_an_artist_section_lists_its_items(client) -> None:
    payload = await async_browse(client, f"{URI_PREFIX}artist_top_songs/{ARTIST_ID}")

    endpoint = client.requested[-1][0]
    assert endpoint == f"catalog/gb/artists/{ARTIST_ID}/view/top-songs"
    assert payload.title == "Top Songs"
    assert payload.children[0].media_class == MediaClass.TRACK


async def test_similar_artists_stay_browsable(client) -> None:
    """The section is only worth having if its artists open in turn."""
    payload = await async_browse(client, f"{URI_PREFIX}artist_similar/{ARTIST_ID}")

    assert payload.children[0].can_expand is True
    assert parse_content_id(payload.children[0].media_content_id)[0] == "artist"


async def test_a_library_artist_lists_albums_rather_than_sections(client) -> None:
    """Views are a catalog feature; a library artist has only its albums.

    Offering the sections anyway gives five children that each answer 404.
    """
    payload = await async_browse(client, f"{URI_PREFIX}artist/r.AbCdEfG")

    assert client.requested[-1][0] == "me/library/artists/r.AbCdEfG/albums"
    assert payload.title == "Albums"
    assert payload.children[0].media_class == MediaClass.ALBUM


async def test_the_root_offers_radio_without_a_token(catalog_only_client) -> None:
    """Radio is catalog content, so it is not gated on the library cookie."""
    assert "Radio" in [c.title for c in root_payload(catalog_only_client).children]


async def test_radio_offers_live_stations_and_genres(client) -> None:
    payload = await async_browse(client, f"{URI_PREFIX}radio")

    kinds = [parse_content_id(c.media_content_id)[0] for c in payload.children]
    assert "station" in kinds
    assert "station_genre" in kinds
    assert [name for name, _ in client.requested].count("catalog/gb/stations") == 2


async def test_your_station_is_only_asked_for_with_a_token(catalog_only_client) -> None:
    """Apple answers 403 to the identity filter without one."""
    await async_browse(catalog_only_client, f"{URI_PREFIX}radio")

    asked = catalog_only_client.requested
    filters = [params.get("filter[identity]") for _, params in asked]
    assert "personal" not in filters


async def test_a_radio_surface_apple_declines_is_left_out(client) -> None:
    """The genres alone still make the node worth opening."""
    client.responses["catalog/gb/stations"] = AppleMusicError("forbidden", status=403)
    payload = await async_browse(client, f"{URI_PREFIX}radio")

    kinds = {parse_content_id(c.media_content_id)[0] for c in payload.children}
    assert kinds == {"station_genre"}


async def test_a_radio_genre_is_named_after_itself(client) -> None:
    """The stations arrive with the genre because only the genre carries its name."""
    payload = await async_browse(client, f"{URI_PREFIX}station_genre/{GENRE_ID}")

    assert payload.title == "Alternative & Indie"
    assert payload.children[0].can_play is True


async def test_an_unknown_radio_genre_is_rejected(client) -> None:
    client.responses[f"catalog/gb/station-genres/{GENRE_ID}"] = {"data": []}
    with pytest.raises(BrowseError):
        await async_browse(client, f"{URI_PREFIX}station_genre/{GENRE_ID}")


async def test_a_genre_with_no_stations_is_still_a_valid_node(client) -> None:
    """A relationship Apple omits is not an error, just an empty shelf."""
    client.responses[f"catalog/gb/station-genres/{GENRE_ID}"] = {
        "data": [station_genre()]
    }
    payload = await async_browse(client, f"{URI_PREFIX}station_genre/{GENRE_ID}")

    assert payload.children == []


def test_the_root_id_is_namespaced() -> None:
    """Routing in the patch layer is by prefix, so this must not drift."""
    assert ROOT_ID.startswith(URI_PREFIX)


async def test_a_stale_artist_section_id_fails_inside_the_contract(client) -> None:
    """`artist_` is dispatched by prefix, so a section this tree no longer mints
    still reaches its handler; only BrowseError is converted for the caller."""
    with pytest.raises(BrowseError, match="no such artist section"):
        await async_browse(client, f"{URI_PREFIX}artist_b_sides/1234")


async def test_a_mixed_level_does_not_claim_one_kind(client) -> None:
    """Radio lists live stations before its genre directories. Labelling the
    whole level with the first child's class labels most of it wrongly."""
    payload = await async_browse(client, f"{URI_PREFIX}radio")

    kinds = {child.media_class for child in payload.children}
    assert len(kinds) > 1
    assert payload.children_media_class is MediaClass.DIRECTORY
