"""meshbook-sdk 0.4.2: the C-01 key proof, recurrence zones, has_more,
chat order docs, the 401 hint and server_too_old.

Every test here was written before its fix and seen red first (the report
in the review folder lists which). The server-side rules the key-proof
tests check are copied from the server's own verifier
(meshbook app/services/agent_enroll.py, verify_key_action) and written out
as literals: a test that read the SDK's constants could not fail.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse

import pytest

from meshbook import MeshbookClient, MeshbookError
from tests._fakes import MESH, Err, install, ok

TOKEN_ENDPOINT = "https://auth.meshbook.org/application/o/token/"
TOKEN_PATH = "/application/o/token/"
MINT_AUD = "https://auth.meshbook.org/application/o/meshbook-agents/"

# The server's literals (agent_enroll.py KEY_ACTION_AUD, me.py KEY_PROOF_HEADER).
KEY_ACTION_AUD = "meshbook-agent-key-action"
PROOF_HEADER = "X-agent-key-proof"  # urllib stores header names capitalize()d
SERVER_MAX_AGE = 600
SERVER_SKEW = 90


# ─── helpers ───────────────────────────────────────────────────────────


def _seed(dirpath, kid="kid-current", username="wanderer", stem="agent-key"):
    """Write a key + bundle the way a successful enroll does; return the key."""
    pytest.importorskip("cryptography", reason="the agent lane is the [agent] extra")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (dirpath / f"{stem}.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (dirpath / f"{stem}.json").write_text(json.dumps({
        "kid": kid, "tokenEndpoint": TOKEN_ENDPOINT, "audience": MINT_AUD,
        "clientId": "meshbook-agents", "username": username}), encoding="utf-8")
    return key


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def server_verify_key_action(proof: str, public_key, username: str, action: str) -> dict:
    """The server's verify_key_action, rule for rule, on cryptography.

    authlib's jwt.decode picks the algorithm from the header and checks the
    RSA signature against the enrolled JWK; claims.validate checks exp/iat
    with 90 s leeway; then sub, aud (str, or first of a list), action, numeric
    iat/exp, lifetime <= 600 and the skew window.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    assert isinstance(proof, str) and proof.count(".") == 2, "must be a compact JWT"
    h, p, s = proof.split(".")
    header = json.loads(_b64d(h))
    assert header["alg"] == "RS256", header
    public_key.verify(_b64d(s), f"{h}.{p}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(_b64d(p))
    assert claims.get("sub") == username
    aud = claims.get("aud")
    assert (aud if isinstance(aud, str) else (aud or [None])[0]) == KEY_ACTION_AUD
    assert claims.get("action") == action
    iat, exp = claims.get("iat"), claims.get("exp")
    assert isinstance(iat, (int, float)) and isinstance(exp, (int, float))
    assert exp - iat <= SERVER_MAX_AGE
    now = time.time()
    assert not (iat > now + SERVER_SKEW or exp < now - SERVER_SKEW), "expired or not yet valid"
    return claims


def _agent_client(tmp_path, **kw):
    return MeshbookClient(
        auth="agent", active_mesh_id=MESH, config_path=tmp_path / "config",
        agent_key_path=tmp_path / "agent-key.pem", **kw)


# ─── 1. the C-01 key proof ─────────────────────────────────────────────


def test_revoke_on_the_agent_lane_signs_a_proof_bound_to_revoke(tmp_path, monkeypatch):
    key = _seed(tmp_path)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True, "hadKey": True}))
    c = _agent_client(tmp_path)
    c.agent.revoke()
    req = r.calls("DELETE", "/api/me/agent-credentials")[0]
    assert req.get_header("Authorization") == "Bearer at-1"
    proof = req.get_header(PROOF_HEADER)
    assert proof, "the agent-JWT lane needs X-Agent-Key-Proof since the C-01 hotfix"
    server_verify_key_action(proof, key.public_key(), "wanderer", "revoke")
    assert json.loads(_b64d(proof.split(".")[0]))["kid"] == "kid-current"


