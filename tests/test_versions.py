"""All release versions must agree: UI, Python package, pyproject and the plugin manifest."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_docker_versions_match_package():
    v = cc_mission_control.__version__
    dockerfile = (ROOT / "deploy" / "hub" / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG VERSION=(\S+)", dockerfile, re.M)
    assert m, "Dockerfile has no ARG VERSION default"
    compose = (ROOT / "deploy" / "hub" / "docker-compose.yml").read_text(encoding="utf-8")
    t = re.search(r"^\s*image:\s*ccmc-hub:(\S+)", compose, re.M)
    assert t, "docker-compose.yml has no image: ccmc-hub:<version>"
    assert (m.group(1), t.group(1)) == (v, v)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_hub_requirements_lock_matches_uv_lock(tmp_path):
    target = tmp_path / "requirements.lock"
    subprocess.run(
        ["uv", "export", "--no-dev", "--no-hashes", "--frozen", "--no-emit-project", "-o", str(target)],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    out = target.read_text(encoding="utf-8")
    lock = (ROOT / "deploy" / "hub" / "requirements.lock").read_text(encoding="utf-8")
    strip = lambda t: [ln for ln in t.splitlines() if not ln.startswith("#    uv export")]
    assert strip(out) == strip(lock), "regenerate: uv export --no-dev --no-hashes --frozen --no-emit-project -o deploy/hub/requirements.lock"
