"""0.4.1: the SDK gaps meshbook-mcp hit porting onto 0.4.0 (REPORT-MCP
2026-10-19, "SDK gaps" 2-8), each pinned on the wire."""
from __future__ import annotations

import pytest

from meshbook import MeshbookClient, MeshbookError
from tests._fakes import MESH, U1, install, ok, ok_list

CH = "c0c0c0c0-0000-0000-0000-000000000001"


@pytest.fixture
def client(tmp_path):
    return MeshbookClient(token="mb_token_test", mesh=MESH, config_path=tmp_path / "none")


# ─── gap 2: a channel UUID is used as given ────────────────────────────


def test_channel_methods_skip_the_lookup_for_a_uuid(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/channels/{CH}/messages", ok_list([{"id": "m"}]))
    r.add("POST", f"/api/channels/{CH}/messages", ok({"id": "m2"}))
    r.add("GET", f"/api/channels/{CH}/members", ok_list([{"userId": U1}]))
    r.add("POST", f"/api/channels/{CH}/members", ok_list([{"userId": U1}]))
    r.add("DELETE", f"/api/channels/{CH}/members/{U1}", ok({}))
    client.channels.read(CH)
    client.channels.post(f"#{CH}", "hi")
    client.channels.members(CH.upper())
    client.channels.add_member(CH, U1)
    client.channels.remove_member(CH, user=U1)
    # One request per call, and never GET /api/channels/{id} or the list.
    assert len(r.requests) == 5
    assert r.calls("GET", f"/api/channels/{CH}") == []
    assert r.calls("GET", f"/api/meshes/{MESH}/channels") == []


def test_channel_names_still_resolve(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/meshes/{MESH}/channels", ok_list([{"id": CH, "name": "news"}]))
    r.add("GET", f"/api/channels/{CH}/messages", ok_list([]))
    client.channels.read("#News")
    assert len(r.calls("GET", f"/api/meshes/{MESH}/channels")) == 1


def test_explicit_resolve_of_a_uuid_still_fetches_the_row(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/channels/{CH}", ok({"id": CH, "name": "news"}))
    assert client.channels.resolve(CH)["name"] == "news"


# ─── gap 3: offset= on cursor-paged routes is refused, before= added ───


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.chat.list(offset=20),
        lambda c: c.chat.search("x", offset=20),
        lambda c: c.channels.read(CH, offset=20),
        lambda c: c.dms.read("@wren", offset=20),
        lambda c: c.users.list(offset=20),
    ],
    ids=["chat.list", "chat.search", "channels.read", "dms.read", "users.list"],
)
def test_offset_is_refused_where_the_server_ignores_it(client, monkeypatch, call):
    r = install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        call(client)
    assert ei.value.code == "bad_argument" and "offset" in ei.value.message
    assert r.requests == []  # refused before the wire


def test_before_cursor_is_sent_on_chat_channels_and_dms(client, monkeypatch):
    import datetime as dt

    r = install(monkeypatch)
    r.add("GET", f"/api/entities/mesh/{MESH}/chat", ok_list([]))
    r.add("GET", f"/api/channels/{CH}/messages", ok_list([]))
    r.add("POST", f"/api/meshes/{MESH}/dms/with/{U1}", ok({"id": CH}))
    stamp = "2026-10-19T09:00:00+11:00"
    client.chat.list(limit=5, before=stamp)
    assert r.query() == {"limit": "5", "before": stamp}
    client.channels.read(CH, before=dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc))
    assert r.query()["before"].startswith("2026-10-19T00:00:00")
    client.dms.read(U1, before=stamp)
    assert r.query()["before"] == stamp
    with pytest.raises(MeshbookError) as ei:
        client.chat.list(before=dt.datetime(2026, 10, 19))  # naive: refused
    assert ei.value.code == "naive_datetime"


# ─── critique C-23: log_time defaults to the LOCAL date ───────────────


def test_log_time_defaults_to_the_local_date_not_utc(client, monkeypatch):
    import datetime as dt
    import types

    import meshbook.resources.work as work

    class LocalDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 10, 20)  # 08:00 on the 20th in Sydney…

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:  # …is still the 19th in UTC
                return dt.datetime(2026, 10, 19, 21, 0, tzinfo=dt.timezone.utc)
            return dt.datetime(2026, 10, 20, 8, 0)

    monkeypatch.setattr(work, "_dt", types.SimpleNamespace(
        date=LocalDate, datetime=Clock, timezone=dt.timezone))
    r = install(monkeypatch)
    r.add("POST", "/api/task-time-logs", ok({"id": "tl"}))
    client.tasks.log_time("t1", 1.5)
    assert r.body()["loggedForDate"] == "2026-10-20"
    client.tasks.log_time("t1", 1.5, date="2026-10-01")
    assert r.body()["loggedForDate"] == "2026-10-01"


