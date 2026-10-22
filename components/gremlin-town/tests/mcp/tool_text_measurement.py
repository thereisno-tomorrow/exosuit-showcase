"""Work-model 8.5: upper budgets for text each client reads.

Tier A: every-feature Diablo tools, core/review skills, provider core rules on.
Tier B: every-feature MCP tools. next-action is reported apart. Absent future
pieces count zero. Dated pins/bands intentionally go; making text shorter passes.
A tool is len(description) + len(json.dumps(schema, ensure_ascii=False)), the
original W0 measure: characters, not envelope/wire bytes.
Run: uv run python tests/mcp/tool_text_measurement.py, from Gremlin Town.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPOSITORY_ROOT / "adapters/hermes/skills"
SKILL_MD_PATH = SKILLS_ROOT / "gremlin-town/SKILL.md"
REVIEW_SKILL_MD_PATH = SKILLS_ROOT / "gremlin-town-review/SKILL.md"
NEXT_ACTION_SKILL_MD_PATH = SKILLS_ROOT / "next-action/SKILL.md"
for _source in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))


class ToolTextTotals(NamedTuple):
    tool_count: int
    description_chars: int
    schema_chars: int

    @property
    def total_chars(self) -> int:
        return self.description_chars + self.schema_chars


class TierATotals(NamedTuple):
    tools: ToolTextTotals
    core_skill_chars: int
    review_skill_chars: int
    provider_block_chars: int

    @property
    def total_chars(self) -> int:
        return self.tools.total_chars + self.core_skill_chars + self.review_skill_chars + self.provider_block_chars


def _totals(entries: list[dict]) -> ToolTextTotals:
    return ToolTextTotals(len(entries), sum(len(t["description"] or "") for t in entries),
                          sum(len(json.dumps(t["input_schema"], ensure_ascii=False)) for t in entries))


def mcp_tool_entries(all_features: bool = False) -> list[dict]:
    from gremlin_town.contracts import Actor
    from gremlin_town.mcp.brain_tools import build_brain_tools
    from gremlin_town.mcp.tools import build_tools

    async def never():
        raise AssertionError("schema inspection cannot authorize")

    tools = build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never,
                              calendar_removal=all_features, project_saves=all_features)
    tools += build_tools(object(), lambda: Actor("tool-text-measurement"))
    if all_features:
        from tests.adapters.test_hermes_brain import orientation_tools
        tools += orientation_tools()
    return [dict(name=t.name, description=t.description, input_schema=t.parameters) for t in tools]


def hermes_tool_entries(all_features: bool = False) -> list[dict]:
    """Inspect real registration with a fake client and explicit feature state."""
    from adapters.hermes import plugin
    from tests.adapters.test_hermes import FakeHubClient, LoaderContext

    context = LoaderContext()
    features = plugin.KNOWN_OPTIONAL_FEATURES if all_features else frozenset()
    with patch.object(plugin, "_enabled_optional_features", return_value=features):
        plugin.register(context, client=FakeHubClient())
    return [dict(name=t["name"], description=t["description"], input_schema=t["schema"]["parameters"])
            for t in context.registrations]


def mcp_tool_totals(all_features: bool = False) -> ToolTextTotals:
    return _totals(mcp_tool_entries(all_features))


def hermes_tool_totals(all_features: bool = False) -> ToolTextTotals:
    return _totals(hermes_tool_entries(all_features))


def _optional_chars(path: Path) -> int:
    return len(path.read_text(encoding="utf-8")) if path.is_file() else 0


def skill_md_chars() -> int:
    return len(SKILL_MD_PATH.read_text(encoding="utf-8"))


def _provider_block(core_rules: bool) -> str:
    from adapters.hermes.memory_brain import provider

    # Injected fake hub, no home/config lookup and no prefetch call.
    brain = provider.BrainProvider(hub=object())
    brain.initialize("w0-tool-text")
    if "core_rules" in provider.SETTING_KEYS:
        brain._options = replace(brain._options, core_rules=core_rules)
    return brain.system_prompt_block()


def core_rules_chars() -> int:
    """Added core rules only; absent before W15 means zero."""
    from adapters.hermes.memory_brain import provider
    if "core_rules" not in provider.SETTING_KEYS:
        return 0
    off, on = _provider_block(False), _provider_block(True)
    assert on.startswith(off), "core rules must add to the existing provider line"
    return len(on) - len(off)


def tier_a_totals() -> TierATotals:
    return TierATotals(hermes_tool_totals(True), skill_md_chars(),
                       _optional_chars(REVIEW_SKILL_MD_PATH), len(_provider_block(True)))


def tier_b_totals() -> ToolTextTotals:
    return mcp_tool_totals(True)


def tool_sizes() -> dict[str, int]:
    """All-feature tools on both surfaces, qualified to avoid colliding names."""
    return {f"{surface}:{t['name']}": len(t["description"] or "") +
            len(json.dumps(t["input_schema"], ensure_ascii=False))
            for surface, entries in (("diablo", hermes_tool_entries(True)), ("mcp", mcp_tool_entries(True)))
            for t in entries}


def combined_total_chars() -> int:
    """Legacy default-surface diagnostic; no longer a gate or a tier sum."""
    return mcp_tool_totals().total_chars + hermes_tool_totals().total_chars + skill_md_chars()


def format_report() -> str:
    a, b = tier_a_totals(), tier_b_totals()
    largest = sorted(tool_sizes().items(), key=lambda entry: (-entry[1], entry[0]))[:5]
    lines = [
        f"Tier A: {a.total_chars} / 63000 chars (Diablo all-feature tools {a.tools.tool_count}: {a.tools.total_chars}; "
        f"core skill {a.core_skill_chars}; review skill {a.review_skill_chars}; provider block {a.provider_block_chars})",
        f"Tier B: {b.total_chars} / 45000 chars (MCP all-feature tools {b.tool_count}; "
        f"description {b.description_chars} + schema {b.schema_chars})",
        f"Core rules: {core_rules_chars()} / 8000 chars",
        f"next-action (apart, excluded from tier A): {_optional_chars(NEXT_ACTION_SKILL_MD_PATH)} chars",
        "Five largest tools (description + schema; cap 12000 each):",
    ]
    lines += [f"  {name}: {chars}" for name, chars in largest]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report())
