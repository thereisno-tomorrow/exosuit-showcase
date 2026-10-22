"""`deploy/telegram-courier.sh`: posts the spooled review and returns to Telegram (step E §2).

Runs the real script with Git Bash's (or the system's) `bash`, against a fake `docker` that
records each call and the exact bytes piped to it and answers like `hermes send --json`.
Nothing here reaches Docker, Hermes or Telegram. Skipped on a machine without bash.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gremlin_town.review.spool import write_message

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "telegram-courier.sh"
HERMES_SEND = ["exec", "-i", "-u", "1000", "hermes-wa", "hermes", "send", "--to", "telegram", "--json", "--file", "-"]
LEDGER_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# Shaped like a bot token (<digits>:<secret>) so the redaction must catch it, but built here and
# plainly fake, so no secret scan mistakes this file for a leak.
FAKE_TOKEN = "0" * 9 + ":" + "fake-not-a-token-" * 2

# FAKE_MODE is a comma-separated mode per call; the last one repeats.
FAKE_DOCKER = r"""#!/usr/bin/env bash
n=$(( $(find "$FAKE_DIR" -maxdepth 1 -name 'call-*.args' | wc -l) + 1 ))
printf '%s\n' "$@" >"$FAKE_DIR/call-$n.args"
cat >"$FAKE_DIR/call-$n.stdin"
IFS=, read -ra modes <<<"${FAKE_MODE:-success}"
mode=${modes[n-1]:-${modes[-1]}}
case $mode in
  success) printf '{"success": true, "message_id": "%s"}\n' "$((264 + n))" ;;
  noisy) printf 'Connecting to telegram\n{"success": true, "message_id": %s}\n' "$((264 + n))" ;;
  no-id) printf '{"success": true}\n' ;;
  refused) printf '{"success": false, "error": "Telegram said no"}\n'; exit 1 ;;
  leaky) echo "POST https://api.telegram.org/bot$FAKE_TOKEN/sendMessage failed" >&2; exit 1 ;;
  garbage) echo "not json at all" ;;
