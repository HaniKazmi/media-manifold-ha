"""The Apple Music HTTP client.

Two behaviours here are load-bearing and invisible from the browse tree: every
request must carry an Origin header, and an oversized page must recover rather
than surface as an error.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.sonos_apple_music.applemusic.api import (
    _KNOWN_PAGE_LIMITS,
    AppleMusicClient,
    AppleMusicError,
    UserTokenInvalid,
    artwork_url,
)
from custom_components.sonos_apple_music.applemusic.const import (
    API_BASE,
    ORIGIN,
    PAGE_SIZE,
    RECENT_PAGE_SIZE,
    SAFE_PAGE_SIZE,
)
from custom_components.sonos_apple_music.applemusic.dev_token import (
    DeveloperToken,
    TokenError,
)

SEARCH = f"{API_BASE}/catalog/gb/search"


@pytest.fixture
def api(hass) -> AppleMusicClient:
    return AppleMusicClient(
        hass, DeveloperToken(hass), user_token="user", storefront="gb"
    )


async def test_a_broken_scrape_arrives_as_an_apple_music_error(hass) -> None:
    """Every caller of `get` handles AppleMusicError and nothing wider. A
    TokenError escaping raw reaches the media browser as an unhandled traceback
    rather than a browse error, and a play as one rather than a failed play.

    The broken token is injected rather than patched onto DeveloperToken: the
    suite already replaces `async_get` on that class for every test, and a second
    replacement of the same attribute unwinds in an order that leaves one of them
    behind.
    """

    class BrokenToken:
        async def async_get(self, force_refresh: bool = False) -> str:
            raise TokenError("Apple has changed the web player")

    api = AppleMusicClient(hass, BrokenToken(), user_token=None, storefront="gb")

    with pytest.raises(AppleMusicError, match="changed the web player"):
        await api.get("catalog/gb/search", term="x")


@pytest.mark.parametrize(
    ("endpoint", "status", "reauth"),
    [
        ("me/library/songs", 403, True),
        ("me/library/songs", 401, True),
        ("catalog/gb/stations", 401, True),
        ("catalog/gb/stations", 403, False),
    ],
    ids=["library-403", "library-401", "catalog-401", "catalog-403"],
)
async def test_only_a_refusal_about_the_cookie_asks_for_a_new_one(
    hass, aioclient_mock, endpoint, status, reauth
) -> None:
    """Apple answers 403 for catalog content a storefront does not carry, and
    `browse._stations` drops such a surface rather than failing the node. Read as
    an expired cookie, that would raise a repair asking the user to replace one
    that is working.
    """
    asked = []
    api = AppleMusicClient(
        hass,
        DeveloperToken(hass),
        user_token="user",
        storefront="gb",
        on_token_invalid=lambda: asked.append(True),
    )
    aioclient_mock.get(f"{API_BASE}/{endpoint}", status=status)

    with pytest.raises(AppleMusicError):
        await api.get(endpoint)

    assert bool(asked) is reauth


async def test_a_next_link_over_an_empty_page_does_not_spin() -> None:
    """Nothing else in the paging loop grows, so a `next` link that serves no
    items would keep the browse requesting for as long as Apple answered.
    """
    served = []

    async def get(endpoint: str, **params: Any) -> dict[str, Any]:
        served.append(endpoint)
        return {"data": [], "next": "/v1/me/library/songs?offset=25"}

    page = await paging_client(get).get_paged("me/library/songs", 48)

    assert page.items == []
    assert len(served) == 1


async def test_every_request_carries_the_origin(api, aioclient_mock) -> None:
    """The scraped developer token is locked to apple.com.

    Without a matching Origin the API answers 401 even on public endpoints, so a
    missing header looks like a credential problem rather than a header problem.
    """
    aioclient_mock.get(SEARCH, json={"results": {}})
    await api.get("catalog/gb/search", term="x")

    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Origin"] == ORIGIN
    assert headers["Authorization"] == "Bearer developer-token"
    assert headers["Music-User-Token"] == "user"


async def test_no_user_token_header_when_there_is_none(hass, aioclient_mock) -> None:
    """Catalog browsing works without a user token, so none must be sent."""
    api = AppleMusicClient(hass, DeveloperToken(hass), user_token=None, storefront="gb")
    aioclient_mock.get(SEARCH, json={"results": {}})
    await api.get("catalog/gb/search", term="x")

    assert "Music-User-Token" not in aioclient_mock.mock_calls[0][3]


async def test_a_401_refreshes_the_developer_token_once(
    api, aioclient_mock, no_token_scraping
) -> None:
    """Tokens expire mid-session, and a refresh is cheaper than failing."""
    responses = [401, 200]

    async def answer(method, url, data):
        return AiohttpClientMockResponse(
            method, url, status=responses.pop(0), json={"results": {}}
        )

    aioclient_mock.get(SEARCH, side_effect=answer)

    await api.get("catalog/gb/search", term="x")

    assert no_token_scraping.call_args.kwargs["force_refresh"] is True
    assert len(aioclient_mock.mock_calls) == 2


async def test_a_persistent_401_blames_the_user_token(api, aioclient_mock) -> None:
    """Once a fresh developer token still fails, the user token is the suspect."""
    aioclient_mock.get(SEARCH, status=401)
    aioclient_mock.get(SEARCH, status=401)

    with pytest.raises(UserTokenInvalid):
        await api.get("catalog/gb/search", term="x")


async def test_a_persistent_401_without_a_user_token_is_a_plain_error(
    hass, aioclient_mock
) -> None:
    api = AppleMusicClient(hass, DeveloperToken(hass), user_token=None, storefront="gb")
    aioclient_mock.get(SEARCH, status=401)
    aioclient_mock.get(SEARCH, status=401)

    with pytest.raises(AppleMusicError) as err:
        await api.get("catalog/gb/search", term="x")
    assert not isinstance(err.value, UserTokenInvalid)


async def test_a_rejected_user_token_is_announced(hass, aioclient_mock) -> None:
    """Nothing downstream can distinguish this from any other failed request.

    Without the announcement the cookie stays stale until someone reads a log.
    """
    told: list[bool] = []
    api = AppleMusicClient(
        hass,
        DeveloperToken(hass),
        user_token="user",
        storefront="gb",
        on_token_invalid=lambda: told.append(True),
    )
    aioclient_mock.get(SEARCH, status=401)
    aioclient_mock.get(SEARCH, status=401)

    with pytest.raises(UserTokenInvalid):
        await api.get("catalog/gb/search", term="x")
    assert told == [True]


async def test_a_failure_that_is_not_the_token_stays_quiet(
    hass, aioclient_mock
) -> None:
    """A reauth prompt for a 500 sends the user to re-copy a cookie that is fine."""
    told: list[bool] = []
    api = AppleMusicClient(
        hass,
        DeveloperToken(hass),
        user_token="user",
        storefront="gb",
        on_token_invalid=lambda: told.append(True),
    )
    aioclient_mock.get(SEARCH, status=500)

    with pytest.raises(AppleMusicError):
        await api.get("catalog/gb/search", term="x")
    assert told == []


async def test_a_rejected_token_is_not_mistaken_for_a_bad_connection(
    api, aioclient_mock
) -> None:
    """Keeping the configured storefront here would swallow the one thing the
    request can prove: that the cookie no longer works."""
    aioclient_mock.get(f"{API_BASE}/me/storefront", status=401)
    aioclient_mock.get(f"{API_BASE}/me/storefront", status=401)

    with pytest.raises(UserTokenInvalid):
        await api.async_resolve_storefront()


async def test_an_unreachable_storefront_lookup_keeps_the_configured_one(
    api, aioclient_mock
) -> None:
    """Apple being down must not cost the storefront the user chose."""
    aioclient_mock.get(f"{API_BASE}/me/storefront", status=500)

    assert await api.async_resolve_storefront() == "gb"


class Recorder:
    """Stands in for AppleMusicClient.get, recording the limits it is asked for."""

    def __init__(self, max_limit: int | None = None, pages: int = 1) -> None:
        self.max_limit = max_limit
        self.pages = pages
        self.limits: list[int] = []
        self._served = 0

    async def __call__(self, endpoint: str, **params: Any) -> dict[str, Any]:
        limit = params["limit"]
        self.limits.append(limit)
        if self.max_limit is not None and limit > self.max_limit:
            raise AppleMusicError(f"{endpoint} failed: 400", status=400)
        self._served += 1
        return {
            "data": [{"type": "songs", "id": str(i)} for i in range(limit)],
            "next": "/v1/me/x?offset=99" if self._served < self.pages else None,
        }


def paging_client(recorder: Recorder) -> AppleMusicClient:
    client = AppleMusicClient.__new__(AppleMusicClient)
    client._page_limits = {}
    client.get = recorder
    return client


async def test_a_known_cap_is_applied_without_probing_for_it() -> None:
    """Apple answers a bare 400 above the me/recent cap, naming no field.

    Discovering it costs a wasted request per restart, and the caller that opens
    the node sees nothing until the retry lands, so this one cap is known up
    front. It belongs to the client rather than to the caller: a caller passing
    its own page size is a second place for the same fact to be wrong.
    """
    recorder = Recorder(max_limit=RECENT_PAGE_SIZE)
    client = paging_client(recorder)
    client._page_limits = dict(_KNOWN_PAGE_LIMITS)

    await client.get_paged("me/recent/played/tracks", PAGE_SIZE)

    assert recorder.limits == [RECENT_PAGE_SIZE]


async def test_an_oversized_page_retries_at_the_safe_size() -> None:
    """Apple rejects an over-large limit with a bare 400 naming no field.

    A per-endpoint constant would only be shown wrong when someone opens that
    node, so the cap is discovered instead.
    """
    recorder = Recorder(max_limit=SAFE_PAGE_SIZE)
    page = await paging_client(recorder).get_paged("me/recommendations", 48)

    assert recorder.limits == [48, SAFE_PAGE_SIZE]
    assert len(page.items) == SAFE_PAGE_SIZE


async def test_the_discovered_cap_is_remembered() -> None:
    """The wasted request happens once per endpoint, not once per browse."""
    recorder = Recorder(max_limit=SAFE_PAGE_SIZE)
    client = paging_client(recorder)

    await client.get_paged("me/recommendations", 48)
    await client.get_paged("me/recommendations", 48)

    assert recorder.limits == [48, SAFE_PAGE_SIZE, SAFE_PAGE_SIZE]


async def test_a_cap_does_not_leak_to_other_endpoints() -> None:
    recorder = Recorder(max_limit=SAFE_PAGE_SIZE)
    client = paging_client(recorder)
    await client.get_paged("me/recommendations", 48)

    recorder.max_limit = None
    await client.get_paged("catalog/gb/search", 48)

    assert recorder.limits[-1] == 48


async def test_a_400_at_the_safe_size_still_raises() -> None:
    """Backoff must not swallow a 400 that has nothing to do with paging."""
    with pytest.raises(AppleMusicError):
        await paging_client(Recorder(max_limit=0)).get_paged("me/recommendations", 48)


async def test_non_400_errors_are_not_retried() -> None:
    class Failing(Recorder):
        async def __call__(self, endpoint: str, **params: Any) -> dict[str, Any]:
            self.limits.append(params["limit"])
            raise AppleMusicError("boom", status=500)

    recorder = Failing()
    with pytest.raises(AppleMusicError):
        await paging_client(recorder).get_paged("me/recommendations", 48)
    assert len(recorder.limits) == 1


async def test_paging_stops_at_the_requested_limit() -> None:
    """BrowseMedia cannot page, so a level must not grow without bound."""
    recorder = Recorder(pages=10)
    page = await paging_client(recorder).get_paged("me/library/songs", 25, page_size=10)
    assert len(page.items) == 25


async def test_a_next_link_keeps_the_offset_it_carries() -> None:
    """Apple's `next` is a /v1/ path with its own offset; dropping it re-reads
    page one forever."""
    seen: list[tuple[str, dict]] = []

    async def get(endpoint: str, **params: Any) -> dict[str, Any]:
        seen.append((endpoint, params))
        return {
            "data": [{"type": "songs", "id": "1"}],
            "next": "/v1/me/library/songs?offset=25&extend=x" if len(seen) == 1 else None,
        }

    await paging_client(get).get_paged("me/library/songs", 48)

    assert seen[1][0] == "me/library/songs"
    assert seen[1][1]["offset"] == "25"
    assert seen[1][1]["extend"] == "x"


async def test_a_next_link_that_echoes_limit_does_not_collide() -> None:
    """`next` is remote data, and several endpoints echo the request params into
    it. A duplicated `limit` raises TypeError, which no caller converts into a
    BrowseError, so the frontend gets a traceback instead of a message."""
    seen: list[dict] = []

    async def get(endpoint: str, **params: Any) -> dict[str, Any]:
        seen.append(params)
        echoed = "/v1/me/library/songs?offset=25&limit=999"
        return {
            "data": [{"type": "songs", "id": "1"}],
            "next": echoed if len(seen) == 1 else None,
        }

    page = await paging_client(get).get_paged("me/library/songs", 48)

    assert len(seen) == 2
    assert seen[1]["limit"] == 48
    assert len(page.items) == 2


async def test_the_total_apple_reports_is_kept() -> None:
    """A capped level has to say how much it hid, or 48 of 2000 songs looks
    exactly like all of them."""

    async def get(endpoint: str, **params: Any) -> dict[str, Any]:
        return {"data": [{"type": "songs", "id": "1"}], "meta": {"total": 2000}}

    page = await paging_client(get).get_paged("me/library/songs", 48)
    assert page.total == 2000


async def test_a_missing_total_is_not_invented() -> None:
    """Not every endpoint publishes a count; guessing one reports a wrong
    remainder rather than none."""

    async def get(endpoint: str, **params: Any) -> dict[str, Any]:
        return {"data": [{"type": "songs", "id": "1"}]}

    page = await paging_client(get).get_paged("me/library/songs", 48)
    assert page.total is None


def test_artwork_url_fills_in_the_template() -> None:
    """Apple returns a template; the placeholders must go or nothing renders."""
    url = artwork_url({"url": "https://example.invalid/{w}x{h}bb.jpg"}, size=500)
    assert url == "https://example.invalid/500x500bb.jpg"


@pytest.mark.parametrize("artwork", [None, {}, {"height": 10}])
def test_artwork_url_tolerates_a_missing_template(artwork) -> None:
    assert artwork_url(artwork) is None
