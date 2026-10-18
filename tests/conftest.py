import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_ccmc_env(monkeypatch):
    """Tests never inherit CCMC_* or CLAUDE_PLUGIN_DATA (settings.json env can point at a real hub)."""
    for name in list(os.environ):
        if name.startswith("CCMC_") and name != "CCMC_SLOW_TESTS":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