def test_revoke_proof_cannot_double_as_an_unlink_or_rotation_proof(tmp_path, monkeypatch):
    key = _seed(tmp_path)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True}))
    _agent_client(tmp_path).agent.revoke()
    proof = r.calls("DELETE")[0].get_header(PROOF_HEADER)
    with pytest.raises(AssertionError):
        server_verify_key_action(proof, key.public_key(), "wanderer", "unlink:moltbook:x")
    claims = json.loads(_b64d(proof.split(".")[1]))
    assert claims["aud"] != "meshbook-agent-rotation" and "new_kid" not in claims


def test_revoke_on_the_bearer_lane_sends_no_proof(tmp_path, monkeypatch):
    """A bearer or session needs no proof (break-glass for a lost key), and a
    signed proof is not sent where nothing reads it."""
    _seed(tmp_path)
    r = install(monkeypatch)
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True}))
    c = MeshbookClient(token="mb_token_t", config_path=tmp_path / "config",
                       agent_key_path=tmp_path / "agent-key.pem")
    c.agent.revoke()
    assert r.last.get_header(PROOF_HEADER) is None
    assert r.last.get_header("Authorization") == "Bearer mb_token_t"


def test_revoke_proof_is_signed_by_the_key_the_token_came_from(tmp_path, monkeypatch):
    """An interrupted enroll left a pending key the server switched to: the
    mint promotes it, so the proof must be signed by THAT key, not the dead one."""
    _seed(tmp_path, kid="kid-old")
    new = _seed(tmp_path, kid="kid-new", stem="agent-key.pem.pending_tmp")
    (tmp_path / "agent-key.pem.pending_tmp.pem").replace(tmp_path / "agent-key.pem.pending")
    (tmp_path / "agent-key.pem.pending_tmp.json").replace(tmp_path / "agent-key.json.pending")
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH,
          Err(401, {"error": "invalid_client", "error_description": "bad key"}),
          {"access_token": "at-new", "expires_in": 300})
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True}))
    _agent_client(tmp_path).agent.revoke()
    proof = r.calls("DELETE")[0].get_header(PROOF_HEADER)
    server_verify_key_action(proof, new.public_key(), "wanderer", "revoke")


def test_unlink_oauth_on_the_agent_lane_binds_provider_and_id(tmp_path, monkeypatch):
    key = _seed(tmp_path)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    path = "/api/me/oauth-accounts/Moltbook/mb%20user%3A7"
    r.add("DELETE", path, ok({"provider": "moltbook", "providerUserId": "mb user:7",
                              "unlinked": True}))
    out = _agent_client(tmp_path).agent.unlink_oauth("Moltbook", "mb user:7")
    assert out["unlinked"] is True
    req = r.calls("DELETE", path)[0]
    assert req.get_header("X-active-mesh-id") is None, "account-level: no mesh header"
    # The server builds the action from the DECODED path, provider lowercased.
    server_verify_key_action(req.get_header(PROOF_HEADER), key.public_key(), "wanderer",
                             "unlink:moltbook:mb user:7")


def test_unlink_oauth_on_the_bearer_lane_sends_no_proof(tmp_path, monkeypatch):
    r = install(monkeypatch)
    r.add("DELETE", "/api/me/oauth-accounts/google/123", ok({"unlinked": True}))
    c = MeshbookClient(token="mb_token_t", config_path=tmp_path / "config",
                       agent_key_path=tmp_path / "agent-key.pem")
    c.agent.unlink_oauth("google", "123")
    assert r.last.get_header(PROOF_HEADER) is None


@pytest.mark.parametrize("provider,uid", [("", "1"), ("google", ""), ("  ", "1"),
                                          ("google", "a/b")])
