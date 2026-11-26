"""Print the requirements, pinned as the installed Home Assistant pins them, of
the integrations the tests import and of everything those load with them.

The tests import the real ``sonos`` and ``apple_tv`` modules, and those pull in
their dependencies' requirements at import time: sonos needs ssdp's
async-upnp-client and network's ifaddr, and imports plex and spotify, which are
its after_dependencies. A hand-written list of these drifts from whatever Home
Assistant release pip just picked, and one missing package fails collection
before any test runs. Reading the manifests of the installed release keeps the
list matched to it.

Manifests are read as files rather than through ``homeassistant.loader``:
importing Home Assistant here would need its own requirements present already.
"""

from importlib.util import find_spec
import json
from pathlib import Path

ROOTS = ("sonos", "apple_tv")

spec = find_spec("homeassistant")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit("homeassistant is not installed")
components = Path(spec.submodule_search_locations[0]) / "components"

seen: set[str] = set()
pending = list(ROOTS)
requirements: set[str] = set()
while pending:
    domain = pending.pop()
    if domain in seen:
        continue
    seen.add(domain)
    manifest = json.loads((components / domain / "manifest.json").read_text())
    requirements.update(manifest.get("requirements", []))
    pending += manifest.get("dependencies", [])
    pending += manifest.get("after_dependencies", [])

print("\n".join(sorted(requirements)))
