"""The agent lane as the single implementation for CLI + MCP (§86/93/96/97/99).

Each test pins a behaviour where meshbook-cli's mesh/agent.py, meshbook-mcp's
inline lane and this SDK differed, and the strictest one was kept.
"""
from __future__ import annotations

import base64
import json
import os
import time

import pytest

import meshbook.config as cfgmod
from meshbook import MeshbookClient, MeshbookError
from tests._fakes import MESH, Err, install, ok, ok_list

pytest.importorskip("cryptography", reason="the agent lane is the [agent] extra")

TOKEN_ENDPOINT = "https://auth.meshbook.org/application/o/token/"
MINT_AUD = "https://auth.meshbook.org/application/o/meshbook-agents/"
TOKEN_PATH = "/application/o/token/"
BUNDLE = {
    "enrolled": True, "username": "wanderer", "kid": "server-kid",
    "tokenEndpoint": TOKEN_ENDPOINT, "clientId": "meshbook-agents",
    "audience": MINT_AUD, "assertionAud": MINT_AUD, "tokenLifetimeSeconds": 300,
}


def _jwt(exp: float) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


def _seed(dirpath, kid="server-kid"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (dirpath / "agent-key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (dirpath / "agent-key.json").write_text(json.dumps({
        "kid": kid, "tokenEndpoint": TOKEN_ENDPOINT, "audience": MINT_AUD,
        "clientId": "meshbook-agents", "username": "wanderer"}), encoding="utf-8")


@pytest.fixture
def cfgdir(tmp_path):
    return tmp_path


def client_in(cfgdir, **kw):
    return MeshbookClient(config_path=cfgdir / "config", **kw)


# ─── readiness + kid ───────────────────────────────────────────────────


def test_ready_needs_key_and_kid(cfgdir):
    c = client_in(cfgdir, token="t")
    assert c.agent.ready() is False
    (cfgdir / "agent-key.pem").write_text("k", encoding="ascii")
    assert c.agent.ready() is False, "a key without a bundle naming its kid cannot mint"
    (cfgdir / "agent-key.json").write_text(json.dumps({"kid": "k1"}), encoding="utf-8")
    assert c.agent.ready() is True


def test_kid_prefix_is_settable_for_cli_and_mcp(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok(dict(BUNDLE, kid=None)))
    c = client_in(cfgdir, token="t")
    c.agent.kid_prefix = "mesh-agent-"
    c.agent.enroll()
    sent_kid = r.body()["publicKey"]["kid"]
    assert sent_kid.startswith("mesh-agent-")
    # No server echo → the local kid is persisted, never None (which would
    # leave a bundle that can never mint).
    assert c.agent.local_meta()["kid"] == sent_kid


# ─── files on disk ─────────────────────────────────────────────────────


@pytest.mark.skipif(os.name != "posix", reason="mode bits are POSIX-only")
def test_key_and_bundle_are_mode_600(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok(BUNDLE))
    c = client_in(cfgdir, token="t")
    c.agent.enroll()
    assert (c.agent.key_path.stat().st_mode & 0o777) == 0o600
    assert (c.agent.meta_path.stat().st_mode & 0o777) == 0o600


def test_key_writes_leave_no_temp_files(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok(BUNDLE))
    c = client_in(cfgdir, token="t")
    c.agent.enroll()
    names = sorted(p.name for p in cfgdir.iterdir())
    assert names == ["agent-key.json", "agent-key.pem"]


def test_register_force_keeps_the_replaced_key_as_bak(cfgdir, monkeypatch):
    _seed(cfgdir)
    old = (cfgdir / "agent-key.pem").read_bytes()
    r = install(monkeypatch)
    r.add("POST", "/api/register/agent", ok(dict(BUNDLE, user_id="u", lobby=True)))
    c = client_in(cfgdir)
    c.agent.register("wanderer", force=True)
    assert (cfgdir / "agent-key.pem.bak").read_bytes() == old
    assert (cfgdir / "agent-key.pem").read_bytes() != old


# ─── persistence of auth_mode ──────────────────────────────────────────


def test_enroll_persist_writes_auth_mode_and_keeps_other_keys(cfgdir, monkeypatch):
    cfgmod.save_config({"token": "mb_token_t", "active_mesh_id": MESH}, cfgdir / "config")
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok(BUNDLE))
    c = client_in(cfgdir)
    c.agent.enroll(persist=True)
    on_disk = cfgmod.load_config(cfgdir / "config")
    assert on_disk["auth_mode"] == "agent"
    assert on_disk["token"] == "mb_token_t" and on_disk["active_mesh_id"] == MESH
    assert c.auth == "agent"


def test_enroll_without_persist_leaves_the_config_alone(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok(BUNDLE))
    client_in(cfgdir, token="t").agent.enroll()
    assert not (cfgdir / "config").exists()


def test_register_persist_and_revoke_persist(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/register/agent", ok(dict(BUNDLE, user_id="u", lobby=True)))
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True}))
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    c = client_in(cfgdir)
    c.agent.register("wanderer", persist=True)
    assert cfgmod.load_config(cfgdir / "config")["auth_mode"] == "agent"
    c.agent.revoke(purge_local=True, persist=True)
    assert "auth_mode" not in cfgmod.load_config(cfgdir / "config")
    assert c.auth == "bearer" and not c.agent.ready()


# ─── config-driven lane (§96) ──────────────────────────────────────────


