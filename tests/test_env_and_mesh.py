"""Env precedence and mesh resolution (contract: "Environment and config",
"Mesh resolution"; DEV-DEBT §102.1, the safety item).

The failure §102.1 describes is a write landing in the wrong mesh. So the
tests below are mostly about what X-Active-Mesh-Id carries, and about the
things that must NOT happen: a name guessed when it is ambiguous, a
per-call override that leaks into the next call, or into the config file.
"""
from __future__ import annotations

import json

import pytest

import meshbook.config as cfgmod
from meshbook import Client, MeshbookClient, MeshbookError
from tests._fakes import MESH, MESH2, install, meshes_payload, ok, ok_list


def write_cfg(tmp_path, **values):
    p = tmp_path / "config"
    p.write_text(json.dumps(values), encoding="utf-8")
    return p


# ─── token / base precedence ───────────────────────────────────────────


def test_base_precedence_arg_env_config_default(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, base="https://from-config.example/")
    assert MeshbookClient(config_path=cfg).base == "https://from-config.example"
    monkeypatch.setenv("MESHBOOK_BASE", "https://from-env.example")
    assert MeshbookClient(config_path=cfg).base == "https://from-env.example"
    assert MeshbookClient(base="https://arg.example", config_path=cfg).base == "https://arg.example"
    monkeypatch.delenv("MESHBOOK_BASE")
    assert MeshbookClient(config_path=tmp_path / "none").base == "https://meshbook.org"


def test_token_env_beats_config_and_sets_the_bearer_lane(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, token="mb_token_cfg", auth_mode="agent")
    monkeypatch.setenv("MESHBOOK_TOKEN", "mb_token_env")
    c = MeshbookClient(config_path=cfg)
    assert c.token == "mb_token_env"
    # An env bearer is a deliberate override: it wins over config auth_mode.
    assert c.auth == "bearer" and c.auth_source == "env"


def test_client_alias_is_the_same_class():
    assert Client is MeshbookClient


def test_default_config_path_follows_meshbook_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHBOOK_CONFIG_DIR", str(tmp_path))
    write_cfg(tmp_path, token="mb_token_dir")
    c = MeshbookClient()
    assert c.config_path == tmp_path / "config"
    assert c.token == "mb_token_dir"
    assert c.agent_key_path == tmp_path / "agent-key.pem"


# ─── mesh precedence ───────────────────────────────────────────────────


def _hdr(req):
    return req.get_header("X-active-mesh-id")


def test_mesh_precedence_per_call_client_env_config(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, token="t", active_mesh_id="cfg0cfg0-0000-0000-0000-000000000000")
    r = install(monkeypatch)
    r.add("GET", "/api/notifications", ok_list([]))

    c = MeshbookClient(config_path=cfg)
    c.notifications.list()
    assert _hdr(r.last) == "cfg0cfg0-0000-0000-0000-000000000000"
    assert c.mesh_source == "config"

    monkeypatch.setenv("MESHBOOK_MESH", MESH2)
    c = MeshbookClient(config_path=cfg)
    c.notifications.list()
    assert _hdr(r.last) == MESH2 and c.mesh_source == "env"

    c = MeshbookClient(config_path=cfg, mesh=MESH)
    c.notifications.list()
    assert _hdr(r.last) == MESH and c.mesh_source == "argument"

    third = "33333333-3333-3333-3333-333333333333"
    c.notifications.list(mesh=third)
    assert _hdr(r.last) == third
    c.notifications.list()
    assert _hdr(r.last) == MESH, "a per-call override must not stick"


def test_mesh_name_resolution_is_case_insensitive_and_cached(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "The Tyl Mesh"), (MESH2, "Work")))
    r.add("GET", "/api/tasks", ok_list([]))
    c = MeshbookClient(token="t", config_path=tmp_path / "none", mesh="the tyl MESH")
    c.tasks.list()
    c.tasks.list(mesh="work")
    c.tasks.list(mesh="WORK")
    assert [_hdr(q) for q in r.calls("GET", "/api/tasks")] == [MESH, MESH2, MESH2]
    assert len(r.calls("GET", "/api/meshes")) == 1, "names resolve once per client"
    assert _hdr(r.calls("GET", "/api/meshes")[0]) is None


def test_env_mesh_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHBOOK_MESH", "Work")
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home"), (MESH2, "Work")))
    r.add("GET", "/api/contacts", ok_list([]))
    MeshbookClient(token="t", config_path=tmp_path / "none").contacts.list()
    assert _hdr(r.last) == MESH2


