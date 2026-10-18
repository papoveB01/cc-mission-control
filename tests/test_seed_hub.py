"""scripts/seed_hub.py against a TestClient hub (SPEC 13, 15.2)."""

import importlib.util
import json
import sys
from pathlib import Path

from tests.test_hub_common import AUTH, TOKEN, make_client

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ccmc_seed_hub", ROOT / "scripts" / "seed_hub.py")
seed = importlib.util.module_from_spec(spec)
sys.modules["ccmc_seed_hub"] = seed
spec.loader.exec_module(seed)

ANCHOR = 1_760_000_000.0


def load(client, envelopes):
    totals = {"accepted": 0, "duplicates": 0, "invalid": 0}
    for i in range(0, len(envelopes), 200):
        body = client.post("/ingest", json=envelopes[i:i + 200], headers=AUTH)
        assert body.status_code == 200, body.text
        for k in totals:
            totals[k] += body.json().get(k, 0)
    return totals


def test_envelopes_are_valid_and_deterministic():
    a = seed.build_envelopes(7, ANCHOR)
    assert a == seed.build_envelopes(7, ANCHOR)
    assert a != seed.build_envelopes(8, ANCHOR)
    assert {e["v"] for e in a} == {2}
    assert len({e["eid"] for e in a}) == len(a)
    assert {e["kind"] for e in a} == {"hook", "ctx", "usage", "meta", "title", "cwd"}
    assert len({e["machine_id"] for e in a}) == 2
    assert len({e["session_id"] for e in a}) == 5
    assert [e["t"] for e in a] == sorted(e["t"] for e in a)


def test_seeded_hub_serves_the_demo_data(tmp_path):
    client = make_client(tmp_path)
    envelopes = seed.build_envelopes(7, ANCHOR)
    totals = load(client, envelopes)
    assert totals == {"accepted": len(envelopes), "duplicates": 0, "invalid": 0}

    rows = client.get("/api/history/sessions?limit=50", headers=AUTH).json()["sessions"]
    assert len(rows) == 5
    assert {r["machine_id"] for r in rows} == {"mbp-papove", "devbox-linux"}
    assert sum(1 for r in rows if r["status"] == "ended") == 4  # one session never ends
    assert all(r["tool_calls"] > 0 and r["tokens_in"] > 0 and r["peak_context"] > 0 for r in rows)
    assert any(r["errors"] > 0 for r in rows)
    assert any(r["agents"] >= 4 for r in rows)  # main + three subagents
    assert {r["name_source"] for r in rows} >= {"custom", "generated"}
    assert [m["machine_id"] for m in client.get("/api/history/machines", headers=AUTH).json()["machines"]]

    first = rows[0]
    base = f"/api/history/sessions/{first['machine_id']}/{first['session_id']}"
    session = client.get(base, headers=AUTH).json()
    assert session["usage"]["total"]["messages"] > 0

    ops = set()
    permissions = 0
    for r in rows:
        b = f"/api/history/sessions/{r['machine_id']}/{r['session_id']}"
        ops |= {f["op"] for f in client.get(b + "/files", headers=AUTH).json()["files"]}
        permissions += sum(p["kind"] == "denied" for p in client.get(b + "/permissions", headers=AUTH).json()["permissions"])
    assert ops == {"read", "write", "edit", "search"}
    assert permissions >= 2

    shared = client.get("/api/history/files", params={"path": "/srv/work/payments-api/app/payments/charge.py"}, headers=AUTH).json()["files"]
    assert {f["machine_id"] for f in shared} == {"mbp-papove", "devbox-linux"}  # one file, two machines


def test_reseeding_with_the_same_seed_only_produces_duplicates(tmp_path):
    client = make_client(tmp_path)
    envelopes = seed.build_envelopes(7, ANCHOR)
    load(client, envelopes)
    again = load(client, seed.build_envelopes(7, ANCHOR))
    assert again["accepted"] == 0 and again["duplicates"] == len(envelopes)


def test_cli_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("CCMC_HUB_TOKEN", raising=False)
    assert seed.main(["--url", "http://127.0.0.1:9/ingest"]) == 2
    assert "CCMC_HUB_TOKEN" in capsys.readouterr().err


def test_post_sends_a_bearer_header_and_batches(monkeypatch):
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"accepted": 1, "duplicates": 0}'

    def fake_urlopen(req, timeout=0):
        sent.append((req.full_url, req.get_header("Authorization"), len(json.loads(req.data))))
        return Reply()

    monkeypatch.setattr(seed.urllib.request, "urlopen", fake_urlopen)
    totals = seed.post_envelopes("http://hub.invalid/ingest", TOKEN, seed.build_envelopes(7, ANCHOR)[:420], batch=200)
    assert [s[2] for s in sent] == [200, 200, 20]
    assert all(s[1] == "Bearer " + TOKEN and s[0] == "http://hub.invalid/ingest" for s in sent)
    assert totals["accepted"] == 3
