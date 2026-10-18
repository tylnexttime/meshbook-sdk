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
    leftovers = [f.name for f in tmp_path.iterdir()
                 if f.name not in ("config", cfgmod.LOCK_FILENAME)]
    assert leftovers == [], f"temp files left behind: {leftovers}"


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
    with cfgmod._locked(tmp_path, timeout=0.1):  # released afterwards
        pass


def test_a_held_lock_times_out_with_a_named_error(tmp_path):
    p = tmp_path / "config"
    t0 = time.monotonic()
    with cfgmod._locked(tmp_path, timeout=1):
        with pytest.raises(MeshbookError) as ei:
            cfgmod.save_config({"token": "t"}, p, lock_timeout=0.2)
    assert ei.value.code == "config_locked"
    assert time.monotonic() - t0 >= 0.2
    assert not p.exists(), "nothing may be written without the lock"


def test_a_lock_file_left_behind_does_not_block(tmp_path):
    """The lock is the OS lock, not the file's existence: a config.lock left
    by a crashed process (or by meshbook-sdk's first draft) is no obstacle."""
    (tmp_path / cfgmod.LOCK_FILENAME).write_text("999999\n", encoding="ascii")
    cfgmod.save_config({"token": "t"}, tmp_path / "config", lock_timeout=0.2)
    assert cfgmod.load_config(tmp_path / "config")["token"] == "t"


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with cfgmod._locked(tmp_path, timeout=0.2):
            raise RuntimeError("boom")
    with cfgmod._locked(tmp_path, timeout=0.2):
        pass


_HOLDER = """
import sys, time
import meshbook.config as c
with c._locked(__import__("pathlib").Path(sys.argv[1]), timeout=5):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_the_lock_excludes_another_process_and_dies_with_it(tmp_path):
    """Cross-process, and the case the age-based draft got wrong: a holder
    that dies (killed, not exiting cleanly) frees the lock at once."""
    import subprocess
    import sys

    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(tmp_path), "30"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(MeshbookError) as ei:
            cfgmod.save_config({"token": "t"}, tmp_path / "config", lock_timeout=0.3)
        assert ei.value.code == "config_locked"
        holder.kill()  # a crash, not a clean release
        holder.wait(timeout=10)
        cfgmod.save_config({"token": "t"}, tmp_path / "config", lock_timeout=2)
        assert cfgmod.load_config(tmp_path / "config")["token"] == "t"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


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


def test_a_corrupt_config_is_never_written_over_and_repair_is_explicit(tmp_path):
    """0.4.1 (critique C-04): 0.4.0 renamed a broken file to config.bad and
    wrote back ONLY the update, so a config caught mid-rewrite by an older
    client lost its token. Now the update is refused and the file is left
    byte-identical; quarantine_config() is the explicit repair."""
    p = tmp_path / "config"
    p.write_text("{half a json", encoding="utf-8")
    with pytest.raises(MeshbookError) as ei:
        cfgmod.save_config({"token": "t"}, p)
    assert ei.value.code == "config_unreadable" and str(p) in ei.value.message
    assert p.read_text(encoding="utf-8") == "{half a json"
    assert not list(tmp_path.glob("config.bad*"))
    moved = cfgmod.quarantine_config(p)
    assert moved is not None and moved.name.startswith("config.bad.")
    assert moved.read_text(encoding="utf-8") == "{half a json" and not p.exists()
    cfgmod.save_config({"token": "t"}, p)
    assert cfgmod.load_config(p)["token"] == "t"
