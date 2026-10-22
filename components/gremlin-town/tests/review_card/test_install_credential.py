"""The preserved managed credential exception rejects writable lookalikes."""
import stat
import os
import pytest
from pathlib import Path
from types import SimpleNamespace
from gremlin_town.review_card import client

@pytest.mark.skipif(os.name != "posix", reason="systemd credential mounts use POSIX paths")
def test_managed_systemd_credential_requires_exact_readonly_mount(monkeypatch):
    path=Path('/run/credentials/gremlin-town-discord.service/discord_review_token')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(path.parent))
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(st_mode=stat.S_IFDIR | 0o550, st_uid=0, st_gid=0))
    monkeypatch.setattr(client.os, 'statvfs', lambda p: SimpleNamespace(f_flag=client.os.ST_RDONLY), raising=False)
    monkeypatch.setattr(client.os, 'ST_RDONLY', 1, raising=False)
    info=SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0, st_gid=0)
    assert client._systemd_credential(path, info)
    monkeypatch.setattr(client.os, 'statvfs', lambda p: SimpleNamespace(f_flag=0), raising=False)
    assert not client._systemd_credential(path, info)
    monkeypatch.setattr(client.os, 'statvfs', lambda p: SimpleNamespace(f_flag=client.os.ST_RDONLY), raising=False)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY','/tmp/lookalike')
    assert not client._systemd_credential(path, info)
