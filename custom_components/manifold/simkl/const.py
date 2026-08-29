"""Constants for scrobbling Apple TV playback to SIMKL."""

from __future__ import annotations

from typing import Final

from ..const import DOMAIN

CONF_SIMKL_TOKEN: Final = "simkl_token"

API_BASE: Final = "https://api.simkl.com"

# SIMKL's PIN flow authenticates the application with the client id alone — no
# secret is involved anywhere in this integration's use of the API — so the id
# is a public identifier and ships here rather than being asked for.
CLIENT_ID: Final = "f51dc38ce80b876ccc8637ceaf747b083f6daa98257b65e0a505d108b852fb31"
APP_NAME: Final = "ha-appletv-scrobbler"
APP_VERSION: Final = "1.0"

# Every call carries these, authenticated or not; SIMKL answers 401 without them.
QUERY: Final = {
    "client_id": CLIENT_ID,
    "app-name": APP_NAME,
    "app-version": APP_VERSION,
}

# Where the user approves the code the PIN flow prints.
PIN_URL: Final = "https://simkl.com/pin"

# Fired when an episode reaches SIMKL's history, carrying the show, its
# numbering, the progress it finished at and the player it was watched on.
# Built from the domain so a rename cannot leave the two out of step.
EVENT_WATCHED: Final = f"{DOMAIN}_watched"

# Raised when SIMKL stops accepting the stored token. Named here because the
# translation in strings.json is keyed on it.
TOKEN_ISSUE: Final = "simkl_token_rejected"

# SIMKL's own published rule: a `stop` at or above this marks the episode
# watched, and below it saves a resumable playback instead. The rule is applied
# on their side, so nothing in the scrobble path consults this. It is mirrored
# here only so `watch.marks_watched` can announce the same episodes SIMKL
# counts — a mirror with nothing keeping it in step, which is the price of the
# scrobble response not saying which of the two it did.
WATCHED_AT: Final = 80
