"""Day 27 secrets audit: no tracked file carries a credential, and the scanner finds one.

The tree scan runs here, in the offline suite, so a pull request that adds a key fails CI.
Every key-shaped value below is assembled at run time: written out whole, it would be the
finding this file exists to prevent.
"""

from __future__ import annotations

import shutil

import pytest
from audit_secrets import ALLOWED, main, scan_line, scan_lines, tracked_lines

_FAKE = "Q7" * 24  # long enough for every pattern, and obviously not a real value


def _shaped(prefix: str) -> str:
    return prefix + _FAKE


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git to list tracked files")
def test_no_tracked_file_carries_a_credential(capsys: pytest.CaptureFixture[str]):
    assert main([]) == 0, capsys.readouterr().out


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("anthropic", _shaped("sk-ant-api03-")),
        ("langfuse", "sk-lf-" + "0123abcd-" * 4),
        ("langfuse", "pk-lf-" + "0123abcd-" * 4),
        ("github", _shaped("ghp_")),
        ("github", _shaped("github_pat_")),
        ("voyage", _shaped("pa-")),
        ("aws", "AKIA" + "Q7ABCDEFGHIJKLMN"),
        ("private_key", "-----BEGIN RSA " + "PRIVATE KEY-----"),
        ("slack", _shaped("xoxb-")),
        ("google", "AIza" + _FAKE[:35]),
        ("url_password", "postgresql://aioc:" + "hunter2real" + "@db.example.com:5432/aioc"),
    ],
)
def test_each_credential_format_is_found(kind: str, value: str):
    assert [k for k, _ in scan_line(f"KEY={value}  # pasted")] == [kind]


def test_a_finding_never_prints_the_whole_value():
    value = _shaped("sk-ant-api03-")
    ((_, redacted),) = scan_line(value)
    assert redacted.endswith("...") and value not in redacted and len(redacted) <= 13


def test_the_fixtures_the_suite_carries_on_purpose_are_allowed_by_exact_text():
    for fixture in ALLOWED:
        assert scan_line(f'monkeypatch.setenv("X", "{fixture}")') == []
    # Allowed by name, never by shape: a longer value with the same prefix is a finding.
    (fixture,) = (f for f in ALLOWED if f.startswith("sk-ant-test"))
    assert [k for k, _ in scan_line(fixture + "-and-more")] == ["anthropic"]


def test_an_interpolated_url_is_judged_by_its_default():
    dev = "postgresql://${POSTGRES_USER:-aioc}:${POSTGRES_PASSWORD:-aioc_dev_only}@postgres:5432"
    assert scan_line(dev) == []
    assert scan_line("postgresql://${USER}:${PASSWORD}@postgres:5432") == []
    real = "postgresql://${USER:-aioc}:${PASSWORD:-" + "hunter2real" + "}@postgres:5432"
    assert [k for k, _ in scan_line(real)] == ["url_password"]


def test_a_finding_names_where_it_was():
    (finding,) = scan_lines([("config/app.env:3", "TOKEN=" + _shaped("ghp_"))])
    assert finding.render().startswith("config/app.env:3: github ghp_Q7Q7Q7")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git to list tracked files")
def test_binary_files_are_skipped_and_text_files_are_read():
    names = {where.rsplit(":", 1)[0] for where, _ in tracked_lines()}
    assert "docker-compose.yml" in names
    assert not any(name.endswith((".gif", ".png")) for name in names)
