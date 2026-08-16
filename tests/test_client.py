"""meshbook-sdk tests — no live HTTP.

Everything is exercised through the transport seam: the whole SDK funnels
HTTP through ``meshbook.client._urlopen``, so we monkeypatch that one
function with a fake that records the outgoing ``urllib.request.Request``
and replays canned responses. Tests then assert the exact method, path,
headers, and JSON body each namespace method puts on the wire — the same
contract the real server sees.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import meshbook.client as mc
from meshbook import MeshbookClient, MeshbookError

# ─── fake transport ────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload, headers=None):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """Records every Request; pops queued responses (last one sticks)."""

    def __init__(self, *responses):
        self.requests: list = []
        self._responses = list(responses) or [FakeResponse({"ok": True, "data": {}})]

    def __call__(self, req, timeout):
        self.requests.append(req)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    @property
    def last(self):
        return self.requests[-1]

    def last_body(self) -> dict:
        return json.loads(self.last.data.decode("utf-8"))


@pytest.fixture
def client(monkeypatch):
    """Client with a token, an active mesh, and NO config-file bleed."""
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    monkeypatch.delenv("MESHBOOK_BASE", raising=False)
    return MeshbookClient(
        token="mb_token_test",
        active_mesh_id="11111111-1111-1111-1111-111111111111",
        config_path="Z:/nonexistent/config",
    )


def install(monkeypatch, *responses) -> FakeTransport:
    t = FakeTransport(*responses)
    monkeypatch.setattr(mc, "_urlopen", t)
    return t


MESH = "11111111-1111-1111-1111-111111111111"


# ─── token resolution ──────────────────────────────────────────────────


def test_token_resolution_env_then_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(json.dumps({"token": "mb_token_from_config", "active_mesh_id": "cfg-mesh"}),
                   encoding="utf-8")
    # config file only
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    c = MeshbookClient(config_path=cfg)
    assert c.token == "mb_token_from_config"
    assert c.active_mesh_id == "cfg-mesh"  # config also supplies the active mesh
    # env beats config
    monkeypatch.setenv("MESHBOOK_TOKEN", "mb_token_from_env")
    c = MeshbookClient(config_path=cfg)
    assert c.token == "mb_token_from_env"
    # explicit arg beats both
    c = MeshbookClient(token="mb_token_explicit", config_path=cfg)
    assert c.token == "mb_token_explicit"


def test_no_token_raises_before_network(monkeypatch):
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    c = MeshbookClient(config_path="Z:/nonexistent/config")
    t = install(monkeypatch)
    with pytest.raises(MeshbookError) as exc:
        c.notifications.list()
    assert exc.value.code == "not_authenticated"
    assert t.requests == []  # never hit the wire


# ─── headers: UA / bearer / X-Active-Mesh-Id ───────────────────────────


def test_headers_ua_bearer_and_active_mesh(client, monkeypatch):
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"items": [], "total": 0}}))
    client.notifications.list()
    req = t.last
    assert req.get_header("User-agent") == "meshbook-sdk/0.1.0"
    assert req.get_header("Authorization") == "Bearer mb_token_test"
    assert req.get_header("X-active-mesh-id") == MESH
    assert req.get_full_url() == "https://meshbook.org/api/notifications"
    assert req.get_method() == "GET"


# ─── meshes ────────────────────────────────────────────────────────────


def test_meshes_list_mine_and_use(client, monkeypatch):
    rows = [
        {"id": "aaa", "name": "Tyl Mesh", "meshType": "family", "memberRole": "admin"},
        {"id": "bbb", "name": "Work", "meshType": "org", "memberRole": "member"},
    ]
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"items": rows, "total": 2}}))
    meshes = client.meshes.list_mine()
    assert t.last.get_full_url() == "https://meshbook.org/api/meshes"
    assert [m.name for m in meshes] == ["Tyl Mesh", "Work"]
    assert meshes[0].member_role == "admin"
    # use() by case-insensitive name updates the header for later calls
    found = client.meshes.use("tyl mesh")
    assert found.id == "aaa"
    client.notifications.list()
    assert t.last.get_header("X-active-mesh-id") == "aaa"


# ─── contacts ──────────────────────────────────────────────────────────


def test_contacts_create_body_shape(client, monkeypatch):
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"id": "c1", "displayName": "Ada Lovelace"}}))
    out = client.contacts.create("Ada", "Lovelace", email="ada@example.org", company="Analytical")
    req = t.last
    assert req.get_method() == "POST"
    assert req.get_full_url() == "https://meshbook.org/api/contacts"
    assert t.last_body() == {
        "firstName": "Ada", "lastName": "Lovelace",
        "primaryEmail": "ada@example.org", "company": "Analytical",
    }
    assert out["id"] == "c1"  # envelope stripped


def test_contacts_list_query_params(client, monkeypatch):
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"items": [], "total": 0}}))
    client.contacts.list(q="ada", limit=5)
    assert t.last.get_full_url() == "https://meshbook.org/api/contacts?limit=5&search=ada"


# ─── leads ─────────────────────────────────────────────────────────────


def test_leads_move_stage(client, monkeypatch):
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"id": "L1"}}))
    client.leads.move_stage("L1", "stage-9")
    assert t.last.get_method() == "POST"
    assert t.last.get_full_url() == "https://meshbook.org/api/leads/L1/move-stage"
    assert t.last_body() == {"stageId": "stage-9"}


# ─── tasks ─────────────────────────────────────────────────────────────


def test_tasks_done_and_list_mine(client, monkeypatch):
    me = {"ok": True, "data": {"user": {"id": "u-42", "username": "claude"}}}
    tasks = {"ok": True, "data": {"items": [{"id": "t1", "title": "Ship SDK"}], "total": 1}}
    patched = {"ok": True, "data": {"id": "t1", "status": "Done"}}
    t = install(monkeypatch, FakeResponse(me), FakeResponse(tasks), FakeResponse(patched))
    mine = client.tasks.list_mine(status="InProgress")
    assert mine[0]["title"] == "Ship SDK"
    # first call resolved self via /api/me, second listed with assigneeId
    assert t.requests[0].get_full_url() == "https://meshbook.org/api/me"
    assert t.requests[1].get_full_url() == \
        "https://meshbook.org/api/tasks?limit=50&assigneeId=u-42&status=InProgress"
    out = client.tasks.done("t1")
    assert t.last.get_method() == "PATCH"
    assert t.last.get_full_url() == "https://meshbook.org/api/tasks/t1"
    assert t.last_body() == {"status": "Done"}
    assert out["status"] == "Done"


# ─── chat ──────────────────────────────────────────────────────────────


def test_chat_post_and_reply(client, monkeypatch):
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"id": "m1"}}))
    client.chat.post("hello @rook", reply_to="parent-1")
    assert t.last.get_method() == "POST"
    assert t.last.get_full_url() == f"https://meshbook.org/api/entities/mesh/{MESH}/chat"
    assert t.last_body() == {"bodyMd": "hello @rook", "parentMessageId": "parent-1"}


def test_channels_add_member_wire(client, monkeypatch):
    """§88a — resolve channel by name, POST {userId} to /members."""
    chans = {"ok": True, "data": {"items": [{"id": "c-7", "name": "leadership"}]}}
    added = {"ok": True, "data": {"items": [{"userId": "u-9"}]}}
    t = install(monkeypatch, FakeResponse(chans), FakeResponse(added))
    members = client.channels.add_member("#leadership", "u-9")
    assert t.last.get_method() == "POST"
    assert t.last.get_full_url() == "https://meshbook.org/api/channels/c-7/members"
    assert t.last_body() == {"userId": "u-9"}
    assert members == [{"userId": "u-9"}]


def test_chat_search_wire_and_flag(client, monkeypatch):
    """§84 — hybrid search hits /api/chat/search and surfaces `semantic`."""
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"items": [{"id": "h1"}], "total": 1, "semantic": True}}))
    res = client.chat.search("who named herself", limit=3)
    assert t.last.get_full_url() == \
        "https://meshbook.org/api/chat/search?q=who+named+herself&limit=3"
    assert res["semantic"] is True
    assert res["items"] == [{"id": "h1"}]
    assert res["total"] == 1


def test_chat_attach_base64(client, monkeypatch, tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello")
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"id": "a1", "filename": "note.txt",
                              "mimeType": "text/plain", "byteSize": 5}}))
    att = client.chat.attach("m1", f)
    assert t.last.get_full_url() == \
        "https://meshbook.org/api/chat-messages/m1/attachments/json"
    body = t.last_body()
    assert body["filename"] == "note.txt"
    assert body["mimeType"] == "text/plain"
    assert body["base64Bytes"] == "aGVsbG8="  # b64("hello")
    assert att.byte_size == 5  # typed Attachment


def test_chat_requires_active_mesh(monkeypatch):
    c = MeshbookClient(token="mb_token_x", config_path="Z:/nonexistent/config")
    install(monkeypatch)
    with pytest.raises(MeshbookError) as exc:
        c.chat.post("hi")
    assert exc.value.code == "no_active_mesh"


# ─── channels ──────────────────────────────────────────────────────────


def test_channels_post_resolves_name(client, monkeypatch):
    chans = {"ok": True, "data": {"items": [{"id": "ch-9", "name": "bugs"}], "total": 1}}
    posted = {"ok": True, "data": {"id": "m2"}}
    t = install(monkeypatch, FakeResponse(chans), FakeResponse(posted))
    client.channels.post("#Bugs", "found one")
    assert t.requests[0].get_full_url() == f"https://meshbook.org/api/meshes/{MESH}/channels"
    assert t.last.get_full_url() == "https://meshbook.org/api/channels/ch-9/messages"
    assert t.last_body() == {"bodyMd": "found one"}


# ─── files ─────────────────────────────────────────────────────────────


def test_files_attach_list_delete(client, monkeypatch, tmp_path):
    f = tmp_path / "spec.pdf"
    f.write_bytes(b"%PDF")
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"id": "ea1", "filename": "spec.pdf",
                              "mimeType": "application/pdf", "byteSize": 4}}))
    att = client.files.attach("lead", "L1", f)
    assert t.last.get_full_url() == \
        "https://meshbook.org/api/entities/lead/L1/attachments/json"
    assert att.mime_type == "application/pdf"
    client.files.list("lead", "L1")
    assert t.last.get_full_url() == "https://meshbook.org/api/entities/lead/L1/attachments"
    client.files.delete("ea1")
    assert t.last.get_method() == "DELETE"
    assert t.last.get_full_url() == "https://meshbook.org/api/entity-attachments/ea1"


def test_files_download_writes_server_filename(client, monkeypatch, tmp_path):
    t = install(monkeypatch, FakeResponse(
        b"BYTES", headers={"Content-Disposition": 'attachment; filename="spec.pdf"'}))
    out = client.files.download("ea1", tmp_path)  # directory → server filename
    assert t.last.get_full_url() == \
        "https://meshbook.org/api/entity-attachments/ea1/download"
    assert out == tmp_path / "spec.pdf"
    assert out.read_bytes() == b"BYTES"


# ─── exports ───────────────────────────────────────────────────────────


def test_exports_start_list_download(client, monkeypatch, tmp_path):
    job = {"id": "ex1", "meshId": "other-mesh", "status": "pending", "byteSize": None,
           "downloadUrl": None, "expiresAt": None}
    ready = dict(job, status="ready", byteSize=7,
                 downloadUrl="/api/mesh-exports/ex1/download")
    t = install(
        monkeypatch,
        FakeResponse({"ok": True, "data": job}),
        FakeResponse({"ok": True, "data": {"items": [ready], "total": 1}}),
        FakeResponse(b"ZIPDATA", headers={
            "Content-Disposition": 'attachment; filename="meshbook-export-other-mesh.zip"'}),
    )
    started = client.exports.start("other-mesh")
    # start() must send X-Active-Mesh-Id matching the exported mesh…
    assert t.requests[0].get_method() == "POST"
    assert t.requests[0].get_full_url() == "https://meshbook.org/api/meshes/other-mesh/export"
    assert t.requests[0].get_header("X-active-mesh-id") == "other-mesh"
    assert started.status == "pending"
    # …and restore the client's own active mesh afterwards
    assert client.active_mesh_id == MESH
    jobs = client.exports.list("other-mesh")
    assert t.requests[1].get_full_url() == "https://meshbook.org/api/meshes/other-mesh/exports"
    assert jobs[0].status == "ready" and jobs[0].byte_size == 7
    out = client.exports.download("ex1", tmp_path / "backup.zip")
    assert t.last.get_full_url() == "https://meshbook.org/api/mesh-exports/ex1/download"
    assert out.read_bytes() == b"ZIPDATA"


# ─── error mapping ─────────────────────────────────────────────────────


def test_http_error_maps_to_typed_error(client, monkeypatch):
    body = json.dumps({"ok": False, "error": {"code": "forbidden",
                                              "message": "Admins only."}}).encode()

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            req.get_full_url(), 403, "Forbidden", hdrs=None, fp=io.BytesIO(body))

    monkeypatch.setattr(mc, "_urlopen", boom)
    with pytest.raises(MeshbookError) as exc:
        client.leads.list()
    assert exc.value.status == 403
    assert exc.value.code == "forbidden"
    assert exc.value.message == "Admins only."


def test_ok_false_in_200_body_raises(client, monkeypatch):
    install(monkeypatch, FakeResponse(
        {"ok": False, "error": {"code": "mesh_mismatch", "message": "Switch mesh."}}))
    with pytest.raises(MeshbookError) as exc:
        client.notifications.list()
    assert exc.value.code == "mesh_mismatch"


# ─── envelope unwrap ───────────────────────────────────────────────────


def test_items_unwraps_all_three_list_shapes():
    bare = [{"id": 1}]
    assert mc._items(bare) == bare
    assert mc._items({"data": bare}) == bare
    assert mc._items({"ok": True, "data": {"items": bare, "total": 1}}) == bare
    assert mc._items({"ok": True, "data": None}) == []
