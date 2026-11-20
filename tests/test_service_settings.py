"""Settings saved from the web UI, layered over .env."""

import json

import pytest

from vproc.config import ConfigError
from vproc.service_settings import BY_KEY, FIELDS, ServiceSettings, text_of

ENV = {"VPROC_OUTPUT_SIZE": "3840x2160", "VPROC_LIPSYNC_OFFSET": "40"}


def settings(tmp_path, env=ENV):
    return ServiceSettings(tmp_path, env=env)


def test_every_field_maps_to_a_config_attribute():
    from vproc.config import Config

    for field in FIELDS:
        assert hasattr(Config(), field.attr), field.key


def test_saved_values_win_over_env_and_reset_goes_back(tmp_path):
    s = settings(tmp_path)
    s.update({"OUTPUT_SIZE": "1920x1080", "LLDV": "true", "LLDV_PEAK": "1390"})
    config, warning = ServiceSettings(tmp_path, env=ENV).config()  # as after a restart
    assert warning is None
    assert (config.output_size, config.lldv, config.lldv_peak, config.lipsync_offset) == ("1920x1080", True, 1390, 40)

    s.update({"OUTPUT_SIZE": None})
    assert ServiceSettings(tmp_path, env=ENV).config()[0].output_size == "3840x2160"
    assert json.loads((tmp_path / "service.json").read_text()) == {"LLDV": "true", "LLDV_PEAK": "1390"}


@pytest.mark.parametrize("changes", [
    {"OUTPUT_SIZE": "big"},
    {"LLDV_PEAK": "50"},
    {"LIPSYNC_URLS": "http://hyperion/lipsync"},     # no {delay_ms}
    {"RGB_RANGE": "medium"},
    {"LOG_LEVEL": "loud"},
    {"API_TOKEN": "x"},                                # not changeable here
])
def test_invalid_changes_are_refused_and_nothing_is_saved(tmp_path, changes):
    s = settings(tmp_path)
    s.update({"LLDV": "true"})
    with pytest.raises(ConfigError):
        s.update(changes)
    assert s.saved == {"LLDV": "true"}
    assert json.loads((tmp_path / "service.json").read_text()) == {"LLDV": "true"}


def test_broken_saved_settings_fall_back_to_env(tmp_path):
    (tmp_path / "service.json").write_text(json.dumps({"OUTPUT_SIZE": "nonsense"}))
    config, warning = settings(tmp_path).config()
    assert config.output_size == "3840x2160"
    assert "ignored" in warning
    (tmp_path / "service.json").write_text("not json")
    assert settings(tmp_path).config() == (settings(tmp_path, ENV).config()[0], None)


def test_describe_shows_sources_running_values_and_pending_restart(tmp_path):
    s = settings(tmp_path)
    running, _ = s.config()
    page = s.describe(running)
    by_key = {f["key"]: f for f in page["fields"]}
    output = by_key["OUTPUT_SIZE"]
    assert (output["value"], output["source"], output["running"]) == ("3840x2160", "env", "3840x2160")
    assert by_key["LLDV"]["source"] == "default" and by_key["LLDV"]["value"] == "false"
    assert not page["restart_needed"]
    assert any("DECKLINK_LIB" in h["name"] for h in page["host_only"])

    s.update({"LLDV": "true"})
    page = s.describe(running)
    lldv = {f["key"]: f for f in page["fields"]}["LLDV"]
    assert (lldv["value"], lldv["source"], lldv["running"]) == ("true", "saved", "false")
    assert page["restart_needed"]


def test_values_are_written_like_env():
    from vproc.config import DEFAULT_REFRESH_MAP

    assert text_of(True) == "true" and text_of(1390.0) == "1390"
    assert text_of(("http://a/1/{delay_ms}", "http://a/2/{delay_ms}")) == "http://a/1/{delay_ms},http://a/2/{delay_ms}"
    assert text_of(("--a=1", "--b")) == "--a=1 --b"
    assert text_of(dict(DEFAULT_REFRESH_MAP)).startswith("23.976=23.976")
    assert BY_KEY["RGB_RANGE"].choices == ("full", "limited")
