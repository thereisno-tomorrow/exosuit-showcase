"""Real host composition: local snapshot reads, with no placement in FlowSavvy mode."""
from datetime import UTC, datetime

import pytest

from gremlin_town.runtime import ConfigurationError, RUNTIME_CONFIG_SCHEMA, build_runtime
from gremlin_town.review.settings import OrientationSettings, OrientationSettingsError
from gremlin_town.transport import FakeTransport
from test_orient_cli import write_config, agentmail_key_env


def config_file(tmp_path, monkeypatch, *, schedule=False, flowsavvy=False, placement=False):
    import gremlin_town.runtime as host
    config = write_config(tmp_path,
        review_table=f'[review]\nenabled=true\nwhy_log_file="{(tmp_path / "why.db").as_posix()}"',
        orientation_table=f'[orientation]\nenabled=true\nblocks=true\nschedule={str(schedule).lower()}')
    if flowsavvy:
        # The host adapters are inert in this composition test; no real Brain credentials.
        monkeypatch.setattr(host, "_brain_mcp_settings", lambda *args: object())
        monkeypatch.setattr(host, "_orientation_brain_storage", lambda *args: None)
        monkeypatch.setattr(host, "_brain_home_owner", lambda *args: None)
        monkeypatch.setattr(host, "_cached_brain_storage", lambda *args: (None, None))
        with config.open("a") as stream:
            stream.write('\n[flowsavvy]\nenabled=true\ntoken_file="unused-secret"\n'
                         'state_file="schedule.db"\nlist_id="synthetic-list"\n'
                         'admin_hours_id="synthetic-admin"\ndeep_focus_hours_id="synthetic-focus"\n'
                         'errands_hours_id="synthetic-errands"\n')
    if placement:
        with config.open("a") as stream:
            stream.write('\n[placement]\nenabled=true\nwrite=true\n')
    return config


def test_schedule_setting_defaults_off_and_rejects_non_boolean():
    assert not OrientationSettings.from_table(None, review_enabled=False).schedule
    assert "schedule" in RUNTIME_CONFIG_SCHEMA["orientation"]
    with pytest.raises(OrientationSettingsError, match="schedule"):
        OrientationSettings.from_table({"schedule": "yes"}, review_enabled=True)


def test_reader_switch_requires_connector(tmp_path, monkeypatch):
    config = config_file(tmp_path, monkeypatch, schedule=True)
    with pytest.raises(ConfigurationError, match="schedule.*flowsavvy"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)


@pytest.mark.parametrize("schedule", [False, True])
def test_flowsavvy_disables_old_placement_and_block_controls(tmp_path, monkeypatch, schedule):
    config = config_file(tmp_path, monkeypatch, flowsavvy=True, placement=True, schedule=schedule)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.placement_context is None
        assert not runtime.orientation.blocks
        assert runtime.orientation_context.schedule_enabled is schedule
        reader = runtime.orientation_context.sources.schedule
        if schedule:
            assert reader is not None
            assert not reader(datetime.now(UTC)).available
            assert not (tmp_path / "schedule.db").exists()
        else:
            assert reader is None
    finally:
        runtime.close()


def test_disabled_connector_keeps_original_blocks(tmp_path, monkeypatch):
    config = config_file(tmp_path, monkeypatch)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.orientation.blocks
        assert not runtime.orientation_context.schedule_enabled
        assert runtime.orientation_context.sources.schedule is None
    finally:
        runtime.close()
