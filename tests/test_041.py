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
