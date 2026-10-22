"""Regenerate Diablo's plugin.py Brain tool copies (_BRAIN_DEFINITIONS) from the hub's live tools.

Run from a worktree's components/gremlin-town:
    uv run --frozen --extra dev python scripts/regen_plugin_defs.py

It keeps each entry's operation and order, takes description and parameters from build_brain_tools
(plus the orientation tools and bc_calendar_remove, which the hub registers only while they are on),
and writes the block with the same pprint width the file already uses
(it refuses if the current block cannot be reproduced first). Keeps the file's line endings.
A brand-new Brain tool is not added here: add its entry to the block by hand first, then rerun.
"""

import pathlib
import pprint
import sys

CRLF = b"\r\n"
LF = b"\n"

sys.path.insert(0, ".")
from adapters.hermes import plugin  # noqa: E402
from tests.adapters.test_hermes_brain import calendar_tools, orientation_tools, server_tools  # noqa: E402

path = pathlib.Path("adapters/hermes/plugin.py")
raw = path.read_bytes()
crlf = CRLF in raw
text = raw.replace(CRLF, LF).decode("utf-8")
start = text.index("_BRAIN_DEFINITIONS = [")
end = text.index("\nBRAIN_TOOL_DEFINITIONS = tuple(")
current = text[start + len("_BRAIN_DEFINITIONS = "):end]
existing = [
    {"operation": d.operation, "name": d.name, "description": d.description, "parameters": dict(d.parameters)}
    for d in plugin.BRAIN_TOOL_DEFINITIONS
]
width = next((w for w in range(60, 140) if pprint.pformat(existing, width=w, sort_dicts=False) == current), None)
if width is None:
    sys.exit("cannot reproduce the current _BRAIN_DEFINITIONS block; was it hand-edited? fix that first")
live = {t.name: t for t in (*server_tools(), *orientation_tools(), *calendar_tools())}
missing = [d["name"] for d in existing if d["name"] not in live]
if missing:
    sys.exit(f"plugin tools with no live hub tool: {missing}")
fresh = [
    {"operation": d["operation"], "name": d["name"], "description": live[d["name"]].description,
     "parameters": live[d["name"]].parameters}
    for d in existing
]
new_text = text[:start] + "_BRAIN_DEFINITIONS = " + pprint.pformat(fresh, width=width, sort_dicts=False) + text[end:]
out = new_text.encode("utf-8")
path.write_bytes(out.replace(LF, CRLF) if crlf else out)
changed = [f["name"] for f, e in zip(fresh, existing) if f != e]
print(f"width {width}; regenerated {len(fresh)} definitions; changed: {changed or 'none'}")
