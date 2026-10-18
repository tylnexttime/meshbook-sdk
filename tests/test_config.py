"""meshbook.config — the single owner of ~/.meshbook/config.

What these tests pin down, because each is a way the three pre-0.4 copies
of this code could lose or leak a user's credential:

* directory resolution is exactly the order every earlier client used, so
  existing installs are found;
* reads are utf-8 (BOM tolerated) and never raise;
* writes MERGE into what is on disk under a lock, keep unknown keys, can
  delete a key, stamp ``schema_version`` and never downgrade it;
* writes are atomic: a failed replace leaves the old file intact and no
  temp file behind;
* the lock serialises concurrent writers (no lost keys), times out on a
  live holder, and breaks a stale one;
* POSIX mode 600.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import meshbook.config as cfgmod
from meshbook import MeshbookError


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HOME with no meshbook env vars set."""
    monkeypatch.delenv("MESHBOOK_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ─── directory resolution ──────────────────────────────────────────────


def test_config_dir_env_wins(home, monkeypatch, tmp_path):
    (home / ".meshbook").mkdir()
    monkeypatch.setenv("MESHBOOK_CONFIG_DIR", str(tmp_path / "pinned"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cfgmod.config_dir() == tmp_path / "pinned"


def test_config_dir_existing_legacy_beats_xdg(home, monkeypatch, tmp_path):
    (home / ".meshbook").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cfgmod.config_dir() == home / ".meshbook"


def test_config_dir_xdg_when_no_legacy(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cfgmod.config_dir() == tmp_path / "xdg" / "meshbook"


def test_config_dir_default_is_legacy(home):
    assert cfgmod.config_dir() == home / ".meshbook"
    assert cfgmod.config_path() == home / ".meshbook" / "config"


# ─── reading ───────────────────────────────────────────────────────────


def test_load_missing_garbage_and_non_object_are_empty(tmp_path):
    p = tmp_path / "config"
    assert cfgmod.load_config(p) == {}
    p.write_text("{not json", encoding="utf-8")
    assert cfgmod.load_config(p) == {}
    p.write_text("[1, 2]", encoding="utf-8")
    assert cfgmod.load_config(p) == {}
    p.write_bytes(b"\xff\xfe\x00garbage")
    assert cfgmod.load_config(p) == {}


def test_load_reads_utf8_and_tolerates_a_bom(tmp_path):
    p = tmp_path / "config"
    p.write_bytes("﻿".encode() + json.dumps({"base": "https://zażółć.example"},
                                                ensure_ascii=False).encode("utf-8"))
    assert cfgmod.load_config(p)["base"] == "https://zażółć.example"


def test_loads_a_config_written_by_cli_0_11(tmp_path):
    """Backward compatibility: the exact bytes `mesh login` wrote before 0.4
    (json.dumps(indent=2), locale encoding — i.e. ASCII), no schema_version."""
    p = tmp_path / "config"
    p.write_text(json.dumps({"base": "https://meshbook.org", "token": "mb_token_old",
                             "active_mesh_id": "m-1"}, indent=2))
    assert cfgmod.load_config(p) == {
        "base": "https://meshbook.org", "token": "mb_token_old", "active_mesh_id": "m-1"}


# ─── writing ───────────────────────────────────────────────────────────


def test_save_merges_keeps_unknown_keys_and_stamps_schema(tmp_path):
    p = tmp_path / "config"
    p.write_text(json.dumps({"token": "mb_token_x", "someone_elses_key": {"a": 1}}),
                 encoding="utf-8")
    written = cfgmod.save_config({"active_mesh_id": "m-2"}, p)
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == written
    assert on_disk["token"] == "mb_token_x"
    assert on_disk["someone_elses_key"] == {"a": 1}
    assert on_disk["active_mesh_id"] == "m-2"
    assert on_disk["schema_version"] == cfgmod.SCHEMA_VERSION == 1


def test_save_none_deletes_a_key(tmp_path):
    p = tmp_path / "config"
    cfgmod.save_config({"token": "t", "auth_mode": "agent"}, p)
    cfgmod.save_config({"auth_mode": None}, p)
    assert "auth_mode" not in cfgmod.load_config(p)
    assert cfgmod.load_config(p)["token"] == "t"


def test_save_never_downgrades_a_newer_schema_version(tmp_path):
    p = tmp_path / "config"
    p.write_text(json.dumps({"schema_version": 7, "future": True}), encoding="utf-8")
    cfgmod.save_config({"token": "t"}, p)
    on_disk = cfgmod.load_config(p)
    assert on_disk["schema_version"] == 7
    assert on_disk["future"] is True


def test_save_creates_the_directory(tmp_path):
    p = tmp_path / "deep" / "er" / "config"
    cfgmod.save_config({"token": "t"}, p)
    assert cfgmod.load_config(p)["token"] == "t"


def test_save_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    """A crash at the replace leaves the previous file byte-identical and
    no temp file behind — the old CLI's write_text truncated first."""
    p = tmp_path / "config"
    cfgmod.save_config({"token": "mb_token_keep"}, p)
    before = p.read_bytes()

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(cfgmod.os, "replace", boom)
    with pytest.raises(MeshbookError) as ei:
        cfgmod.save_config({"token": "mb_token_new"}, p)
    assert ei.value.code == "config_write_failed"
    assert p.read_bytes() == before
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "config"]
    assert leftovers == [], f"temp/lock files left behind: {leftovers}"


def test_concurrent_writers_lose_no_keys(tmp_path):
    """Twenty threads each write their own key at once. Without the lock +
    re-read-under-lock, last-writer-wins drops most of them."""
    p = tmp_path / "config"
    cfgmod.save_config({"token": "t"}, p)
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            cfgmod.save_config({f"k{i}": i}, p)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    on_disk = cfgmod.load_config(p)
    assert all(on_disk.get(f"k{i}") == i for i in range(20)), on_disk
    assert on_disk["token"] == "t"
    assert not (tmp_path / cfgmod.LOCK_FILENAME).exists()


def test_a_live_lock_times_out_with_a_named_error(tmp_path):
    p = tmp_path / "config"
    lock = tmp_path / cfgmod.LOCK_FILENAME
    lock.write_text("12345\n", encoding="ascii")  # fresh: a live writer
    t0 = time.monotonic()
    with pytest.raises(MeshbookError) as ei:
        cfgmod.save_config({"token": "t"}, p, lock_timeout=0.2)
    assert ei.value.code == "config_locked"
    assert str(lock) in ei.value.message
    assert time.monotonic() - t0 >= 0.2
    assert not p.exists(), "nothing may be written without the lock"
    assert lock.exists(), "someone else's lock must not be removed"


def test_a_stale_lock_is_broken(tmp_path):
    p = tmp_path / "config"
    lock = tmp_path / cfgmod.LOCK_FILENAME
    lock.write_text("999999\n", encoding="ascii")
    old = time.time() - (cfgmod.LOCK_STALE_AFTER + 5)
    os.utime(lock, (old, old))
    cfgmod.save_config({"token": "t"}, p, lock_timeout=0.2)
    assert cfgmod.load_config(p)["token"] == "t"
    assert not lock.exists()


@pytest.mark.skipif(os.name != "posix", reason="mode bits are POSIX-only")
def test_config_lands_mode_600(tmp_path):
    p = tmp_path / "config"
    cfgmod.save_config({"token": "t"}, p)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_reset_config_removes_only_the_config(tmp_path):
    p = tmp_path / "config"
    cfgmod.save_config({"token": "t"}, p)
    (tmp_path / "agent-key.pem").write_text("k", encoding="ascii")
    assert cfgmod.reset_config(p) is True
    assert not p.exists()
    assert (tmp_path / "agent-key.pem").exists(), "logout must not destroy the agent key"
    assert cfgmod.reset_config(p) is False


def test_save_config_defaults_to_the_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHBOOK_CONFIG_DIR", str(tmp_path / "cfg"))
    cfgmod.save_config({"base": "https://meshbook.org"})
    assert cfgmod.load_config()["base"] == "https://meshbook.org"
    assert (tmp_path / "cfg" / "config").exists()


def test_an_unreadable_config_is_never_overwritten(tmp_path, monkeypatch):
    """If the read half of read-modify-write fails, writing back {updates}
    would silently drop the token. Refuse instead."""
    p = tmp_path / "config"
    cfgmod.save_config({"token": "mb_token_keep"}, p)
    before = p.read_bytes()
    real_read_text = Path.read_text

    def flaky(self, *a, **kw):
        if self == p:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(MeshbookError) as ei:
        cfgmod.save_config({"active_mesh_id": "m-1"}, p)
    assert ei.value.code == "config_write_failed"
    monkeypatch.undo()
    assert p.read_bytes() == before


def test_a_corrupt_config_is_kept_aside_and_replaced(tmp_path):
    """A broken file must not block `mesh login` from repairing it, and must
    not be destroyed either."""
    p = tmp_path / "config"
    p.write_text("{half a json", encoding="utf-8")
    cfgmod.save_config({"token": "t"}, p)
    assert cfgmod.load_config(p)["token"] == "t"
    assert (tmp_path / "config.bad").read_text(encoding="utf-8") == "{half a json"