# ─── critique C-16: read-phase network failures are MeshbookError ──────


class _BrokenBody:
    headers: dict = {}

    def __init__(self, exc):
        self._exc = exc

    def read(self):
        raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("timed out"), ConnectionResetError("reset"),
     __import__("http.client").client.IncompleteRead(b"par", 10)],
    ids=["timeout", "reset", "incomplete"],
)
def test_read_phase_failures_are_typed(client, monkeypatch, exc):
    import meshbook.client as mc

    monkeypatch.setattr(mc, "_urlopen", lambda req, timeout: _BrokenBody(exc))
    with pytest.raises(MeshbookError) as ei:
        client.chat.post("x")
    assert ei.value.code == "network_error" and ei.value.status == 0
    assert "may or may not have been applied" in ei.value.message  # a write: outcome unknown
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.code == "network_error"
    assert "applied" not in ei.value.message  # a read: nothing to warn about


def test_raw_oserror_from_urlopen_is_typed(client, monkeypatch):
    import meshbook.client as mc

    def boom(req, timeout):
        raise ConnectionAbortedError("aborted")

    monkeypatch.setattr(mc, "_urlopen", boom)
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.code == "network_error"


def test_error_body_cut_off_still_raises_the_status(client, monkeypatch):
    import io
    import urllib.error

    import meshbook.client as mc

    class Cut(io.BytesIO):
        def read(self, *a):
            raise TimeoutError("mid-body")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.get_full_url(), 404, "nf", {}, Cut())

    monkeypatch.setattr(mc, "_urlopen", boom)
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.status == 404


# ─── critique C-10: an empty mesh is an error, never a fallthrough ─────


def _cfg_with_mesh(tmp_path):
    import json

    p = tmp_path / "cfg"
    p.write_text(json.dumps({"token": "t", "active_mesh_id": MESH}), encoding="utf-8")
    return p


@pytest.mark.parametrize("kwargs", [{"mesh": ""}, {"mesh": "  "}, {"active_mesh_id": ""}])
def test_empty_client_mesh_is_refused_not_the_persisted_one(tmp_path, kwargs):
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(config_path=_cfg_with_mesh(tmp_path), **kwargs)
    assert ei.value.code == "bad_argument"


@pytest.mark.parametrize("value", ["", "   "])
def test_set_but_empty_env_mesh_is_refused(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MESHBOOK_MESH", value)
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(config_path=_cfg_with_mesh(tmp_path))
    assert ei.value.code == "bad_argument" and "MESHBOOK_MESH" in ei.value.message


def test_unset_env_mesh_still_uses_the_config(tmp_path):
    assert MeshbookClient(config_path=_cfg_with_mesh(tmp_path)).active_mesh_id == MESH


def test_empty_per_call_mesh_is_bad_argument(client, monkeypatch):
    r = install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        client.tasks.list(mesh="")
    assert ei.value.code == "bad_argument"
    with pytest.raises(MeshbookError) as ei:
        client.meshes.leave(mesh="")
    assert ei.value.code == "bad_argument"
    assert r.requests == []


# ─── gap 4: a public, cached me_id() ──────────────────────────────────


def test_me_id_is_cached_per_client(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/me", ok({"authenticated": True, "user": {"id": U1}}))
    assert client.me_id() == U1
    assert client.me_id() == U1
    assert len(r.calls("GET", "/api/me")) == 1
    assert client.me_id(refresh=True) == U1
    assert len(r.calls("GET", "/api/me")) == 2


def test_me_id_after_whoami_costs_nothing_and_no_user_raises(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/me", ok({"authenticated": True, "user": {"id": U1, "username": "w"}}))
    client.whoami()
    assert client.me_id() == U1 and len(r.requests) == 1
    r2 = install(monkeypatch)
    r2.add("GET", "/api/me", ok({"authenticated": True, "user": {}}))
    with pytest.raises(MeshbookError) as ei:
        client.me_id(refresh=True)
    assert ei.value.code == "no_user"
