from pathlib import Path

from cc_mission_control.config import Config


def test_defaults_without_plugin_data():
    c = Config.from_env({})
    assert c.port == 4317
    assert c.data_dir == Path.home() / ".cc-mission-control"
    assert c.context_window == 200_000
    assert c.idle_minutes == 30
    assert c.stale_minutes == 60
    assert c.max_calls == 500
    assert c.max_field_chars == 4000
    assert c.redact_file is None
    assert c.no_browser is False
    assert c.upstream_url is None
    assert c.dev_origins == ()


def test_data_dir_prefers_explicit_then_plugin_data(tmp_path):
    assert Config.from_env({"CLAUDE_PLUGIN_DATA": str(tmp_path)}).data_dir == tmp_path
    explicit = tmp_path / "x"
    env = {"CLAUDE_PLUGIN_DATA": str(tmp_path), "CCMC_DATA_DIR": str(explicit)}
    assert Config.from_env(env).data_dir == explicit


def test_parsing():
    c = Config.from_env(
        {
            "CCMC_PORT": "5000",
            "CCMC_IDLE_MINUTES": "0",
            "CCMC_MAX_CALLS": "not-a-number",
            "CCMC_NO_BROWSER": "1",
            "CCMC_DEV_ORIGINS": "http://localhost:5173/, http://127.0.0.1:5173",
            "CCMC_UPSTREAM_URL": " ",
        }
    )
    assert c.port == 5000
    assert c.idle_minutes == 0
    assert c.max_calls == 500
    assert c.no_browser is True
    assert c.dev_origins == ("http://localhost:5173", "http://127.0.0.1:5173")
    assert c.upstream_url is None
