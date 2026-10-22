"""damien-map.py NEW_COMMIT LIVE_COMMIT OUT_PREFIX

Compare Diablo's two install manifests at NEW_COMMIT with LIVE_COMMIT (what is installed now) and write:
  OUT_PREFIX.map  lines "<sha256> <path in a git archive> <destination under /srv/hermes-example>" for every
                  pinned file whose pin changed
  OUT_PREFIX.pre  a `sha256sum -c` file of those destinations' live pins (what must be there before)
  OUT_PREFIX.paths the archive paths, one per line, for `git archive`
Run from anywhere inside the repo. Destinations come from the manifests (/opt/data is the container's view of
/srv/hermes-example). Sources are relative to components/gremlin-town in the manifests.
"""

import json
import subprocess
import sys

NEW, LIVE, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
MANIFESTS = (
    "components/gremlin-town/adapters/hermes/install-manifest.json",
    "components/gremlin-town/adapters/hermes/memory_brain/install-manifest.json",
)


def pins(commit: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for manifest in MANIFESTS:
        data = json.loads(subprocess.run(["git", "show", f"{commit}:{manifest}"], capture_output=True, check=True).stdout)
        for section in ("files", "skill_files"):
            for entry in data.get(section, []):
                if "sha256" not in entry:
                    continue
                dest = entry["destination"]
                if not dest.startswith("/opt/data/"):
                    sys.exit(f"unexpected destination {dest}")
                out[dest[len("/opt/data/"):]] = ("components/gremlin-town/" + entry["source"], entry["sha256"])
    return out


new, live = pins(NEW), pins(LIVE)
changed = sorted(dest for dest in new if new[dest][1] != live.get(dest, (None, None))[1])
with open(OUT + ".map", "w", newline="\n") as fh:
    for dest in changed:
        fh.write(f"{new[dest][1]} {new[dest][0]} {dest}\n")
with open(OUT + ".pre", "w", newline="\n") as fh:
    for dest in changed:
        if dest in live:
            fh.write(f"{live[dest][1]}  {dest}\n")
with open(OUT + ".paths", "w", newline="\n") as fh:
    for src in sorted({new[dest][0] for dest in changed}):
        fh.write(src + "\n")
for dest in changed:
    print(f"{dest}: {live.get(dest, ('', 'new file'))[1][:12]} -> {new[dest][1][:12]} (from {new[dest][0]})")
