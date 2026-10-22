"""Create two isolated signing keys only after the operator's explicit approval.

Never invoked by the observer/checker. Refuses existing files and live paths.
Outputs names and custody only; no key values or hashes.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path


def create(root: Path):
    if os.name != "posix" or os.geteuid() != 0 or root.is_symlink():
        raise ValueError("hub practice root and root operator required")
    root = root.resolve()
    if root.parent != Path("/opt/hermes-upgrade") or not root.name.startswith("practice-"):
        raise ValueError("isolated practice root required")
    marker = root / "practice-only.json"
    if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()).get("practice_only") is not True:
        raise ValueError("explicit practice marker required")
    directory = root / "home/practice-secrets"
    if (root / "home").is_symlink() or directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
        raise ValueError("existing private practice directory required")
    metadata = directory.stat()
    if metadata.st_uid != 1000 or metadata.st_mode & 0o777 != 0o700:
        raise ValueError("invalid practice secret directory custody")
    paths = [directory / name for name in ("words-shadow.key", "wake-signing.key")]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("refusing existing signing key paths")
    for path in paths:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchown(descriptor, 1000, 1000)
            data = secrets.token_hex(32).encode()
            if os.write(descriptor, data) != len(data):
                raise OSError("incomplete key write; retained file requires review")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"practice_only": True, "created": [path.name for path in paths], "uid": 1000, "mode": "0600"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.root)))
