"""All release versions must agree: UI, Python package, pyproject and the plugin manifest."""
import json
import re
from pathlib import Path

import cc_mission_control

ROOT = Path(__file__).resolve().parent.parent


def test_versions_are_consistent():
    ui = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["version"]
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml has no version"
    versions = {
        "ui/package.json": ui,
        "cc_mission_control.__version__": cc_mission_control.__version__,
        "pyproject.toml": m.group(1),
        ".claude-plugin/plugin.json": plugin,
    }
    assert len(set(versions.values())) == 1, versions
