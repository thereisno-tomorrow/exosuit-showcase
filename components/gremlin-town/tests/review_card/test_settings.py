from dataclasses import replace

import pytest

from gremlin_town.review_card.settings import ALLOWED_KEYS, DiscordSettings, FLAGS
from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA


def configured(tmp_path):
    return DiscordSettings(enabled=True, card=True, owner_id="fictional-owner", owner_user_id="101",
        application_id="102", guild_id="103", morning_channel_id="104", home_channel_id="105",
        state_file=tmp_path / "state.sqlite", token_file=tmp_path / "credential")


def test_absent_table_has_no_enabled_features_or_file_reads(tmp_path):
    settings = DiscordSettings.from_table(None, base=tmp_path)
    assert not any(getattr(settings, key) for key in FLAGS)
    assert list(tmp_path.iterdir()) == []
    assert RUNTIME_CONFIG_SCHEMA["discord"] == ALLOWED_KEYS


@pytest.mark.parametrize("key", sorted(FLAGS))
def test_every_feature_requires_boolean(key, tmp_path):
    with pytest.raises(ValueError, match="boolean"):
        replace(configured(tmp_path), **{key: "true"}).validate()


@pytest.mark.parametrize("key", ["delivery", "ask_diablo", "auto_tidy"])
def test_card_subfeatures_need_card(key, tmp_path):
    with pytest.raises(ValueError, match="require card"):
        replace(configured(tmp_path), card=False, **{key: True}).validate()


def test_strict_configuration_and_safe_url(tmp_path):
    with pytest.raises(ValueError, match="known"):
        DiscordSettings.from_table({"bot_token": "fictional"}, base=tmp_path)
    for url in ("http://dashboard.test", "https://person:password@dashboard.test", "https://dashboard.test/?token=fictional"):
        with pytest.raises(ValueError, match="HTTPS"):
            replace(configured(tmp_path), dashboard_url=url).validate()
    with pytest.raises(ValueError, match="numeric"):
        replace(configured(tmp_path), guild_id=103).validate()
    with pytest.raises(ValueError, match="require enabled"):
        replace(configured(tmp_path), enabled=False).validate()
