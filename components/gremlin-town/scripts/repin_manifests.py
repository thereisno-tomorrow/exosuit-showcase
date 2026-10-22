"""Re-pin install-manifest SHA-256s to the LF bytes of each source file (what git stores and ships).

Run from a worktree's components/gremlin-town (paths in the manifests are relative to it):
    python scripts/repin_manifests.py [manifest ...]

With no arguments it re-pins every adapters/*/install-manifest.json and
adapters/hermes/memory_brain/install-manifest.json. Only "files" and "skill_files" entries are touched;
"preimage_sha256" maps record what is installed today and are left alone. Keeps each manifest's line
endings. Prints every change. The pin tests compare against HEAD, so they pass only after a commit.
"""

import hashlib
import json
import pathlib
import re
import sys

CRLF = b"\r\n"
LF = b"\n"


def lf_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(CRLF, LF)).hexdigest()


manifests = [pathlib.Path(p) for p in sys.argv[1:]] or sorted(
    set(pathlib.Path("adapters").glob("*/install-manifest.json"))
    | {pathlib.Path("adapters/hermes/memory_brain/install-manifest.json")}
)
for manifest in manifests:
    raw = manifest.read_bytes()
    crlf = CRLF in raw
    text = raw.replace(CRLF, LF).decode("utf-8")
    data = json.loads(text)
    for section in ("files", "skill_files"):
        for entry in data.get(section, []):
            source = pathlib.Path(entry["source"])
            if not source.is_file():
                sys.exit(f"{manifest}: source {source} is missing")
            new = lf_sha256(source)
            old = entry.get("sha256")
            if old is None:
                print(f"{manifest}: {source} has no sha256 pin; left alone")
                continue
            if old == new:
                continue
            pattern = re.compile(
                r'("source": "' + re.escape(entry["source"]) + r'",(?:\s*"[a-z_]+": "[^"]*",)*?\s*"sha256": ")'
                + re.escape(old) + '"'
            )
            text, count = pattern.subn(lambda m: m.group(1) + new + '"', text)
            if count != 1:
                sys.exit(f"{manifest}: could not re-pin {source} (matched {count})")
            print(f"{manifest}: {source} {old[:12]} -> {new[:12]}")
    json.loads(text)
    out = text.encode("utf-8")
    manifest.write_bytes(out.replace(LF, CRLF) if crlf else out)
print("done")