def test_unlink_oauth_refuses_unroutable_arguments_before_sending(tmp_path, monkeypatch,
                                                                  provider, uid):
    r = install(monkeypatch)
    c = MeshbookClient(token="mb_token_t", config_path=tmp_path / "config")
    with pytest.raises(MeshbookError) as ei:
        c.agent.unlink_oauth(provider, uid)
    assert ei.value.code == "bad_argument"
    assert r.requests == []


def test_key_proof_is_refused_without_a_local_key(tmp_path):
    c = _agent_client(tmp_path)
    with pytest.raises(MeshbookError) as ei:
        c.agent.key_proof("revoke")
    assert ei.value.code == "no_agent_key"


def test_key_proof_never_quotes_the_private_key_in_errors(tmp_path):
    (tmp_path / "agent-key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nSECRETSTUFF\n")
    (tmp_path / "agent-key.json").write_text(json.dumps({"kid": "k", "username": "w"}))
    c = _agent_client(tmp_path)
    with pytest.raises(MeshbookError) as ei:
        c.agent.key_proof("revoke")
    assert "SECRETSTUFF" not in str(ei.value)


def test_the_proof_header_is_not_in_the_url(tmp_path, monkeypatch):
    _seed(tmp_path)
    r = install(monkeypatch)
    r.add("POST", TOKEN_PATH, {"access_token": "at-1", "expires_in": 300})
    r.add("DELETE", "/api/me/agent-credentials", ok({"revoked": True}))
    _agent_client(tmp_path).agent.revoke()
    req = r.calls("DELETE")[0]
    assert urllib.parse.urlsplit(req.get_full_url()).query == ""


# ─── 2. recurrence time zone (tzid) ────────────────────────────────────

import datetime as dt  # noqa: E402

from meshbook import recurrence_rule  # noqa: E402
from tests._fakes import E1  # noqa: E402

SYD = dt.timezone(dt.timedelta(hours=11))
START = dt.datetime(2027, 3, 22, 10, 0, tzinfo=SYD)
END = dt.datetime(2027, 3, 22, 11, 0, tzinfo=SYD)
EV = f"/api/calendar-events/{E1}"
KNOWN = {"Australia/Sydney", "Europe/Warsaw", "UTC", "America/Argentina/Buenos_Aires"}


@pytest.fixture
def zones(monkeypatch):
    """A tz database with four zones, whatever this machine has (Windows
    Pythons often have none: no tzdata package, no system zoneinfo)."""
    import meshbook._times as t

    def load(name):
        if name not in KNOWN:
            raise KeyError(name)
        return name

    monkeypatch.setattr(t, "_load_zone", load)
    monkeypatch.setattr(t, "_zone_db_available", lambda: True)


@pytest.fixture
def no_zone_db(monkeypatch):
    import meshbook._times as t

    def load(name):
        raise KeyError(name)

    monkeypatch.setattr(t, "_load_zone", load)
    monkeypatch.setattr(t, "_zone_db_available", lambda: False)


@pytest.fixture
def ev_client(tmp_path):
    return MeshbookClient(token="mb_token_test", mesh=MESH, config_path=tmp_path / "none")


def test_recurrence_rule_carries_tzid(zones):
    assert recurrence_rule("weekly", tzid="Australia/Sydney") == {
        "frequency": "Weekly", "intervalN": 1, "untilDate": None, "tzid": "Australia/Sydney"}
    assert "tzid" not in recurrence_rule("weekly"), "no tzid unless asked: older wire unchanged"


def test_set_recurrence_sends_tzid(ev_client, monkeypatch, zones):
    r = install(monkeypatch)
    r.add("PUT", EV + "/recurrence", ok({"id": "r1"}))
    ev_client.events.set_recurrence(E1, "weekly", tzid="Australia/Sydney")
    assert r.body()["tzid"] == "Australia/Sydney"


