"""The committed history bundle works under the hub's CSP (SPEC 13.2, 13.4)."""

import re
from pathlib import Path

from tests.test_hub_common import make_client

STATIC = Path(__file__).resolve().parent.parent / "cc_mission_control" / "hub" / "static"


def test_history_page_has_nothing_inline():
    html = (STATIC / "history.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert scripts and all(" src=" in s for s in scripts)  # no inline script bodies
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html)  # no inline event handlers
    assert "data:" not in html and "javascript:" not in html


def test_served_page_and_assets_carry_the_csp(tmp_path):
    client = make_client(tmp_path)
    page = client.get("/history")
    assert page.status_code == 200 and "<div id=\"root\">" in page.text
    assert page.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
    refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
    assert any(r.endswith(".js") for r in refs) and any(r.endswith(".css") for r in refs)
    for ref in refs:
        asset = client.get(ref)
        assert asset.status_code == 200, ref
        assert "default-src 'self'" in asset.headers["content-security-policy"]


def test_bundle_has_no_live_socket_code():
    js = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "assets").glob("*.js"))
    assert "WebSocket" not in js and "/ws" not in js
    css = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "assets").glob("*.css"))
    assert "url(data:" not in css and "@import" not in css
