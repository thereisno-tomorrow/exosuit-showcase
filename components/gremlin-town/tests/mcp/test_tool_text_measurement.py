"""Work-model 8.5 budgets replace dated exact pins/bands intentionally.

Only upper bounds are enforced. Smaller descriptions pass. Feature registration
still needs behavioural proof, with no size pins. W0 reply caps stay frozen for
W13/W16 and are not recalculated here.
"""

from copy import deepcopy
import json
from unittest.mock import patch

from tests.mcp import tool_text_measurement as measurement
from tests.mcp.reply_caps import REPLY_CAPS, measure_reply_chars


def test_tier_a_within_63000():
    assert measurement.tier_a_totals().total_chars <= 63_000, measurement.format_report()


def test_tier_b_within_45000():
    assert measurement.tier_b_totals().total_chars <= 45_000, measurement.format_report()


def test_every_tool_within_12000():
    assert all(chars <= 12_000 for chars in measurement.tool_sizes().values()), measurement.format_report()


def test_core_rules_block_within_8000():
    assert measurement.core_rules_chars() <= 8_000


def test_gated_tools_register_only_when_on():
    from adapters.hermes import plugin
    from tests.adapters.test_hermes import FakeHubClient, LoaderContext

    all_names = {t.name for t in plugin.ALL_TOOL_DEFINITIONS}
    gated = plugin.OPTIONAL_TOOL_FEATURES
    assert gated.keys() <= all_names
    for enabled in (frozenset(), plugin.KNOWN_OPTIONAL_FEATURES,
                    *(frozenset({feature}) for feature in sorted(plugin.KNOWN_OPTIONAL_FEATURES))):
        context = LoaderContext()
        with patch.object(plugin, "_enabled_optional_features", return_value=enabled):
            plugin.register(context, client=FakeHubClient())
        names = {t["name"] for t in context.registrations}
        expected = {name for name in all_names if name not in gated or gated[name] in enabled}
        assert names == expected
        assert len(names) == len(context.registrations)
    default_mcp = {t["name"] for t in measurement.mcp_tool_entries()}
    all_mcp = {t["name"] for t in measurement.mcp_tool_entries(True)}
    orientation = {"bc_orient", "bc_next", "bc_next_respond", "bc_agenda", "bc_weekly"}
    assert orientation <= all_mcp
    assert default_mcp.isdisjoint(orientation)
    assert all_mcp.isdisjoint({"bc_work", "bc_project_save", "bc_calendar_remove"})
    assert default_mcp <= all_mcp


def test_a_cut_never_fails(monkeypatch):
    before_a, before_b = measurement.tier_a_totals().total_chars, measurement.tier_b_totals().total_chars
    original_mcp, original_diablo = measurement.mcp_tool_entries, measurement.hermes_tool_entries

    def shorter(builder):
        def entries(all_features=False):
            result = deepcopy(builder(all_features))
            for item in result:
                item["description"] = (item["description"] or "")[:1]
            return result
        return entries

    monkeypatch.setattr(measurement, "mcp_tool_entries", shorter(original_mcp))
    monkeypatch.setattr(measurement, "hermes_tool_entries", shorter(original_diablo))
    assert measurement.tier_a_totals().total_chars < before_a
    assert measurement.tier_b_totals().total_chars < before_b
    # Run actual budget assertions, detecting reintroduced lower bounds/pins.
    test_tier_a_within_63000()
    test_tier_b_within_45000()
    test_every_tool_within_12000()
    test_core_rules_block_within_8000()


def test_default_replies_within_frozen_w0_caps(tmp_path):
    for name, chars in measure_reply_chars(tmp_path).items():
        assert chars <= REPLY_CAPS[name], f"{name}: {chars} > {REPLY_CAPS[name]}"


def test_report_keeps_next_action_apart_and_five_largest_tools():
    report = measurement.format_report()
    assert "Tier A:" in report and "Tier B:" in report
    assert "next-action (apart, excluded from tier A):" in report
    assert len([line for line in report.splitlines() if line.startswith("  ")]) == 5


def test_snapshot_files_are_independent_baseline_inputs():
    # Historical inputs for W1; do not repin them as future features grow.
    root = measurement.REPOSITORY_ROOT / "tests/mcp/work_model_snapshots"
    paths = sorted(root.glob("*.json"))
    assert len(paths) == 4
    for path in paths:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert snapshot["source_base"] == "d8d566d"
        tools = snapshot["tools"]
        assert tools and len({t["name"] for t in tools}) == len(tools)
        assert all(set(t) == {"name", "description", "input_schema"} for t in tools)
