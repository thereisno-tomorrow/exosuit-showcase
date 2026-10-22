"""W15 same-day on/off replay harness. No built-in network or live runner.

Use injected fresh-chat/run/cleanup adapters only after the recorded install gate.
The CLI reads fixture observations and prints measurements; it cannot call Diablo,
change config, archive records or touch calendar state. Outcome grading is separate
from the doer's answer. Missing measurements fail loudly, never become zero.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str


PROMPTS = tuple(Prompt(id, text) for id, text in [('R01', 'What is the next step for the exhibition permit?'), ('R02', 'What do I need before the gallery opens?'), ('R03', 'Did we finish the display-stand reservation?'), ('R04', 'Which print proof still needs review?'), ('R05', 'Remind me which projector cable we need.'), ('R06', 'Was there an editing tool subscription to check?'), ('R07', 'What exhibition work is planned today?'), ('R08', 'I am visiting the workshop. What can I collect?'), ('R09', 'Which checklist item fits fifteen minutes?'), ('R10', 'How is the exhibition preparation going?'), ('R11', 'What remains on the launch checklist?'), ('R12', 'Did we order the signboard already?'), ('R13', 'Which demo adapter should I investigate?'), ('R14', 'What exhibition deadlines are due this week?'), ('R15', 'How should I structure a product case study?'), ('S01', 'We need to renew the exhibition permit before December.'), ('S02', 'Remind me to call the print shop next Tuesday after lunch.'), ('S03', 'The workshop served excellent sandwiches today.'), ('S04', 'Casey will send the display dimensions by Friday.'), ('S05', 'I promised Morgan the exhibition photos this weekend.'), ('S06', 'Maybe someday I should learn screen printing.'), ('S07', 'Forwarded from Riley: please send the sample layout by Thursday.'), ('S08', 'Which brush shape is useful for watercolor?')])


class ReplayDriver(Protocol):
    """Future authorised runner seam; fixture tests use a fake implementing this.

    Each fresh chat receives one fixed setting and the same model/day. run must
    return an observation and a per-chat agent-log measurement, plus created IDs
    from that chat's write journal. cleanup archives only those IDs through the
    authorised owner operation; this module implements no archival or transport.
    """
    def fresh_chat(self, *, core_rules: bool, model: str, day: str) -> str: ...
    def run(self, session: str, prompt: Prompt) -> Mapping[str, Any]: ...
    def cleanup(self, record_ids: tuple[str, ...]) -> None: ...


def measurements(observation: Mapping[str, Any]) -> dict[str, int]:
    """Consume explicitly measured per-chat log counters, never guess from prose."""
    counts = observation.get("agent_log_counts")
    if not isinstance(counts, Mapping):
        raise ValueError("agent_log_counts is required from the per-chat log reader")
    answer = {}
    for name in ("skill_loads", "model_calls"):
        value = counts.get(name)
        if type(value) is not int or value < 0:
            raise ValueError(f"agent_log_counts.{name} must be a non-negative measured integer")
        answer[name] = value
    return answer


def run_replay(driver: ReplayDriver, *, model: str, day: str,
               judge: Callable[[Prompt, Mapping[str, Any]], bool],
               prompts: tuple[Prompt, ...] = PROMPTS, repeats: int = 3) -> list[dict[str, Any]]:
    """Run each prompt three times on/off in fresh chats on the same model/day.

    The caller must establish calendar-off/placement-paused and record-cleanup
    authority before providing a runner. There is no API URL or default runner.
    Cleanup happens after every chat, including a failed judge/measurement; the
    next chat never starts after cleanup fails. Output is measurements, not logs,
    messages or canonical records. S03/S08 remain controls graded by the caller.
    """
    if not model or not day or repeats < 1:
        raise ValueError("one fixed model/day and positive repeat count are required")
    results = []
    seen_sessions = set()
    for core_rules in (False, True):
        for repetition in range(repeats):
            for prompt in prompts:
                session = driver.fresh_chat(core_rules=core_rules, model=model, day=day)
                if not session or session in seen_sessions:
                    raise ValueError("each prompt needs a distinct fresh chat")
                seen_sessions.add(session)
                observation = driver.run(session, prompt)
                ids = observation.get("record_ids")
                if not isinstance(ids, list) or any(not isinstance(id, str) or not id for id in ids):
                    raise ValueError("record_ids must come from this chat's write journal")
                try:
                    counts = measurements(observation)
                    correct = judge(prompt, observation)
                    if type(correct) is not bool:
                        raise ValueError("independent judge must return a boolean")
                    results.append({"prompt": prompt.id, "core_rules": core_rules,
                                    "repetition": repetition + 1, "correct": correct, **counts})
                finally:
                    driver.cleanup(tuple(ids))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True,
                        help="fixture observations only; no live endpoint exists")
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if not isinstance(fixture, list):
        raise ValueError("fixture must be a list of per-chat observations")
    print(json.dumps([measurements(row) for row in fixture], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