esac
"""


def _bash() -> str:
    if os.name == "nt":
        path = Path("C:/Program Files/Git/bin/bash.exe")
        if path.exists():
            return str(path)
    path = shutil.which("bash")
    if path is None:
        pytest.skip("bash is needed to run the courier script")
    return path


def _write_script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class Courier:
    def __init__(self, root: Path) -> None:
        self.bash = _bash()
        self.spool = root / "spool"
        self.spool.mkdir()
        self.calls = root / "calls"
        self.calls.mkdir()
        tools = root / "bin"
        tools.mkdir()
        self.docker = tools / "docker"
        _write_script(self.docker, FAKE_DOCKER)
        # Git Bash has no flock; one courier at a time is the hub's concern, not this test's.
        _write_script(tools / "flock", "#!/usr/bin/env bash\nexit 0\n")
        system = [Path(self.bash).parent.parent / "usr" / "bin"] if os.name == "nt" else [Path("/usr/bin"), Path("/bin")]
        self.path = os.pathsep.join(str(p) for p in [tools, *system])

    def run(self, mode: str = "success", spool: Path | None = None, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update({
            "PATH": self.path,
            "COURIER_DOCKER": self.docker.as_posix(),
            "COURIER_PYTHON": Path(sys.executable).as_posix(),
            "FAKE_DIR": self.calls.as_posix(),
            "FAKE_MODE": mode,
            "FAKE_TOKEN": FAKE_TOKEN,
            **overrides,
        })
        return subprocess.run(
            [self.bash, "--noprofile", "--norc", SCRIPT.as_posix(), (spool or self.spool).as_posix()],
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )

    def message(self, name: str, text: str, age_seconds: float = 0) -> Path:
        path = self.spool / name
        path.write_bytes(text.encode("utf-8"))
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def call(self, n: int) -> tuple[list[str], bytes]:
        args = (self.calls / f"call-{n}.args").read_text(encoding="utf-8").splitlines()
        return args, (self.calls / f"call-{n}.stdin").read_bytes()

    def call_count(self) -> int:
        return len(list(self.calls.glob("call-*.args")))

    def ledger(self) -> list[list[str]]:
        path = self.spool / "sent" / "ledger.tsv"
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def courier(tmp_path: Path) -> Courier:
    return Courier(tmp_path)


def test_script_is_checked_out_with_lf_line_endings() -> None:
    assert b"\r\n" not in SCRIPT.read_bytes()  # a CRLF copy breaks bash here and on the hub


def test_success_sends_oldest_first_moves_the_file_and_records_the_message_id(courier: Courier) -> None:
    review = "Today\n1. Send the invoice — Acme (due today)\n"
    moment = "1. File the form — picked today"
    courier.message("review-2026-09-29.txt", review, age_seconds=120)
    courier.message("moment-pc_on-hermes-wa-pc_on-7.txt", moment, age_seconds=60)
    (courier.spool / ".review-2026-09-30.x1y2.tmp").write_text("half-written", encoding="utf-8")
    (courier.spool / ".hidden.txt").write_text("not the courier's", encoding="utf-8")

    result = courier.run()

    assert result.returncode == 0, result.stderr
    assert courier.call(1) == (HERMES_SEND, review.encode("utf-8"))  # the exact bytes, oldest first
    assert courier.call(2) == (HERMES_SEND, moment.encode("utf-8"))
    assert courier.call_count() == 2  # hidden and temporary files are never sent
    sent = courier.spool / "sent"
    assert (sent / "review-2026-09-29.txt").read_text(encoding="utf-8") == review
    assert (sent / "moment-pc_on-hermes-wa-pc_on-7.txt").read_text(encoding="utf-8") == moment
    assert sorted(p.name for p in courier.spool.glob("*.txt")) == [".hidden.txt"]
    ledger = courier.ledger()
    assert [row[1:] for row in ledger] == [["review-2026-09-29.txt", "265"], ["moment-pc_on-hermes-wa-pc_on-7.txt", "266"]]
    assert all(LEDGER_TIME.fullmatch(row[0]) for row in ledger)
    assert "sent review-2026-09-29.txt as Telegram message 265" in result.stderr

    # Nothing left to send: the next run sends nothing and still succeeds.
    assert courier.run().returncode == 0
    assert courier.call_count() == 2


def test_a_failed_send_stays_for_the_next_run_while_the_rest_still_go(courier: Courier) -> None:
    courier.message("review-2026-09-29.txt", "the review", age_seconds=120)
    courier.message("moment-pc_on-hermes-wa-pc_on-7.txt", "the return", age_seconds=60)

    first = courier.run(mode="refused,success")

    assert first.returncode == 1  # the unit shows failed
    assert "not sent, will retry: review-2026-09-29.txt" in first.stderr
    assert "Telegram said no" in first.stderr
    assert (courier.spool / "review-2026-09-29.txt").read_text(encoding="utf-8") == "the review"
    assert [row[1:] for row in courier.ledger()] == [["moment-pc_on-hermes-wa-pc_on-7.txt", "266"]]

    second = courier.run(mode="success")  # the next run retries it
    assert second.returncode == 0, second.stderr
    assert not (courier.spool / "review-2026-09-29.txt").exists()
    assert [row[1] for row in courier.ledger()] == ["moment-pc_on-hermes-wa-pc_on-7.txt", "review-2026-09-29.txt"]


@pytest.mark.parametrize(
    ("mode", "sent_as"),
    [("noisy", "265"), ("no-id", "unknown"), ("garbage", None)],
)
def test_only_a_success_result_counts_as_sent(courier: Courier, mode: str, sent_as: str | None) -> None:
    courier.message("review-2026-09-29.txt", "the review")

    result = courier.run(mode=mode)

    if sent_as is None:
        assert result.returncode == 1
        assert "no JSON result from hermes send" in result.stderr
        assert (courier.spool / "review-2026-09-29.txt").exists()
        assert courier.ledger() == []
    else:
        assert result.returncode == 0, result.stderr
        assert [row[1:] for row in courier.ledger()] == [["review-2026-09-29.txt", sent_as]]


def test_a_bot_token_in_an_error_never_reaches_the_journal(courier: Courier) -> None:
    courier.message("review-2026-09-29.txt", "the review")

    result = courier.run(mode="leaky")

    assert result.returncode == 1
    assert "<redacted>" in result.stderr
    assert FAKE_TOKEN not in result.stderr and "fake-not-a-token" not in result.stderr


def test_sent_files_older_than_30_days_are_pruned_and_the_ledger_kept(courier: Courier) -> None:
    sent = courier.spool / "sent"
    sent.mkdir()
    old_stamp = time.time() - 40 * 86400
    recent_stamp = time.time() - 5 * 86400
    for name, stamp in (("old.txt", old_stamp), ("recent.txt", recent_stamp), ("ledger.tsv", old_stamp)):
        (sent / name).write_text(name, encoding="utf-8")
        os.utime(sent / name, (stamp, stamp))

    result = courier.run()

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in sent.iterdir()) == ["ledger.tsv", "recent.txt"]
    assert courier.call_count() == 0


def test_container_and_target_come_from_the_environment_for_tests(courier: Courier) -> None:
    courier.message("review-2026-09-29.txt", "the review")

    result = courier.run(COURIER_CONTAINER="hermes-test", COURIER_TARGET="telegram:12345")

    assert result.returncode == 0, result.stderr
    args, _ = courier.call(1)
    assert args == ["exec", "-i", "-u", "1000", "hermes-test", "hermes", "send", "--to", "telegram:12345",
                    "--json", "--file", "-"]


def test_a_missing_spool_folder_fails_loudly(courier: Courier, tmp_path: Path) -> None:
    result = courier.run(spool=tmp_path / "no-such-spool")

    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert courier.call_count() == 0


def test_gremlin_town_then_the_courier_then_a_replay_sends_once(courier: Courier) -> None:
    """GT's write, the courier's move to sent/, then a replayed GT write: one Telegram message."""

    text = "Nothing due, nothing new. Calendar is clear."
    assert write_message(str(courier.spool), "review-2026-09-29", text) is not None

    assert courier.run().returncode == 0
    assert courier.call(1) == (HERMES_SEND, text.encode("utf-8"))

    assert write_message(str(courier.spool), "review-2026-09-29", text) is None
    assert courier.run().returncode == 0
    assert courier.call_count() == 1
