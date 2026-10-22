"""One synthetic model conversation; reports independent fixture evidence.

Run only in the practice container with a practice-only marker and start binding.
No token values or provider subprocess output are returned to the operator.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

PROMPT = """This is a synthetic upgrade practice, not a real personal request.
Do these steps in order using tools. Save this exact practice-only task in Brain:
"Practice fixture: check the purple lantern 42." Use owner/direct words, personal
scope, kind item, and operation_id practice-purple-lantern-42. Wait for the receipt
and report its record ID. Then search Brain for purple lantern 42 and read back
the stored words. Next this is the numbered morning-review answer: item 1 is
that saved fixture record, and my answer is 'pick today'. Record the answer with
the review response tool, using that exact record ID. Finally obtain a weekly
overview of the fixture items using an available Brain tool. Discover a suitable
rare tool through the more-tools front; do not substitute your own summary.
Do not use terminal, network, messaging, or file tools. Report any failure;
never invent a saved receipt. All data is synthetic practice data.
"""


def main():
    home = Path(os.environ["HERMES_HOME"])
    if json.loads((home / "practice-only.json").read_text()).get("practice_only") is not True:
        raise RuntimeError("practice marker required")
    name = os.environ["PRACTICE_CONTAINER_NAME"]
    for _ in range(100):
        path = home / "practice-starts.json"
        if path.is_file() and json.loads(path.read_text()).get(name):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("container-start binding not supplied")
    prompt = PROMPT.replace("operation_id practice-purple-lantern-42", "operation_id " + name)
    done = subprocess.run(["hermes", "-z", prompt, "--provider", "openai-codex", "--model", "gpt-6.1-sol"],
                          capture_output=True, text=True, timeout=300, cwd=home)
    # Retain synthetic-only response privately for review. Never send it to chat.
    (home / (name + "-response.txt")).write_text(done.stdout, encoding="utf-8")
    rows = [json.loads(line) for line in (home / "practice-observer.jsonl").read_text().splitlines()] if (home / "practice-observer.jsonl").is_file() else []
    rows = [r for r in rows if r.get("container_name") == name]
    requests = [r for r in rows if r.get("event") == "llm_execution"]
    front = [r for r in rows if r.get("event") == "tool_request" and r.get("front_target") == "bc_weekly" and r.get("front_action") == "call"]
    result = {"model_exit_code": done.returncode, "observed_model_requests": len(requests),
              "full_tool_budget_pass": bool(requests) and all(r["within_budget"] for r in requests),
              "total_chars": max((r["total_chars"] for r in requests), default=0),
              "largest_chars": max((r["largest_chars"] for r in requests), default=0),
              "byte_stable_schemas": bool(requests) and len({r["schemas_sha256"] for r in requests}) == 1,
              "rare_weekly_call_attempts": len(front), "private_output_suppressed": True}
    print(json.dumps(result))
    if done.returncode or not result["full_tool_budget_pass"] or not result["byte_stable_schemas"] or len(front) != 1:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