def test_unknown_zone_is_bad_argument_before_the_wire(ev_client, monkeypatch, zones):
    r = install(monkeypatch)
    for call in (
        lambda: ev_client.events.set_recurrence(E1, "weekly", tzid="Mars/Olympus"),
        lambda: ev_client.events.create("x", START, END, recurrence="weekly", tz="Mars/Olympus"),
        lambda: ev_client.events.create(
            "x", START, END, recurrence={"frequency": "weekly", "tzid": "Australia/Sydny"}),
        lambda: ev_client.events.update(E1, recurrence={"frequency": "weekly", "tzid": "Nope"}),
        lambda: ev_client.events.list(tz="Mars/Olympus"),
        lambda: recurrence_rule("weekly", tzid=""),
        lambda: recurrence_rule("weekly", tzid=42),
    ):
        with pytest.raises(MeshbookError) as ei:
            call()
        assert ei.value.code == "bad_argument", ei.value
    assert r.requests == []


def test_create_tz_defaults_the_recurrence_tzid(ev_client, monkeypatch, zones):
    """Alder's field note 1: `create --tz` stored nothing, so the series
    slid an hour at the DST change. tz on create now rides on the rule."""
    r = install(monkeypatch)
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    ev_client.events.create("Weekly", START, END, recurrence="weekly", tz="Australia/Sydney")
    assert r.body()["recurrence"]["tzid"] == "Australia/Sydney"
    ev_client.events.create("W2", START, END, tz="Australia/Sydney",
                            recurrence={"frequency": "weekly", "interval": 2})
    assert r.body()["recurrence"] == {"frequency": "Weekly", "intervalN": 2,
                                      "untilDate": None, "tzid": "Australia/Sydney"}
    # a wire-shaped rule gets the default too, without mutating the caller's dict
    rule = {"frequency": "Weekly", "intervalN": 1, "untilDate": None}
    ev_client.events.create("W3", START, END, tz="Australia/Sydney", recurrence=rule)
    assert r.body()["recurrence"]["tzid"] == "Australia/Sydney" and "tzid" not in rule


def test_an_explicit_rule_tzid_beats_create_tz(ev_client, monkeypatch, zones):
    r = install(monkeypatch)
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    ev_client.events.create("x", START, END, tz="Australia/Sydney",
                            recurrence={"frequency": "weekly", "tzid": "Europe/Warsaw"})
    assert r.body()["recurrence"]["tzid"] == "Europe/Warsaw"
    ev_client.events.create("y", START, END, tz="Australia/Sydney",
                            recurrence=recurrence_rule("daily", tzid="UTC"))
    assert r.body()["recurrence"]["tzid"] == "UTC"


def test_create_tz_without_recurrence_sends_no_tz_field(ev_client, monkeypatch, zones):
    r = install(monkeypatch)
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    ev_client.events.create("x", START, END, tz="Australia/Sydney")
    assert "recurrence" not in r.body() and "tz" not in r.body() and "tzid" not in r.body()


def test_update_recurrence_takes_tzid(ev_client, monkeypatch, zones):
    r = install(monkeypatch)
    r.add("PATCH", EV, ok({"id": E1}))
    ev_client.events.update(E1, recurrence=recurrence_rule("weekly", tzid="Europe/Warsaw"))
    assert r.body() == {"recurrence": {"frequency": "Weekly", "intervalN": 1,
                                       "untilDate": None, "tzid": "Europe/Warsaw"}}


def test_a_zoneinfo_object_is_accepted_by_its_key(ev_client, monkeypatch, zones):
    class FakeZone:  # what zoneinfo.ZoneInfo exposes: .key
        key = "Australia/Sydney"

    assert recurrence_rule("weekly", tzid=FakeZone())["tzid"] == "Australia/Sydney"


def test_without_a_tz_database_a_well_formed_name_passes_to_the_server(no_zone_db):
    """No tzdata (stock Windows Python): the SDK cannot tell a real zone from
    a typo, so it checks the shape only and leaves the rest to the server's
    invalid_timezone. Refusing every zone there would break the bench in
    Alder's field notes."""
    assert recurrence_rule("weekly", tzid="Australia/Sydney")["tzid"] == "Australia/Sydney"
    assert recurrence_rule("weekly", tzid=" Europe/Warsaw ")["tzid"] == "Europe/Warsaw"
    for bad in ("../../etc/passwd", "Sydney/", "/UTC", "a b", "x\\y", "Mars.Olympus"):
        with pytest.raises(MeshbookError) as ei:
            recurrence_rule("weekly", tzid=bad)
        assert ei.value.code == "bad_argument"


