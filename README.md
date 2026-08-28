# Apple Music for Sonos, Jellyfin for Apple TV

Two sources grafted onto media players Home Assistant already has: Apple Music
browsing, search and playback in the Sonos media browser, and Jellyfin playback
on an Apple TV by way of Infuse. Both wrap core integrations rather than forking
them, and neither adds an entity — the players a household already has gain a
source, rather than a duplicate that has to be kept in step with the real one.

A third thing rides along and grafts onto nothing: playback in the Apple TV's
own app can be scrobbled to [SIMKL](https://simkl.com), which needs no seam at
all because Home Assistant already publishes every state the television reaches.

Either graft works without the other: a household with only one of those players
sets up normally, and a graft that cannot install leaves its integration
untouched. They do share one config entry, though, so an Apple Music failure
that puts the entry into retry — an unreachable Apple, a changed web player —
holds the Apple TV graft back with it until it clears.

## Installing

Add this repository to HACS as a custom repository of type *Integration*,
install it, and restart Home Assistant. Then add it from Settings → Devices &
Services.

Setup asks about Apple Music only. The Apple TV side needs no configuration at
all: the Jellyfin credentials come from the core `jellyfin` integration's own
config entry, read at play time.

The one value it asks for is optional: the `media-user-token` cookie from a
signed-in music.apple.com session (browser devtools → Application → Cookies).
Leave it blank and catalog search, charts, radio and playback still work; supply
it to browse your own library, playlists, recently played and recommendations.

The cookie expires. When Apple rejects it, the entry raises a reauth prompt
asking for a fresh one; leaving that field empty is a valid answer, and drops
library access while keeping everything the catalog covers.

Setup then offers to link SIMKL. Choosing to shows a five-character code to
enter at <https://simkl.com/pin> on any device; press Submit once it is
approved. Skipping is a complete answer and leaves everything else working.
Either way it can be changed later with *Reconfigure* on the entry, which is
also how scrobbling is turned off again.

**One requirement that is easy to miss:** at least one Apple Music favorite must
exist in the Sonos app. See [The account serial](#the-account-serial-sn).

## How Apple Music works

Browsing and playback are independent, and neither involves Sonos's music
service API:

- **Metadata** comes from `api.music.apple.com` directly.
- **Playback** is a natively-formed Sonos URI. The speaker resolves it against
  the household's *own* Apple Music link, so nothing is streamed through Home
  Assistant.

Apple Music's SMAPI endpoint (service id 204) is Sonos-identity-gated:
`getAppLink` answers `403 NOT_AUTHORIZED` to third-party clients, because the
service manifest ships keys only Sonos's app and speaker firmware can decrypt.
So `soco.music_services.MusicService` cannot browse Apple Music, and this does
not try.

## The three playback shapes

Apple Music content reaches a speaker three different ways. Using the wrong one
fails in a way that is hard to read from Home Assistant: the call succeeds and
no audio starts.

| | Track | Album / playlist | Station |
|---|---|---|---|
| URI scheme | `x-sonosapi-hls-static:` | `x-rincon-cpcontainer:` | `x-sonosapi-radio:` |
| flags | 8232 | 8300 | 0 |
| DIDL item id | `00032020song%3a…` | `1004206calbum%3A…` / `1006206cplaylist%3A…` | `000c0000radio%3a…` |
| DIDL parent id | the album's catalog id | same as item id | `-1` |
| How it is played | `add_to_queue` | `add_to_queue` | `play_uri` |

`play_uri` answers **UPnP 714 "Illegal MIME-Type"** for every container form, so
containers must be queued; `add_to_queue` expands them server-side into real
entries. Stations are the reverse — a stream has no queue entries to sit beside,
so every enqueue mode collapses to replacing the transport. This split mirrors
core's own `_play_favorite`, which sends radio and line-in through `play_uri`
and everything else through the queue.

Every item also carries `<desc id="cdudn">SA_RINCON52231_X_#Svc52231-0-Token`,
which binds it to the household's Apple Music account. 52231 is the RINCON
service type, `(204 << 8) + 7`. soco defaults `desc` to
`RINCON_AssociatedZPUDN`, so it must be passed explicitly every time.

A track's `parentID` must be a real album catalog id. A placeholder is accepted
into the queue and then silently refuses to start.

## The account serial (`sn`)

Playback URIs carry `sn=<serial>`, identifying the household's Apple Music
account. Sonos S2 removed `/status/accounts`, so the serial is only observable
in URIs the Sonos app has already written: `discover_sn` scans the in-memory
favorites for one belonging to service 204 and reads its `sn`. It reads each
favorite's own `resources[0].uri` rather than `.reference`, because soco 0.31.1
lets `IndexError` escape from `DidlFavorite.reference` for favorites without
`resMD`, and one such favorite would otherwise end the scan.

The fallback is 1, never 0 — services that require an account binding reject
`sn=0`.

**This means at least one Apple Music favorite must exist in the Sonos app.**
Without it the serial is a guess. The guess is right for plenty of households,
so playback still goes ahead — refusing outright would deny playback that would
have worked — but a wrong guess costs an item that enqueues cleanly and never
sounds. A repair issue is raised whenever the scan comes up empty, because that
silence is otherwise a failure with no error attached to it. It clears itself
once a favorite exists and something is played.

## Credentials

Three, and only the first is required:

- **Developer token** — scraped from the Apple Music web player's JS bundle,
  which carries three ES256 JWTs. Only the one issued by `AMPWebPlay` is
  accepted; the other two are well-formed, unexpired, and answer 401. Selection
  is on the issuer, not position — the order varies between hosts.

  The token's claims include `"root_https_origin": ["apple.com"]` and the API
  enforces it: a request is 200 with `Origin: https://music.apple.com` and 401
  with any other Origin or none. Every request therefore sends that header.

  This is also why there is no in-browser sign-in. A MusicKit JS page served
  from Home Assistant cannot spoof its Origin, so it would be refused before
  reaching a login prompt.

- **Music user token** — the `media-user-token` cookie from a signed-in
  music.apple.com session, pasted during setup. Optional: catalog search,
  charts, radio and playback all work without it, and only the library surfaces
  are gated on it.

  It expires on Apple's schedule, not on restart, so the check that matters is
  the one that runs while Home Assistant is up: the client announces a rejected
  token wherever it happens and the entry starts a reauth flow. Setup checks it
  too, and fails the entry rather than rendering library surfaces that error one
  by one when opened.

- **SIMKL access token** — obtained through SIMKL's PIN device flow during
  setup, and used only for scrobbling. Optional in the strongest sense: without
  it nothing subscribes to anything.

  The flow needs no client secret and no redirect URI, so the client id ships
  with the integration the way every scrobbler app's does, and a self-hosted
  Home Assistant has nothing to register.

  The token lasts five years. When SIMKL rejects one anyway, this raises a
  repair issue pointing at *Reconfigure* rather than putting the whole entry
  into reauth: the reauth flow asks for the Apple Music cookie, and sending
  someone there to fix scrobbling would make the common path stranger to serve
  the rare one.

## The browse tree

| Node | Needs the cookie | Source |
|---|---|---|
| Charts | no | `catalog/{sf}/charts` |
| Radio | no | live stations by id, `station-genres`, plus your own station with a cookie |
| Your Library | yes | `me/library/*` |
| Recently Played | yes | `me/recent/played/tracks` |
| Made for You | yes | `me/recommendations`, one node per shelf |

An artist opens as its sections — Top Songs, Albums, Singles & EPs, Appears On,
Similar Artists — from Apple's artist views. Asking for every view costs one
request and says which of them hold anything, so a section Apple has nothing for
is never offered; plenty of artists appear on nothing. Top Songs is also the only
way to play an artist at all, since Sonos has no "play this artist" URI. A
library artist has no views, only an albums relationship, so it opens as that
list instead.

Radio's live broadcasts (Apple Music 1, Hits, Country) belong to no genre and no
filtered listing reaches them, so they are fetched by id — names and artwork
still come from Apple, and a retired id is simply absent from the response.

## Page sizes

Apple caps page size per endpoint, publishes no way to ask what the cap is, and
rejects an oversized request with a bare 400 naming no field — so a wrong
constant only shows up when someone opens that node. `get_paged` therefore
retries a rejected request once at 10 items, the size every documented endpoint
accepts, and remembers the cap for that endpoint. It logs the discovered cap at
INFO, and re-raises any 400 that survives the smaller request, so a 400 with
nothing to do with paging is not swallowed.

## Jellyfin on the Apple TV

The Apple TV entity already browses Jellyfin — with an app list present it
passes no content filter to the media source tree, so films and episodes render
in its media browser today. What it does with them is the problem: pressing play
sends the `media-source://jellyfin/…` id down the AirPlay path, which wants a
streamable HTTP URL and is wrong for video. This graft intercepts those ids and
hands the Apple TV an `infuse://` URL instead.

Infuse offers two ways in, and they differ in what they leave behind:

| Item | URL | Leaves behind |
|---|---|---|
| Matched against TMDB | `infuse://movie/{tmdb}?play`, `infuse://series/{tmdb}-{season}-{episode}?play` | Playback stays inside Infuse's library, which is what syncs watched and resume state back to Jellyfin |
| Everything else | `infuse://x-callback-url/play?url=…&position=…` | Nothing — the film plays and stays unwatched in Jellyfin |

The deep link is tried first and the stream URL is the fallback, so home videos
and unmatched files play rather than failing silently. The fallback carries
Jellyfin's own resume position, so resume survives even where progress does not
flow back.

Music from the same tree is left alone. RAOP already plays Jellyfin audio, and
Infuse is a video player: handed a track it would open on nothing.

## Scrobbling to SIMKL

Only the Apple TV's own app is scrobbled. Infuse playback is deliberately left
alone: a Jellyfin webhook already reports it, and a second reporter would race
that one for the same episode.

What makes the two separable is the same thing that makes the app's playback
readable at all. The native app reports an eleven-character `media_content_id`
and Infuse reports none, so requiring one is both the gate and the source of the
numbering:

```
A 00544 01 004     ->  Black Bird, season 1, episode 4
│ │     │  └── episode, three digits
│ │     └───── season, two digits
│ └─────────── a per-show prefix, stable across sessions
└───────────── literal A
```

pyatv populates none of `media_series_title`, `media_season` or `media_episode`
for this app, so that id is the only place the numbering exists. The show's name
comes from `media_title`. Apple Music on the same television shares the
`com.apple.TV` prefix and also reports no content id, so it is excluded by the
same rule.

Three transitions are reported, and one state change can produce two of them:
playing on into the next episode ends one and begins another, and a `start`
alone would leave the finished episode short of the 80% that marks it watched.

| Transition | Sent | Progress from |
|---|---|---|
| into `playing`, or onto a different episode | `start` | the new state |
| `playing` → `paused`, same episode | `pause` | the new state |
| a playing or paused episode replaced or left | `stop` | the state it had |

Progress is `media_position` plus the seconds since `media_position_updated_at`
while playing, over `media_duration`. Those two position attributes are in
`MediaPlayerEntity._entity_component_unrecorded_attributes`, so they are live
only and never reach the database — history is no help in reasoning about them.
Without a duration the progress is unknown rather than zero, and the `pause` or
`stop` is dropped: both write the number they carry into SIMKL's saved position,
so reporting zero would replace a real resume point with a place nobody watched
to.

### Naming the show to SIMKL

A scrobble needs an id, or a title **and** a year. The television reports a
title and no year, so the id is looked up — once per show, then remembered:

| `show` payload | Result |
|---|---|
| `{"title": "Black Bird"}` | 404 `id_err` |
| `{"title": "Black Bird", "year": 2022}` | 201 |
| `{"ids": {"simkl": 1624792}}` | 201 |
| `{"ids": {"simkl_id": 1624792}}` | 201, body `{"id": 0}` |

The last row is why a status code is not read as success here. `GET /search/tv`
*returns* the id under the key `simkl_id` and the scrobble endpoints expect
`simkl`; handing the one straight to the other is answered 201 with no show, and
nothing is recorded. Only the body tells them apart, so a scrobble counts as
having landed only when SIMKL echoes a non-zero `id`.

Requests are serialised behind a lock, because SIMKL holds a 20-second lock per
user and answers overlapping calls with 429. A 429 is dropped rather than
retried — by the time a retry landed it would describe a moment that has passed
— and a 409 on a `stop` means SIMKL already recorded that episode within the
hour, which is success.

### What it announces

A scrobble that reaches SIMKL's history fires `sonos_apple_music_watched`,
carrying the show, its numbering, the progress it finished at, the SIMKL id it
resolved to and the player it was watched on. Only that moment is announced:
starting and pausing are reported to SIMKL and nothing else.

Whether an episode counts as watched is SIMKL's rule, applied on their side —
`/scrobble/stop` marks it at 80% and saves a resume point below that — so the
scrobble path itself carries no threshold. Announcing it does need one, because
the response does not say which of the two SIMKL did, so `watch.marks_watched`
mirrors the published 80 with nothing keeping the two in step. If SIMKL moves
that number, this is the line that has to move with it. The event does not fire
for a 409: that episode reached the history through some other call.

`logbook.py` describes the event, so it reads as *SIMKL — marked Black Bird
S01E04 watched* on the Apple TV's **own** logbook timeline. Asking a player what
it has been playing is what its logbook is for, which is why this integration
can surface its work without adding an entity to hold it.

For a dashboard, a trigger-based template sensor needs nothing from here:

```yaml
template:
  - trigger:
      - trigger: event
        event_type: sonos_apple_music_watched
    sensor:
      - name: Last watched
        state: >-
          {{ trigger.event.data.show }}
          S{{ '%02d' % trigger.event.data.season }}E{{ '%02d' % trigger.event.data.episode }}
```

## Where it patches

| Integration | Concern | Seam |
|---|---|---|
| `sonos` | Browse | `sonos.media_browser.async_browse_media`, on the module |
| `sonos` | Play, search | `SonosMediaPlayerEntity.async_play_media` / `.async_search_media`, on the class |
| `apple_tv` | Play | `AppleTvMediaPlayer.async_play_media`, on the class |

Scrobbling appears in no row of that table. It only reads states Home Assistant
already publishes, so it subscribes rather than patching, and it finds its
players through the entity registry rather than through the loaded `apple_tv`
entries — which lets the two integrations start in either order, and an Apple TV
added later be picked up without a restart.

The entity looks the browse function up as a module attribute on every call, and
`root_payload` recurses through the same global, so one module-level patch covers
both. Patching browse on the entity *as well* grafts the Apple Music node twice.

`root_payload` returns its single child directly when only one source exists, so
the graft rebuilds a root in that case rather than appending to whatever that
child happened to be.

Browse is not patched on the Apple TV at all, because its entity already renders
the Jellyfin tree. The finished `infuse://` URL goes back through the original
method as `MediaType.URL`, which upstream answers with `apps.launch_app` — the
call that opens a deep link on tvOS — so this graft stays off pyatv's API
surface entirely, and has one thing to keep working rather than two.

If an expected attribute is missing — an upstream refactor, or an integration
never set up so its requirement is absent — that graft is skipped and the
integration it wraps is left untouched. A broken add-on must not take the
speakers or the television down with it.

Search needs `can_search=True` on the Apple Music nodes. The frontend shows its
search input only on non-root pages that opt in, and Sonos sets the flag nowhere,
which is why its browser otherwise has no search box at all.

## Diagnostics

The entry's **Download diagnostics** button answers the questions that otherwise
need a shell on the Home Assistant host: whether each graft installed, whether
the developer token scrape is holding and until when, which account serial the
last browse read from a favorite, which players are being watched, and which
SIMKL id each show resolved to.

That last one is the failure this integration cannot see from the outside. A
wrong id is accepted by SIMKL and the episode is filed under whichever show that
id names, so the cache it came from is the only place the mistake is visible.

Both tokens are redacted, so the file can go into an issue as it is. An entry
that failed to set up still reports — it is the one most worth asking about, and
what it was configured with is the answer it has.

## Testing

```
pip install -r requirements_test.txt
pytest
ruff check .
```

Python 3.14 or newer: `pytest-homeassistant-custom-component` needs it, and on
3.13 pip resolves to a much older release instead of failing.

`requirements_test.txt` also carries the Sonos integration's own requirements
and those of the components it imports at module scope (`ssdp`, and the
`spotify` and `plex` integrations that `sonos/media_player.py` imports
directly), plus `pyatv` for the Apple TV. The patch tests import the real Sonos
and apple_tv modules rather than stubs, so an upstream rename fails in CI
instead of in someone's living room.

Neither `soco` nor `pyatv` may be imported at module scope by this integration:
Home Assistant imports the package to reach the config flow, long before either
graft can decline to patch anything, so an import there turns "Sonos is not set
up yet" into an integration that cannot even be added. A test blocks each in a
subprocess to hold that line.

The URI and DIDL expectations are pinned as literals taken from a live household
rather than rebuilt from the templates the code uses, so a template edit cannot
pass its own mistake. `soco` is pinned to the version Home Assistant ships,
because those expectations are only meaningful against the serialiser a speaker
actually receives bytes from.

`tests/test_browse_ids.py` walks the AST and asserts every content-id kind
`browse.py` can mint is dispatched in `_async_browse`. A minted kind with no
handler renders normally and dead-ends only when opened.

Nothing in the suite touches the network or a speaker. Verifying a change
against real hardware means running somewhere on the speakers' own network —
a general-purpose build host will not reach them.