def test_ambiguous_name_is_refused_never_guessed(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Work"), (MESH2, "work")))
    c = MeshbookClient(token="t", config_path=tmp_path / "none")
    with pytest.raises(MeshbookError) as ei:
        c.tasks.list(mesh="Work")
    assert ei.value.code == "mesh_ambiguous"
    assert MESH in ei.value.message and MESH2 in ei.value.message
    assert r.calls("GET", "/api/tasks") == [], "nothing may be sent to a guessed mesh"


def test_unknown_name_lists_the_real_ones(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home")))
    c = MeshbookClient(token="t", config_path=tmp_path / "none")
    with pytest.raises(MeshbookError) as ei:
        c.chat.post("hi", mesh="Nope")
    assert ei.value.code == "mesh_not_found"
    assert "Home" in ei.value.message


def test_uuid_mesh_is_sent_without_a_lookup(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/leads", ok_list([]))
    MeshbookClient(token="t", config_path=tmp_path / "none").leads.list(mesh=MESH2)
    assert r.calls("GET", "/api/meshes") == []
    assert _hdr(r.last) == MESH2


def test_path_scoped_calls_use_the_per_call_mesh_in_path_and_header(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/meshes/{MESH2}/channels", ok_list([{"id": "c", "name": "general"}]))
    c = MeshbookClient(token="t", config_path=tmp_path / "none", mesh=MESH)
    c.channels.list(mesh=MESH2)
    assert _hdr(r.last) == MESH2


def test_account_level_calls_never_send_a_mesh(tmp_path, monkeypatch):
    """A stale or out-of-scope active mesh 403s EVERY route under a scoped
    token (middleware mesh-scope gate) — including the calls you need to
    diagnose and escape that state."""
    r = install(monkeypatch)
    r.add("GET", "/api/me", ok({"user": {"id": "u"}}))
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home")))
    r.add("POST", "/api/meshes/active", ok({}))
    r.add("GET", "/api/meshes/my-pending", ok_list([]))
    c = MeshbookClient(token="t", config_path=tmp_path / "none", mesh=MESH2)
    c.me()
    c.meshes.list_mine()
    c.meshes.use("Home")
    c.meshes.pending_invitations()
    assert all(_hdr(q) is None for q in r.requests)


def test_per_call_mesh_never_touches_the_config_file(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, token="t", active_mesh_id=MESH)
    before = cfg.read_bytes()
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home"), (MESH2, "Work")))
    r.add("POST", "/api/tasks", ok({"id": "t1"}))
    c = MeshbookClient(config_path=cfg)
    c.tasks.create("44444444-4444-4444-4444-444444444444", "x", mesh="Work")
    assert _hdr(r.last) == MESH2
    assert cfg.read_bytes() == before
    assert c.active_mesh_id == MESH


def test_use_persists_only_when_asked(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, token="t", other="kept")
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home"), (MESH2, "Work")))
    r.add("POST", "/api/meshes/active", ok({}))
    c = MeshbookClient(config_path=cfg)
    c.meshes.use("work")
    assert "active_mesh_id" not in cfgmod.load_config(cfg)
    assert c.active_mesh_id == MESH2
    c.meshes.use("Home", persist=True)
    on_disk = cfgmod.load_config(cfg)
    assert on_disk["active_mesh_id"] == MESH
    assert on_disk["other"] == "kept" and on_disk["token"] == "t"


def test_use_by_uuid_still_checks_membership(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home")))
    c = MeshbookClient(token="t", config_path=tmp_path / "none")
    with pytest.raises(MeshbookError) as ei:
        c.meshes.use(MESH2)
    assert ei.value.code == "mesh_not_found"
    assert r.calls("POST", "/api/meshes/active") == []


def test_members_with_a_mesh_no_longer_switches_the_client(tmp_path, monkeypatch):
    """0.3 implemented members(mesh) via use(verify=False), which silently
    changed the client's active mesh."""
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home"), (MESH2, "Work")))
    r.add("GET", f"/api/meshes/{MESH2}/detail", ok({"mesh": {"name": "Work"}, "members": []}))
    c = MeshbookClient(token="t", config_path=tmp_path / "none", mesh=MESH)
    out = c.meshes.members("Work")
    assert out["meshId"] == MESH2 and out["count"] == 0
    assert c.active_mesh_id == MESH
    assert _hdr(r.last) == MESH2


def test_no_mesh_anywhere_is_a_named_error(tmp_path, monkeypatch):
    install(monkeypatch)
    c = MeshbookClient(token="t", config_path=tmp_path / "none")
    with pytest.raises(MeshbookError) as ei:
        c.channels.list()
    assert ei.value.code == "no_active_mesh"
    assert "MESHBOOK_MESH" in ei.value.message


def test_mesh_and_active_mesh_id_conflict_is_refused(tmp_path):
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(token="t", config_path=tmp_path / "none", mesh=MESH, active_mesh_id=MESH2)
    assert ei.value.code == "bad_argument"


def test_user_agent_prefix(tmp_path, monkeypatch):
    import meshbook.client as mc

    r = install(monkeypatch)
    r.add("GET", "/api/me", ok({"user": {"id": "u"}}))
    MeshbookClient(token="t", config_path=tmp_path / "none", user_agent="meshbook-cli/9.9").me()
    assert r.last.get_header("User-agent") == f"meshbook-cli/9.9 {mc.USER_AGENT}"
