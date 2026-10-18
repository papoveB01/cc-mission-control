import base64
import json

import pytest

from test_hub_common import AUTH, MACHINE, SID, TOKEN, env, envelopes, hook, hub_config, make_app, make_client, post, scenario
from cc_mission_control.config import Config, HubConfig
from cc_mission_control.hub.app import create_app
from cc_mission_control.hub.__main__ import main
from fastapi.testclient import TestClient


def session_events(sid, start, prompt, path=None, project="/work/proj"):
    ev = [("hook", start, {"hook_event_name": "SessionStart", "session_id": sid, "cwd": project, "source": "startup"}),
          ("hook", start + 1, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": project, "prompt": prompt})]
    if path:
        ev.append(("hook", start + 2, {"hook_event_name": "PreToolUse", "session_id": sid, "cwd": project,
                                       "tool_name": "Edit", "tool_use_id": "e1", "tool_input": {"file_path": path}}))
    return ev


@pytest.fixture
def client(tmp_path):
    c = make_client(tmp_path)
    items = envelopes(session_events("s1", 1000, "alpha refactor", "/work/proj/src/a.py"), sid="s1", machine="m1", user="ann", host="h1")
    items += envelopes(session_events("s2", 2000, "beta review 100%", "/work/other/b.py", "/work/other"), sid="s2", machine="m1", user="ann", host="h1")
    items += envelopes(session_events("s3", 3000, "gamma"), sid="s3", machine="m2", user="bob", host="h2")
    post(c, items)
    return c


def get(client, route, **params):
    r = client.get("/api/history" + route, params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def ids(body):
    return [s["session_id"] for s in body["sessions"]]


def test_list_sorted_and_filters(client):
    assert ids(get(client, "/sessions")) == ["s3", "s2", "s1"]
    assert ids(get(client, "/sessions", machine="m1")) == ["s2", "s1"]
    assert ids(get(client, "/sessions", project="other")) == ["s2"]
    assert ids(get(client, "/sessions", q="alpha")) == ["s1"]
    assert ids(get(client, "/sessions", q="OTHER")) == ["s2"]  # project name, case-insensitive
    assert ids(get(client, "/sessions", q="100%")) == ["s2"]  # % is literal
    assert ids(get(client, "/sessions", q="%")) == ["s2"]
    assert ids(get(client, "/sessions", **{"from": 1500, "to": 2500})) == ["s2"]
    assert ids(get(client, "/sessions", has_file="/work/proj/src/a.py")) == ["s1"]
    assert ids(get(client, "/sessions", has_file="/work/other/")) == ["s2"]
    assert ids(get(client, "/sessions", has_file="/work/nothing")) == []


def test_cursor_pagination(client):
    page = get(client, "/sessions", limit=2)
    assert ids(page) == ["s3", "s2"] and page["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
    assert decoded == [2000.0, "m1", "s2"]
    rest = get(client, "/sessions", limit=2, cursor=page["next_cursor"])
    assert ids(rest) == ["s1"] and rest["next_cursor"] is None
    assert client.get("/api/history/sessions", params={"cursor": "garbage"}, headers=AUTH).status_code == 400


def test_file_lookup(client):
    exact = get(client, "/files", path="/work/proj/src/a.py")["files"]
    assert [(f["session_id"], f["op"], f["status"], f["accesses"], f["ops"]) for f in exact] == [("s1", "edit", "running", 1, ["edit"])]
    assert exact[0]["session_name"]
    pref = get(client, "/files", prefix="/work/")["files"]
    assert [f["session_id"] for f in pref] == ["s2", "s1"]  # newest first
    assert get(client, "/files", prefix="/nope")["files"] == []
    assert client.get("/api/history/files", headers=AUTH).status_code == 400
    assert client.get("/api/history/files", params={"path": "/a", "prefix": "/"}, headers=AUTH).status_code == 400


def test_machines(client):
    ms = {m["machine_id"]: m for m in get(client, "/machines")["machines"]}
    assert set(ms) == {"m1", "m2"}
    assert (ms["m1"]["user"], ms["m1"]["host"], ms["m1"]["sessions"]) == ("ann", "h1", 2)
    assert ms["m1"]["first_seen"] == 1000 and ms["m1"]["last_seen"] == 2002


def test_unknown_session_404(client):
    assert client.get("/api/history/sessions/m1/nope", headers=AUTH).status_code == 404
    assert client.get("/api/history/sessions/m1/nope/files", headers=AUTH).status_code == 404


def test_auth_matrix(client):
    assert client.get("/api/history/machines").status_code == 401
    assert client.get("/api/history/machines", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/history/machines", headers=AUTH).status_code == 200
    assert client.post("/ingest", json=[]).status_code == 401
    assert client.post("/ingest", json=[], headers={"Authorization": "Bearer wrong"}).status_code == 401
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["app"] == "cc-mission-control-hub"
    assert health.json()["sessions"] == 3


def test_login_cookie(client):
    assert client.post("/login", json={"token": "wrong"}).status_code == 401
    r = client.post("/login", json={"token": TOKEN})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie
    assert "secure" not in cookie
    assert client.get("/api/history/machines").status_code == 200  # cookie jar carries the session
    assert client.post("/ingest", json=[]).status_code == 401  # cookies never authorize /ingest
    client.cookies.clear()
    client.cookies.set("ccmc_hub_session", "123.deadbeef")
    assert client.get("/api/history/machines").status_code == 401


def test_login_cookie_is_secure_over_https(tmp_path):
    c = TestClient(make_app(tmp_path), base_url="https://testserver")
    assert "secure" in c.post("/login", json={"token": TOKEN}).headers["set-cookie"].lower()


def test_host_and_origin(client):
    assert client.get("/api/history/machines", headers={**AUTH, "Host": "evil.example"}).status_code == 403
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/history/machines", headers={**AUTH, "Host": "testserver:4318"}).status_code == 200
    bad = {**AUTH, "Origin": "http://evil.example"}
    assert client.get("/api/history/machines", headers=bad).status_code == 403
    assert client.post("/login", json={"token": TOKEN}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/history/machines", headers={**AUTH, "Origin": "http://testserver"}).status_code == 200


def test_host_with_explicit_port_entry(tmp_path):
    c = make_client(tmp_path, hosts=("hub.example:4318",))
    assert c.get("/health", headers={"Host": "hub.example:4318"}).status_code == 200
    assert c.get("/health", headers={"Host": "hub.example:9"}).status_code == 403
    assert c.get("/health", headers={"Host": "127.0.0.1:4318"}).status_code == 200  # loopback health check


def test_history_page_placeholder_and_static(tmp_path):
    c = make_client(tmp_path)
    assert "not built" in c.get("/history").text
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "history.html").write_text("<h1>real ui</h1>")
    (static / "assets" / "x.js").write_text("1")
    app = create_app(hub_config(tmp_path / "other"), store_config=Config(), static_dir=static)
    c2 = TestClient(app)
    assert "real ui" in c2.get("/history").text and c2.get("/assets/x.js").status_code == 200


def test_startup_refuses_missing_or_short_token(tmp_path, monkeypatch, capsys):
    for token in (None, "short"):
        with pytest.raises(ValueError):
            create_app(hub_config(tmp_path, token=token))
    monkeypatch.delenv("CCMC_HUB_TOKEN", raising=False)
    assert main() == 2
    assert "CCMC_HUB_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("CCMC_HUB_TOKEN", "x" * 31)
    assert main() == 2


def test_hub_config_from_env():
    cfg = HubConfig.from_env({"CCMC_HUB_TOKEN": "t" * 32, "CCMC_HUB_DB": "/tmp/h.db", "CCMC_HUB_PORT": "9000",
                              "CCMC_HUB_BIND": "127.0.0.1", "CCMC_HUB_HOSTS": "A:1, b ,", "CCMC_HUB_RETENTION_DAYS": "30",
                              "CCMC_HUB_BACKUPS": "3"})
    assert (cfg.port, cfg.bind, cfg.hosts, cfg.retention_days, cfg.backups) == (9000, "127.0.0.1", ("a:1", "b"), 30, 3)
    assert cfg.problem() is None
    d = HubConfig.from_env({})
    assert (d.port, d.bind, d.hosts, d.retention_days, d.backups, str(d.db)) == (4318, "0.0.0.0", ("ccmc-hub",), 0, 7, "/data/history.db")
