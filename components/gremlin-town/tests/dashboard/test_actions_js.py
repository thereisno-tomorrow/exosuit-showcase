"""Browser retry semantics with a synthetic DOM and fetch; never a network call."""
from pathlib import Path
import subprocess


def test_action_reply_loss_and_undo_receipts():
    component = Path(__file__).resolve().parents[2]
    result = subprocess.run(["node", str(Path(__file__).with_name("actions-regression.cjs")),
        str(component / "src/gremlin_town/dashboard/static/actions.js")],
        # Allow for cold Node startup on shared Windows runners. The four
        # assertions below still have to complete; a hang remains bounded.
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS ") == 4
