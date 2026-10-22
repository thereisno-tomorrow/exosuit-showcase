from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("script, marker", [("ask_ui.cjs", "Ask UI contracts passed"),
                                            ("ask_reload.cjs", "Ask reload contracts passed")])
def test_ask_ui_retains_uncertain_request_and_renders_plain_text(script, marker):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    assert node, "Node is required for the dashboard UI contract test"
    result = subprocess.run([node, str(Path(__file__).with_name(script)),
                             str(root / "src/gremlin_town/dashboard/static/ask.js")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout
