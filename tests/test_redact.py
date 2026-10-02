import logging
from pathlib import Path

import pytest

from cc_mission_control.config import Config
from cc_mission_control.redact import MASK, Redactor, load_patterns, truncate

# Fake secrets are assembled at runtime so the source never holds a string that
# looks like a live credential (GitHub push protection scans for them).
A20 = "a1B2c3D4e5F6g7H8i9J0"
FAKE = {
    "anthropic": "sk-" + "ant-api03-" + A20 + A20,
    "generic_sk": "sk-" + "proj-" + A20 + "XYZ",
    "github_ghp": "gh" + "p_" + A20 + A20[:16],
    "github_ghs": "gh" + "s_" + A20 + A20[:16],
    "github_pat": "github" + "_pat_" + "11ABCDEFG0" + A20 + "_" + A20,
    "slack": "xo" + "xb-" + "1234567890-0987654321-" + A20,
    "aws": "AK" + "IA" + "ABCDEFGHIJ234567",
    "google": "AI" + "za" + "Sy" + A20 + "0123456789ab",
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0In0" + ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
}

PRIVATE_KEY = (
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + A20 + "\nabc+/def==\n"
    "-----END RSA " + "PRIVATE KEY-----"
)


@pytest.fixture
def r() -> Redactor:
    return Redactor()


@pytest.mark.parametrize("name", sorted(FAKE))
def test_builtin_token_masked(r, name):
    secret = FAKE[name]
    out = r.redact(f"before {secret} after")
    assert secret not in out
    assert out == f"before {MASK} after"


def test_private_key_block_masked(r):
    out = r.redact(f"key:\n{PRIVATE_KEY}\nend")
    assert "MIIEow" not in out
    assert out == f"key:\n{MASK}\nend"


def test_private_key_without_end_line_masked_to_end(r):
    cut = PRIVATE_KEY.split("-----END")[0]
    out = r.redact("x " + cut)
    assert "MIIEow" not in out
    assert out == "x " + MASK


def test_bearer_keeps_scheme(r):
    out = r.redact("Authorization: Bearer abcdefghijklmnop123")
    assert out == f"Authorization: Bearer {MASK}"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("password=hunter2", f"password={MASK}"),
        ("DB_PASSWORD = 's3cr3t value'", f"DB_PASSWORD = {MASK}"),
        ('{"api_key": "abc123"}', f'{{"api_key": {MASK}}}'),
        ("export GITHUB_TOKEN=abc123 && run", f"export GITHUB_TOKEN={MASK} && run"),
        ("client_secret: xyz", f"client_secret: {MASK}"),
        ("aws_secret_access_key=xyz", f"aws_secret_access_key={MASK}"),
        ("pwd=1; next", f"pwd={MASK}; next"),
        ("PRIVATE-KEY: abc", f"PRIVATE-KEY: {MASK}"),
    ],
)
def test_keyed_secret_keeps_key(r, text, expected):
    assert r.redact(text) == expected


def test_keyed_secret_does_not_double_mask(r):
    text = "token=" + FAKE["github_ghp"]
    assert r.redact(text) == f"token={MASK}"


@pytest.mark.parametrize(
    "text",
    [
        "pytest -q tests/",
        "src/fraud/score.py",
        "max_tokens=4000",
        "the password is required",
        "skip-this-flag --sk-mode",
        "grep -rn tokenize src/",
        "def get_secret_name(self):",
        "",
    ],
)
def test_benign_text_unchanged(r, text):
    assert r.redact(text) == text


def test_extra_patterns_from_file(tmp_path: Path):
    f = tmp_path / "extra.txt"
    f.write_text("# internal ids\nACME-[0-9]{6}\n\n")
    r = Redactor(load_patterns(f))
    assert r.redact("ticket ACME-123456 open") == f"ticket {MASK} open"


def test_invalid_extra_pattern_skipped(tmp_path: Path, caplog):
    f = tmp_path / "extra.txt"
    f.write_text("([unclosed\nFOO-[0-9]+\n")
    with caplog.at_level(logging.WARNING):
        patterns = load_patterns(f)
    assert len(patterns) == 1
    assert "line 1" in caplog.text
    assert Redactor(patterns).redact("FOO-42") == MASK


def test_missing_extra_file_is_not_fatal(tmp_path: Path):
    assert load_patterns(tmp_path / "nope.txt") == []
    assert load_patterns(None) == []


def test_truncate_suffix():
    assert truncate("abcdef", 4) == "abcd… [2 more characters]"
    assert truncate("abc", 4) == "abc"
    assert truncate("abcdef", 0) == "abcdef"


def test_text_redacts_before_truncating():
    secret = FAKE["anthropic"]
    r = Redactor(max_chars=20)
    out = r.text("k " + secret + " " + "x" * 50)
    assert "sk-ant" not in out
    assert out.startswith("k " + MASK)
    assert out.endswith("more characters]")


def test_value_walks_nested_structures_and_keeps_keys(r):
    payload = {
        "command": "curl -H 'Authorization: Bearer abcdefghijklmnop123' x",
        "env": {"api_key": FAKE["generic_sk"], "count": 3},
        "args": ["--token", FAKE["github_ghp"], None, True],
    }
    out = r.value(payload)
    assert out["command"] == f"curl -H 'Authorization: Bearer {MASK}' x"
    assert out["env"] == {"api_key": MASK, "count": 3}
    assert out["args"] == ["--token", MASK, None, True]
    assert payload["env"]["api_key"] == FAKE["generic_sk"]  # input not mutated


def test_from_config_reads_env(tmp_path: Path):
    f = tmp_path / "extra.txt"
    f.write_text("ZZZ-[0-9]+\n")
    cfg = Config.from_env({"CCMC_REDACT_FILE": str(f), "CCMC_MAX_FIELD_CHARS": "10"})
    r = Redactor.from_config(cfg)
    assert r.max_chars == 10
    assert r.redact("ZZZ-9") == MASK