def test_config_auth_mode_agent_mints_for_every_call(cfgdir, monkeypatch):
    cfgmod.save_config({"auth_mode": "agent", "active_mesh_id": MESH}, cfgdir / "config")
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    r.add("GET", "/api/tasks", ok_list([]))
    c = client_in(cfgdir)
    assert c.auth == "agent" and c.auth_source == "config" and c.auth_mode == "agent"
    c.tasks.list()
    c.tasks.list()
    assert len(r.calls("POST", TOKEN_PATH)) == 1
    assert r.last.get_header("Authorization") == "Bearer at-1"


def test_config_agent_without_a_key_uses_the_stored_bearer(cfgdir, monkeypatch):
    cfgmod.save_config({"auth_mode": "agent", "token": "mb_token_fallback",
                        "active_mesh_id": MESH}, cfgdir / "config")
    r = install(monkeypatch)
    r.add("GET", "/api/tasks", ok_list([]))
    c = client_in(cfgdir)
    assert c.auth_mode == "bearer"
    c.tasks.list()
    assert r.last.get_header("Authorization") == "Bearer mb_token_fallback"


def test_config_agent_mint_failure_falls_back_to_the_bearer(cfgdir, monkeypatch, caplog):
    cfgmod.save_config({"auth_mode": "agent", "token": "mb_token_fallback",
                        "active_mesh_id": MESH}, cfgdir / "config")
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, Err(400, {"error": "invalid_client", "error_description": "no source"}))
    r.add("GET", "/api/tasks", ok_list([]))
    with caplog.at_level("WARNING", logger="meshbook"):
        client_in(cfgdir).tasks.list()
    assert r.last.get_header("Authorization") == "Bearer mb_token_fallback"
    assert "falling back" in caplog.text
    assert "mb_token_fallback" not in caplog.text, "the bearer itself must never be logged"


def test_explicit_agent_auth_never_falls_back(cfgdir, monkeypatch):
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, Err(400, {"error": "invalid_client"}))
    c = client_in(cfgdir, auth="agent", mesh=MESH)
    c.token = "mb_token_present"
    with pytest.raises(MeshbookError) as ei:
        c.tasks.list()
    assert ei.value.code == "invalid_client"


def test_config_agent_with_no_key_and_no_bearer_says_so(cfgdir, monkeypatch):
    cfgmod.save_config({"auth_mode": "agent"}, cfgdir / "config")
    r = install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        client_in(cfgdir, mesh=MESH).tasks.list()
    assert ei.value.code == "not_authenticated" and "agent key" in ei.value.message
    assert r.requests == []


# ─── 401 → one re-mint ─────────────────────────────────────────────────


def test_a_401_on_a_minted_token_remints_once_even_for_post(cfgdir, monkeypatch):
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300},
          {"access_token": "at-2", "expires_in": 300})
    r.add("POST", "/api/tasks", Err(401, {"detail": "token expired"}), ok({"id": "t"}))
    c = client_in(cfgdir, auth="agent", mesh=MESH)
    assert c.tasks.create("44444444-4444-4444-4444-444444444444", "x") == {"id": "t"}
    sent = [q.get_header("Authorization") for q in r.calls("POST", "/api/tasks")]
    assert sent == ["Bearer at-1", "Bearer at-2"]


def test_a_second_401_is_raised_not_looped(cfgdir, monkeypatch):
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1"}, {"access_token": "at-2"})
    r.add("GET", "/api/tasks", Err(401, {"detail": "nope"}))
    c = client_in(cfgdir, auth="agent", mesh=MESH)
    with pytest.raises(MeshbookError) as ei:
        c.tasks.list()
    assert ei.value.status == 401 and ei.value.message == "nope"
    assert len(r.calls("GET", "/api/tasks")) == 2


def test_a_401_on_a_bearer_is_not_retried(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/tasks", Err(401, {"detail": "bad"}))
    with pytest.raises(MeshbookError):
        client_in(cfgdir, token="t", mesh=MESH).tasks.list()
    assert len(r.requests) == 1


# ─── token lifetime: the earlier deadline wins ─────────────────────────


def test_lifetime_is_the_earlier_of_expires_in_and_jwt_exp(cfgdir, monkeypatch):
    _seed(cfgdir)
    r = install(monkeypatch)
    soon = time.time() + 60
    r.add("POST", TOKEN_PATH, {"access_token": _jwt(soon), "expires_in": 300})
    c = client_in(cfgdir, auth="agent")
    c.agent.token()
    assert c._agent_token_expires_at == pytest.approx(soon, abs=3)


def test_mint_uses_the_client_user_agent(cfgdir, monkeypatch):
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at"})
    c = client_in(cfgdir, auth="agent", user_agent="meshbook-mcp/0.8.0")
    c.agent.token()
    assert r.last.get_header("User-agent").startswith("meshbook-mcp/0.8.0 meshbook-sdk/")


def test_whoami_and_status_send_no_mesh(cfgdir, monkeypatch):
    """meshbook-cli sent the active mesh on status/revoke/whoami."""
    _seed(cfgdir)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at"})
    r.add("GET", "/api/me", ok({"user": {"id": "u", "username": "wanderer"}}))
    r.add("GET", "/api/me/agent-credentials", ok({"enrolled": True, "kid": "server-kid"}))
    c = client_in(cfgdir, token="t", mesh=MESH)
    c.agent.whoami()
    c.agent.status()
    assert all(q.get_header("X-active-mesh-id") is None for q in r.requests)


def test_not_enrolled_error_names_keys_not_values(cfgdir, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/me/agent-credentials", ok({"username": "w", "secretish": "VALUE"}))
    with pytest.raises(MeshbookError) as ei:
        client_in(cfgdir, token="t").agent.enroll()
    assert ei.value.code == "not_enrolled"
    assert "VALUE" not in ei.value.message and "secretish" in ei.value.message
