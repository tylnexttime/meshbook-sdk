"""0.4.1 config hardening: critique C-04 (an unparsable config is never
written over; transient half-writes are re-read) and C-40 (a broken config
is reported as broken, not as "no API token")."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import meshbook.config as cfgmod
from meshbook import MeshbookClient, MeshbookError
from tests._fakes import install, ok_list


def _flaky_reads(monkeypatch, p, first_answers):
    """Path.read_text on *p* returns each of *first_answers* once, then the truth."""
    real = Path.read_text
    queue = list(first_answers)

    def fake(self, *a, **kw):
        if self == p and queue:
            return queue.pop(0)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake)


@pytest.mark.parametrize("glimpse", ["", "{", '{"token": "mb_tok'], ids=["empty", "brace", "half"])
def test_a_config_caught_mid_rewrite_is_re_read_and_the_token_kept(tmp_path, monkeypatch,
                                                                    glimpse):
    """An older client truncates then rewrites without the lock: a 0.4.0
    writer that caught the file empty or half-written wrote back only its
    update, and the token was gone from every client on the machine."""
    p = tmp_path / "config"
    p.write_text(json.dumps({"token": "mb_token_keep", "custom": 1}), encoding="utf-8")
    _flaky_reads(monkeypatch, p, [glimpse])
    cfgmod.save_config({"active_mesh_id": "m-1"}, p)
    monkeypatch.undo()
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["token"] == "mb_token_keep" and on_disk["custom"] == 1
    assert on_disk["active_mesh_id"] == "m-1"
    assert not list(tmp_path.glob("config.bad*"))


def test_an_empty_file_that_stays_empty_is_just_empty(tmp_path):
    p = tmp_path / "config"
    p.write_text("  \n", encoding="utf-8")
    assert cfgmod.save_config({"token": "t"}, p)["token"] == "t"


@pytest.mark.parametrize("content", [b"{half", b"[1, 2]", b"\xff\xfe\x00garbage"],
                         ids=["half-json", "non-object", "not-utf8"])
def test_a_persistently_broken_config_is_refused_and_untouched(tmp_path, content):
    p = tmp_path / "config"
    p.write_bytes(content)
    with pytest.raises(MeshbookError) as ei:
        cfgmod.save_config({"active_mesh_id": "m-1"}, p)
    assert ei.value.code == "config_unreadable"
    assert p.read_bytes() == content
    assert sorted(f.name for f in tmp_path.iterdir()) == ["config", cfgmod.LOCK_FILENAME]


def test_quarantine_never_overwrites_an_earlier_one(tmp_path):
    p = tmp_path / "config"
    p.write_text("{first", encoding="utf-8")
    one = cfgmod.quarantine_config(p)
    p.write_text("{second", encoding="utf-8")
    two = cfgmod.quarantine_config(p)
    assert one != two
    assert one.read_text(encoding="utf-8") == "{first"
    assert two.read_text(encoding="utf-8") == "{second"


def test_quarantine_leaves_a_readable_or_missing_config_alone(tmp_path):
    p = tmp_path / "config"
    assert cfgmod.quarantine_config(p) is None
    cfgmod.save_config({"token": "t"}, p)
    assert cfgmod.quarantine_config(p) is None
    assert cfgmod.load_config(p)["token"] == "t"


def test_reset_removes_quarantined_copies_and_temp_files(tmp_path):
    """Logging out must not leave the token behind in config.bad*."""
    p = tmp_path / "config"
    cfgmod.save_config({"token": "t"}, p)
    (tmp_path / "config.bad").write_text('{"token": "old"}', encoding="utf-8")
    (tmp_path / "config.bad.20261019T000000Z").write_text("{", encoding="utf-8")
    (tmp_path / ".config.abc.tmp").write_text("{", encoding="utf-8")
    (tmp_path / "agent-key.pem").write_text("KEY", encoding="utf-8")
    assert cfgmod.reset_config(p) is True
    assert sorted(f.name for f in tmp_path.iterdir()) == sorted([cfgmod.LOCK_FILENAME, "agent-key.pem"])


def test_load_config_strict_reports_and_lenient_stays_empty(tmp_path):
    p = tmp_path / "config"
    p.write_text("{nope", encoding="utf-8")
    assert cfgmod.load_config(p) == {}
    with pytest.raises(MeshbookError) as ei:
        cfgmod.load_config(p, strict=True)
    assert ei.value.code == "config_unreadable"
    assert cfgmod.load_config(tmp_path / "missing", strict=True) == {}


# ─── C-40: the client says "config unreadable", not "no API token" ──────


def test_client_with_a_broken_config_reports_it_not_no_token(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text('{"token": "mb_token_in_here", ', encoding="utf-8")
    c = MeshbookClient(config_path=p)
    assert c.config_error is not None and c.config_error.code == "config_unreadable"
    install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        c.contacts.list()
    assert ei.value.code == "config_unreadable" and str(p) in ei.value.message
    assert "mesh login" not in ei.value.message.split("(")[0]


def test_an_explicit_token_still_works_with_a_broken_config(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("{", encoding="utf-8")
    c = MeshbookClient(token="t", mesh="11111111-1111-1111-1111-111111111111", config_path=p)
    r = install(monkeypatch)
    r.add("GET", "/api/contacts", ok_list([]))
    assert c.contacts.list() == []


def test_a_missing_config_is_still_plain_not_authenticated(tmp_path, monkeypatch):
    c = MeshbookClient(config_path=tmp_path / "absent")
    assert c.config_error is None
    install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        c.contacts.list()
    assert ei.value.code == "not_authenticated"
