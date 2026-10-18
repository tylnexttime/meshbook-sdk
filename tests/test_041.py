"""0.4.1: the SDK gaps meshbook-mcp hit porting onto 0.4.0 (REPORT-MCP
2026-10-19, "SDK gaps" 2-8), each pinned on the wire."""
from __future__ import annotations

import pytest

from meshbook import MeshbookClient
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