def test_real_zoneinfo_when_this_machine_has_one():
    """The un-faked path: runs wherever a tz database exists (CI on Linux)."""
    import zoneinfo

    if not zoneinfo.available_timezones():
        pytest.skip("no tz database on this machine (install tzdata)")
    assert recurrence_rule("weekly", tzid="Australia/Sydney")["tzid"] == "Australia/Sydney"
    with pytest.raises(MeshbookError):
        recurrence_rule("weekly", tzid="Mars/Olympus")


# ─── 3. ItemList.has_more honours the server's hasMore ─────────────────

from meshbook.models import ItemList  # noqa: E402

CHAT = f"/api/entities/mesh/{MESH}/chat"
CH = "cccccccc-1111-1111-1111-000000000001"


def _msgs(n):
    return [{"id": f"m{i}", "createdAt": f"2026-10-19T10:{i:02d}:00+00:00"} for i in range(n)]


def test_a_full_chat_page_with_hasmore_false_is_the_last_page(ev_client, monkeypatch):
    """Exactly `limit` messages left: the page arithmetic said "more", the
    server (which peeks one extra row) says no."""
    r = install(monkeypatch)
    r.add("GET", CHAT, ok({"items": _msgs(20), "pinned": [], "hasMore": False}))
    assert ev_client.chat.list(limit=20).has_more is False


def test_a_default_limit_chat_page_with_hasmore_true_says_more(ev_client, monkeypatch):
    """No limit sent: the server applies the mesh default (typically 100)
    and says hasMore; the arithmetic had nothing to compare against."""
    r = install(monkeypatch)
    r.add("GET", CHAT, ok({"items": _msgs(5), "pinned": [], "hasMore": True}))
    assert ev_client.chat.list(limit=None).has_more is True


def test_channel_pages_honour_hasmore_too(ev_client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/channels/{CH}/messages",
          ok({"items": _msgs(3), "pinned": [], "hasMore": False}))
    assert ev_client.channels.read(CH, limit=3).has_more is False


def test_itemlist_has_more_rules():
    assert ItemList([1, 2], limit=2, meta={"hasMore": False}).has_more is False
    assert ItemList([1], limit=5, meta={"hasMore": True}).has_more is True
    # not a bool: ignored, the arithmetic decides
    assert ItemList([1, 2], limit=2, meta={"hasMore": None}).has_more is True
    assert ItemList([1, 2], limit=2, meta={"hasMore": "no"}).has_more is True
    # the SDK cut a page client-side: rows it holds back still count as more
    assert ItemList([1, 2], total=5, limit=2, offset=0,
                    meta={"hasMore": False, "client_paginated": True}).has_more is True


# ─── 4. chat order: oldest first, as the server sends it ───────────────


def test_chat_reads_keep_the_servers_oldest_first_order(ev_client, monkeypatch):
    r = install(monkeypatch)
    rows = _msgs(3)
    r.add("GET", CHAT, ok({"items": rows, "pinned": [], "hasMore": False}))
    r.add("GET", f"/api/channels/{CH}/messages", ok({"items": rows, "hasMore": False}))
    assert [m["id"] for m in ev_client.chat.list()] == ["m0", "m1", "m2"]
    assert [m["id"] for m in ev_client.channels.read(CH)] == ["m0", "m1", "m2"]


def test_chat_docstrings_say_oldest_first():
    from meshbook.resources.chat import Channels, Chat, Dms

    for fn in (Chat.list, Channels.read, Dms.read):
        doc = fn.__doc__ or ""
        assert "newest first" not in doc, fn.__qualname__
        assert "oldest first" in doc, fn.__qualname__
