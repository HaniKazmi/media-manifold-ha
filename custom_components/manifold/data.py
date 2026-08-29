"""The runtime state of one config entry.

One record rather than a bare client, so that everything a running entry knows
has a single home: the patched Sonos methods reach the Apple Music client
through it, and diagnostics reads all of it from the same place.

The annotations are deferred, so nothing here imports at runtime and this module
can be imported from anywhere in the integration without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .applemusic.api import AppleMusicClient
    from .simkl.watch import Scrobbler


@dataclass
class RuntimeData:
    """What one loaded entry holds."""

    apple_music: AppleMusicClient

    # Graft name -> whether it installed. Which integrations were actually
    # extended is otherwise knowable only from a log line at setup.
    grafted: dict[str, bool] = field(default_factory=dict)

    # None when SIMKL is not linked, which is also when nothing is subscribed.
    scrobbler: Scrobbler | None = None

    # The household's Apple Music serial as the last play read it from a
    # favorite, or None before one has happened. Kept because a wrong serial
    # enqueues items that never play, and nothing else records which was used.
    account_serial: int | None = None
