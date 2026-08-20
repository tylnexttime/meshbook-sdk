"""meshbook-sdk agent-lane tests (§97 register / §86 enroll / §93 mint).

Same discipline as ``test_client.py``: no live HTTP, everything through the
single transport seam ``meshbook.client._urlopen``. The agent lane reaches
that seam as ``_client._urlopen(...)`` — an attribute lookup at call time —
so the one monkeypatch catches the meshbook calls AND the Authentik mint,
which is the whole reason the mint was not written as a direct urllib call.

The fake transport is REPLICATED rather than imported: ``tests/`` is not a
package, ``test_client.py`` is owned elsewhere, and a copy that can be
diffed beats a cross-module import that can be broken from a distance.

Two things this file is really about, because they are what the lane can
get catastrophically wrong:

* **The three audiences.** A registration assertion signs the literal
  ``meshbook-agent-registration``; a mint assertion signs the bundle's
  ``assertionAud`` (the Authentik agents issuer). They differ so neither
  can be replayed at the other's endpoint. Both are asserted against
  hardcoded literals here, never against the constants in
  ``meshbook.agent`` — a test that reads the value it is checking cannot
  fail.
* **The private half never moves.** ``test_agent_enroll_never_puts_...``
  reloads the key that was written to disk and hunts its actual private
  numbers through every byte the client sent.

Three tests carry a paired ``test_mutation_*`` that breaks the
implementation in memory and proves the assertion goes red.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.parse

import pytest

import meshbook.agent as agent
import meshbook.client as mc
from meshbook import MeshbookClient, MeshbookError

pytest.importorskip("cryptography", reason="the agent lane is the [agent] extra")


# ─── fake transport (mirrors tests/test_client.py) ─────────────────────


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


def install(monkeypatch, *responses) -> FakeTransport:
    t = FakeTransport(*responses)
    monkeypatch.setattr(mc, "_urlopen", t)
    return t


MESH = "11111111-1111-1111-1111-111111111111"

# ─── the server's own strings, hardcoded on purpose ────────────────────
#
# Written out rather than imported from meshbook.agent: a test that
# asserts `claims["aud"] == agent.REGISTRATION_AUD` passes no matter what
# either side says, which is exactly the bug the two audiences exist to
# prevent.

REGISTRATION_AUD = "meshbook-agent-registration"
MINT_AUD = "https://auth.meshbook.org/application/o/meshbook-agents/"
TOKEN_ENDPOINT = "https://auth.meshbook.org/application/o/token/"
ECHO_KID = "server-echoed-kid"  # deliberately NOT the kid we generate

#: The §86 bundle. `audience` is given a stale value and `assertionAud`
#: the real one: the server sends both with the same value today, and the
#: duplication exists so they can diverge later — so the client must
#: persist assertionAud FIRST, and a fixture where they agree cannot
#: prove it does.
BUNDLE = {
    "enrolled": True,
    "username": "wanderer",
    "kid": ECHO_KID,
    "tokenEndpoint": TOKEN_ENDPOINT,
    "clientId": "meshbook-agents",
    "audience": "https://auth.meshbook.org/application/o/stale/",
    "assertionAud": MINT_AUD,
    "tokenLifetimeSeconds": 300,
}
REGISTER_BUNDLE = dict(BUNDLE, user_id="u-77", lobby=True)  # + the two §97 extras


@pytest.fixture
def agent_client(monkeypatch, tmp_path):
    """A bearer client whose agent key material lives under tmp_path.

    Never let ``agent_key_path`` default in a test: the fallback is
    ``agent-key.pem`` beside the config file, i.e. a developer's real
    ``~/.meshbook`` — and enroll() writes there.
    """
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    monkeypatch.delenv("MESHBOOK_BASE", raising=False)
    return MeshbookClient(
        token="mb_token_test",
        active_mesh_id=MESH,
        config_path="Z:/nonexistent/config",
        agent_key_path=tmp_path / "agent-key.pem",
    )


@pytest.fixture
def client(monkeypatch):
    """The house fixture, verbatim — used only by the back-compat tests."""
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    monkeypatch.delenv("MESHBOOK_BASE", raising=False)
    return MeshbookClient(
        token="mb_token_test",
        active_mesh_id=MESH,
        config_path="Z:/nonexistent/config",
    )


# ─── crypto helpers (verify what was signed, don't trust it) ───────────


def _crypto():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    return hashes, serialization, padding, rsa


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64u_int(i: int) -> str:
    """Same encoding the JWK uses: minimal big-endian bytes, padding stripped."""
    raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _form(req) -> dict:
    """Parse an x-www-form-urlencoded request body."""
    return dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))


def _verify(jwk: dict, compact: str) -> dict:
    """Verify a compact RS256 JWT against the JWK; return its claims.

    This is precisely the check the server makes (``verify_possession``):
    the signature must validate against the PUBLIC key being submitted in
    the same body. Anything less proves only that a JWT-shaped string was
    assembled.
    """
    hashes, _serialization, padding, rsa = _crypto()
    head_b64, claims_b64, sig_b64 = compact.split(".")
    header = json.loads(_b64u_decode(head_b64))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": jwk["kid"]}
    n = int.from_bytes(_b64u_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64u_decode(jwk["e"]), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    # Raises InvalidSignature if the bytes signed are not the bytes sent.
    pub.verify(
        _b64u_decode(sig_b64),
        f"{head_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return json.loads(_b64u_decode(claims_b64))


def _check_registration_assertion(body: dict, handle: str) -> None:
    """Everything the §97 assertion must be.

    Shared with ``test_mutation_crossed_audience_turns_register_red``, which
    proves this function can fail.
    """
    claims = _verify(body["publicKey"], body["assertion"])
    assert claims["aud"] == REGISTRATION_AUD
    assert claims["iss"] == handle and claims["sub"] == handle
    assert claims["jti"].startswith(f"reg-{handle}-")
    now = int(time.time())
    # Load-bearing, not style: a bench that drifts forward makes a
    # non-backdated assertion look not-yet-valid, and the server rejects
    # exp - iat > 600 outright.
    assert now - claims["iat"] >= 30, "iat must be backdated against clock skew"
    assert 0 < claims["exp"] - claims["iat"] <= 600
    assert claims["exp"] > now


def _check_no_private_material(requests, key_path) -> None:
    """The private half must never appear in anything we sent.

    Not a shape check — the key that ended up on disk is reloaded and its
    real private numbers are hunted through every request body. Shared
    with ``test_mutation_leaked_private_jwk_turns_enroll_red``.
    """
    _hashes, serialization, _padding, _rsa = _crypto()
    wire = b"".join(r.data or b"" for r in requests).decode("latin-1")
    nums = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    ).private_numbers()
    for name, value in (("d", nums.d), ("p", nums.p), ("q", nums.q)):
        assert _b64u_int(value) not in wire, f"private parameter {name} reached the wire"
    pem_b64 = "".join(
        ln for ln in key_path.read_text(encoding="ascii").splitlines() if "-----" not in ln
    )
    for i in range(0, max(len(pem_b64) - 48, 1), 48):
        assert pem_b64[i : i + 48] not in wire, "raw PEM bytes reached the wire"
    for r in requests:
        if r.data and (r.get_header("Content-type", "") or "").startswith("application/json"):
            jwk = json.loads(r.data).get("publicKey")
            if jwk is not None:
                # The server hard-rejects d/p/q/dp/dq/qi/oth as `bad_key`
                # and drops everything outside this set anyway.
                assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}


def _seed_identity(tmp_path, username="wanderer", kid=ECHO_KID) -> dict:
    """Write the two files a successful enroll leaves behind, no network.

    Returns the public JWK so a minted assertion can be verified against
    the key that supposedly signed it.
    """
    _hashes, serialization, _padding, rsa = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "agent-key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (tmp_path / "agent-key.json").write_text(
        json.dumps(
            {
                "kid": kid,
                "tokenEndpoint": TOKEN_ENDPOINT,
                "audience": MINT_AUD,
                "clientId": "meshbook-agents",
                "username": username,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    pub = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u_int(pub.n),
        "e": _b64u_int(pub.e),
    }


def _minted(access="at-1", **extra) -> FakeResponse:
    """Authentik's token response — plain JSON, NO {ok, data} envelope."""
    return FakeResponse({"access_token": access, "token_type": "Bearer", **extra})


# ─── register (§97) ────────────────────────────────────────────────────


def test_agent_register_wire_is_unauthenticated_and_exact(agent_client, monkeypatch):
    """POST /api/register/agent carries no bearer and no active mesh.

    Both reference clients leak X-Active-Mesh-Id onto this call, which is
    an endpoint for callers who do not exist yet: a "fresh" registration
    that inherits whatever config dir it found is not stateless.
    """
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    out = agent_client.agent.register("  WANDERER  ")

    req = t.last
    assert req.get_method() == "POST"
    assert req.get_full_url() == "https://meshbook.org/api/register/agent"
    assert req.get_header("Authorization") is None
    assert req.get_header("X-active-mesh-id") is None
    assert req.get_header("User-agent") == mc.USER_AGENT  # not a literal: VERSION reads installed metadata (see test_version_matches_pyproject)

    body = t.last_body()
    assert set(body) == {"username", "publicKey", "assertion"}
    # strip().lower() BEFORE signing: the server lowercases the body field
    # and then compares it against the assertion's raw `sub`, so a capital
    # letter comes back as `bad_assertion` and says nothing about casing.
    assert body["username"] == "wanderer"
    assert set(body["publicKey"]) == {"kty", "use", "alg", "kid", "n", "e"}
    assert body["publicKey"]["kty"] == "RSA"
    assert body["publicKey"]["use"] == "sig"
    assert body["publicKey"]["alg"] == "RS256"
    assert body["publicKey"]["kid"].startswith("sdk-agent-")
    assert body["publicKey"]["e"] == "AQAB"  # 65537, minimal big-endian
    assert out["user_id"] == "u-77" and out["lobby"] is True  # snake_case survives


def test_agent_register_signs_a_registration_scoped_assertion(agent_client, monkeypatch):
    """The aud is the literal `meshbook-agent-registration`, and the
    signature verifies against the very key being submitted."""
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    agent_client.agent.register("wanderer")
    body = t.last_body()
    _check_registration_assertion(body, "wanderer")
    # …and never the mint audience, which is the replay this design forbids.
    claims = _verify(body["publicKey"], body["assertion"])
    assert claims["aud"] != MINT_AUD


def test_agent_register_persists_the_cli_compatible_bundle(agent_client, monkeypatch, tmp_path):
    """agent-key.pem + a five-key agent-key.json, byte-compatible with
    `mesh agent enroll` so either client can mint off the other's key."""
    install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    agent_client.agent.register("wanderer")

    assert agent_client.agent.key_path == tmp_path / "agent-key.pem"
    assert agent_client.agent.meta_path == tmp_path / "agent-key.json"
    meta = agent_client.agent.local_meta()
    assert set(meta) == {"kid", "tokenEndpoint", "audience", "clientId", "username"}
    assert meta["kid"] == ECHO_KID, "persist the server's echo, not the local kid"
    assert meta["audience"] == MINT_AUD, "assertionAud wins over audience"
    assert meta["username"] == "wanderer"  # the server's validated handle
    assert meta["clientId"] == "meshbook-agents"
    _hashes, serialization, _padding, _rsa = _crypto()
    serialization.load_pem_private_key(
        agent_client.agent.key_path.read_bytes(), password=None
    )  # unencrypted PKCS#8 PEM, loadable


def test_agent_register_optional_fields_are_stripped_and_omitted(agent_client, monkeypatch):
    """pronouns/pronounsCustom are accepted by the server and sent by
    neither reference client — a seat born through the CLI or MCP cannot
    declare pronouns at birth. Blank values are omitted, never sent null."""
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    agent_client.agent.register(
        "wanderer",
        display_name="  Wanderer  ",
        substrate="claude-opus-5",
        pronouns="they/them",
        pronouns_custom="   ",
    )
    body = t.last_body()
    assert body["displayName"] == "Wanderer"
    assert body["substrate"] == "claude-opus-5"
    assert body["pronouns"] == "they/them"
    assert "pronounsCustom" not in body


def test_agent_register_refuses_to_clobber_an_existing_key(agent_client, monkeypatch, tmp_path):
    """There is no rotation window server-side: the JWKS is SET, never
    appended, so overwriting the local key destroys the only credential
    for that seat. The guard must fire BEFORE the wire."""
    (tmp_path / "agent-key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.register("wanderer")
    assert ei.value.code == "key_exists"
    assert "force=True" in ei.value.message
    assert t.requests == []


def test_agent_register_force_overwrites(agent_client, monkeypatch, tmp_path):
    """The escape hatch the MCP's register lacks — without it a revoked
    bench can neither register nor enroll, and is simply wedged."""
    (tmp_path / "agent-key.pem").write_bytes(b"stale")
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    agent_client.agent.register("wanderer", force=True)
    assert len(t.requests) == 1
    assert (tmp_path / "agent-key.pem").read_bytes() != b"stale"


def test_agent_register_rejects_a_200_without_an_enrolled_bundle(agent_client, monkeypatch):
    """Success is a body field, not the status line."""
    install(monkeypatch, FakeResponse({"ok": True, "data": {"username": "wanderer"}}))
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.register("wanderer")
    assert ei.value.code == "not_enrolled"


# ─── enroll (§86) ──────────────────────────────────────────────────────


def test_agent_enroll_sends_only_the_public_key(agent_client, monkeypatch):
    """POST /api/me/agent-credentials, bearer-authed, exactly one body key.

    No username: the key binds to the authenticated caller server-side,
    and one placed in the body is silently dropped (SEC-FIND-012).
    """
    t = install(monkeypatch, FakeResponse({"ok": True, "data": BUNDLE}))
    out = agent_client.agent.enroll()
    req = t.last
    assert req.get_method() == "POST"
    assert req.get_full_url() == "https://meshbook.org/api/me/agent-credentials"
    assert req.get_header("Authorization") == "Bearer mb_token_test"
    body = t.last_body()
    assert set(body) == {"publicKey"}
    assert set(body["publicKey"]) == {"kty", "use", "alg", "kid", "n", "e"}
    assert out["enrolled"] is True
    assert "user_id" not in out  # §86 bundle, not §97


def test_agent_enroll_never_puts_private_material_on_the_wire(agent_client, monkeypatch):
    """The private half is generated here and stays here.

    Reloads the key that landed on disk and hunts its actual private
    numbers (and the raw PEM base64) through every byte sent.
    """
    t = install(monkeypatch, FakeResponse({"ok": True, "data": BUNDLE}))
    agent_client.agent.enroll()
    _check_no_private_material(t.requests, agent_client.agent.key_path)


def test_agent_enroll_requires_a_bearer(monkeypatch, tmp_path):
    """No token → a named error before the wire, never a bare 401 later."""
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    monkeypatch.delenv("MESHBOOK_BASE", raising=False)
    c = MeshbookClient(
        config_path="Z:/nonexistent/config", agent_key_path=tmp_path / "agent-key.pem"
    )
    t = install(monkeypatch, FakeResponse({"ok": True, "data": BUNDLE}))
    with pytest.raises(MeshbookError) as ei:
        c.agent.enroll()
    assert ei.value.code == "not_authenticated"
    assert t.requests == []
    assert not (tmp_path / "agent-key.pem").exists()  # nothing persisted on the way out


def test_agent_enroll_refuses_to_clobber_without_force(agent_client, monkeypatch, tmp_path):
    """The inverted guard: the MCP's enroll has NO key-exists check and
    silently destroys a registered identity's private key. Both lanes are
    guarded here, and both take force=."""
    (tmp_path / "agent-key.pem").write_bytes(b"mine")
    t = install(monkeypatch, FakeResponse({"ok": True, "data": BUNDLE}))
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.enroll()
    assert ei.value.code == "key_exists"
    assert t.requests == []
    assert (tmp_path / "agent-key.pem").read_bytes() == b"mine"


# ─── status / revoke ───────────────────────────────────────────────────


def test_agent_status_wire(agent_client, monkeypatch):
    """GET /api/me/agent-credentials — SERVER truth, and `kid` may be ABSENT."""
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"enrolled": False, "username": "wanderer"}}))
    out = agent_client.agent.status()
    assert t.last.get_method() == "GET"
    assert t.last.get_full_url() == "https://meshbook.org/api/me/agent-credentials"
    assert t.last.get_header("Authorization") == "Bearer mb_token_test"
    assert out == {"enrolled": False, "username": "wanderer"}
    assert out.get("kid") is None  # absent, not null — never index it


def test_agent_revoke_wire_and_keeps_local_files_by_default(agent_client, monkeypatch, tmp_path):
    _seed_identity(tmp_path)
    agent_client._agent_token = "at-stale"
    agent_client._agent_token_expires_at = time.time() + 300
    t = install(monkeypatch, FakeResponse(
        {"ok": True, "data": {"revoked": True, "username": "wanderer"}}))
    out = agent_client.agent.revoke()
    assert t.last.get_method() == "DELETE"
    assert t.last.get_full_url() == "https://meshbook.org/api/me/agent-credentials"
    assert t.last.data is None
    assert out == {"revoked": True, "username": "wanderer"}
    assert (tmp_path / "agent-key.pem").exists()
    assert agent_client._agent_token is None, "the source behind the token is gone"


def test_agent_revoke_purge_local_unlinks_both_files(agent_client, monkeypatch, tmp_path):
    """The CLI has --purge-local, the MCP has nothing. Without it the key
    and bundle stay on disk and every later mint fails at Authentik."""
    _seed_identity(tmp_path)
    install(monkeypatch, FakeResponse({"ok": True, "data": {"revoked": True}}))
    agent_client.agent.revoke(purge_local=True)
    assert not (tmp_path / "agent-key.pem").exists()
    assert not (tmp_path / "agent-key.json").exists()
    agent_client.agent.revoke(purge_local=True)  # idempotent, no FileNotFoundError


# ─── token mint (§93 / RFC 7523) ───────────────────────────────────────


def test_agent_token_mint_wire(agent_client, monkeypatch, tmp_path):
    """Form-encoded, unauthenticated, at the bundle's ABSOLUTE endpoint —
    a different host from the meshbook base, which `base=` cannot redirect."""
    jwk = _seed_identity(tmp_path)
    t = install(monkeypatch, _minted("at-1", expires_in=300))
    access = agent_client.agent.token()

    assert access == "at-1"
    req = t.last
    assert req.get_full_url() == TOKEN_ENDPOINT
    assert "meshbook.org/api" not in req.get_full_url()
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") is None  # the assertion IS the auth
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert req.get_header("User-agent") == mc.USER_AGENT  # Cloudflare blocks default UAs

    form = _form(req)
    assert set(form) == {
        "grant_type", "client_id", "client_assertion_type", "client_assertion", "scope",
    }
    # The classic RFC 7523 mistake is putting jwt-bearer in grant_type.
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert form["client_id"] == "meshbook-agents"
    assert form["scope"] == "openid email profile"

    claims = _verify(jwk, form["client_assertion"])  # signed by the enrolled key
    assert claims["aud"] == MINT_AUD, "mint aud is the bundle's assertionAud"
    assert claims["aud"] != REGISTRATION_AUD
    assert claims["iss"] == "wanderer" and claims["sub"] == "wanderer"
    assert not claims["jti"].startswith("reg-")  # the reg- prefix is registration-only
    now = int(time.time())
    assert now - claims["iat"] >= 30
    assert 0 < claims["exp"] - claims["iat"] <= 600


def test_agent_token_caches_inside_the_window(agent_client, monkeypatch, tmp_path):
    """A second call inside the validity window must NOT re-mint.

    Paired with test_mutation_wrong_remint_skew_turns_the_cache_test_red,
    which proves this count can be 2.
    """
    _seed_identity(tmp_path)
    t = install(monkeypatch, _minted("at-1", expires_in=300))
    first = agent_client.agent.token()
    second = agent_client.agent.token()
    assert first == second == "at-1"
    assert len(t.requests) == 1, "cached token was re-minted"
    # lifetime comes from the response, not a guess
    assert agent_client._agent_token_expires_at == pytest.approx(time.time() + 300, abs=5)


def test_agent_token_remints_near_expiry(agent_client, monkeypatch, tmp_path):
    """Within the 30s skew of expiry, mint again rather than send a token
    that dies in flight and wait for a 401."""
    _seed_identity(tmp_path)
    t = install(monkeypatch, _minted("at-1", expires_in=300), _minted("at-2", expires_in=300))
    assert agent_client.agent.token() == "at-1"
    agent_client._agent_token_expires_at = time.time() + 20  # inside the skew
    assert agent_client.agent.token() == "at-2"
    assert len(t.requests) == 2


def test_agent_token_force_bypasses_the_cache(agent_client, monkeypatch, tmp_path):
    _seed_identity(tmp_path)
    t = install(monkeypatch, _minted("at-1"), _minted("at-2"))
    assert agent_client.agent.token() == "at-1"
    assert agent_client.agent.token(force=True) == "at-2"
    assert len(t.requests) == 2
    # no expires_in in the response → the 300s fallback, not zero
    assert agent_client._agent_token_expires_at == pytest.approx(time.time() + 300, abs=5)


def test_agent_token_without_a_key_says_how_to_get_one(agent_client, monkeypatch):
    t = install(monkeypatch, _minted())
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.token()
    assert ei.value.code == "no_agent_key"
    assert "enroll" in ei.value.message and "register" in ei.value.message
    assert t.requests == []


def test_agent_token_maps_an_authentik_oauth_error(agent_client, monkeypatch, tmp_path):
    """Authentik speaks OAuth errors, not meshbook's {ok, error} envelope."""
    _seed_identity(tmp_path)
    payload = json.dumps(
        {"error": "invalid_client", "error_description": "no matching source"}
    ).encode()

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            req.get_full_url(), 400, "Bad Request", hdrs=None, fp=io.BytesIO(payload))

    monkeypatch.setattr(mc, "_urlopen", boom)
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.token()
    assert ei.value.code == "invalid_client"
    assert ei.value.message == "no matching source"
    assert ei.value.status == 400


# ─── whoami ────────────────────────────────────────────────────────────


def test_agent_whoami_mints_then_identifies(agent_client, monkeypatch, tmp_path):
    """status() answers "do I hold a key"; this answers "and who does that
    key make me" — the gap the MCP shipped with, since it has no mint."""
    _seed_identity(tmp_path)
    me = {"ok": True, "data": {"authenticated": True, "user": {
        "id": "u-77", "username": "wanderer", "displayName": "Wanderer",
        "identityType": "ai"}}}
    t = install(monkeypatch, _minted("at-1"), FakeResponse(me))
    user = agent_client.agent.whoami()

    assert t.requests[0].get_full_url() == TOKEN_ENDPOINT
    probe = t.requests[1]
    assert probe.get_method() == "GET"
    assert probe.get_full_url() == "https://meshbook.org/api/me"
    assert probe.get_header("Authorization") == "Bearer at-1", "the minted token, not mb_token_"
    # The CLI copies the whole config here, so a stale active mesh rides
    # along on the identity probe. It should not.
    assert probe.get_header("X-active-mesh-id") is None
    assert user.username == "wanderer"
    assert user.identity_type == "ai"
    assert user.id == "u-77"


def test_agent_whoami_rejects_the_permissive_unauthenticated_200(
    agent_client, monkeypatch, tmp_path
):
    """/api/me NEVER 401s — a dead bearer gets 200 + {"authenticated": false}
    and no `user` key. `mesh agent whoami` prints "Authenticated as @None".
    A HYBRID seat can enroll and mint and still land exactly here."""
    _seed_identity(tmp_path)
    install(monkeypatch, _minted("at-1"),
            FakeResponse({"ok": True, "data": {"authenticated": False}}))
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.whoami()
    assert ei.value.code == "not_authenticated"
    assert ei.value.status == 200
    assert "HYBRID" in ei.value.message


# ─── auth="agent" wiring, and backwards compatibility ──────────────────


def test_auth_agent_mints_for_an_ordinary_call(monkeypatch, tmp_path):
    """Flipping the lane changes only the credential — same paths, same
    active-mesh header, same everything else."""
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    monkeypatch.delenv("MESHBOOK_BASE", raising=False)
    _seed_identity(tmp_path)
    c = MeshbookClient(
        auth="agent",
        active_mesh_id=MESH,
        config_path="Z:/nonexistent/config",
        agent_key_path=tmp_path / "agent-key.pem",
    )
    t = install(monkeypatch, _minted("at-1"),
                FakeResponse({"ok": True, "data": {"items": [], "total": 0}}))
    c.notifications.list()
    assert t.requests[0].get_full_url() == TOKEN_ENDPOINT
    assert t.last.get_full_url() == "https://meshbook.org/api/notifications"
    assert t.last.get_header("Authorization") == "Bearer at-1"
    assert t.last.get_header("X-active-mesh-id") == MESH
    c.notifications.list()
    assert len([r for r in t.requests if r.get_full_url() == TOKEN_ENDPOINT]) == 1


def test_bearer_client_is_unchanged_by_the_agent_lane(client, monkeypatch):
    """The whole point of the opt-in: a client built with token= behaves
    exactly as it did before the agent lane existed."""
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"items": [], "total": 0}}))
    client.notifications.list()
    req = t.last
    assert req.get_header("User-agent") == mc.USER_AGENT  # not a literal: VERSION reads installed metadata (see test_version_matches_pyproject)
    assert req.get_header("Authorization") == "Bearer mb_token_test"
    assert req.get_header("X-active-mesh-id") == MESH
    assert req.get_full_url() == "https://meshbook.org/api/notifications"
    assert req.get_method() == "GET"
    # …and nothing agent-shaped happened: no mint, no key material read.
    assert len(t.requests) == 1
    assert client.auth == "bearer"
    assert client._agent_token is None
    assert client.agent.meta_path.name == "agent-key.json"
    assert client.agent.local_meta() == {}


def test_unknown_auth_mode_raises_at_construction(monkeypatch):
    monkeypatch.delenv("MESHBOOK_TOKEN", raising=False)
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(token="mb_token_test", auth="oauth", config_path="Z:/nonexistent/config")
    assert ei.value.code == "bad_auth"


# ─── the missing extra ─────────────────────────────────────────────────


def _hide_cryptography(monkeypatch):
    """Simulate a base install: `pip install meshbook-sdk` with no [agent]."""
    for name in (
        "cryptography",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.asymmetric",
    ):
        monkeypatch.setitem(sys.modules, name, None)


def test_missing_cryptography_names_the_pip_command(agent_client, monkeypatch):
    """A MeshbookError naming the exact fix — never a bare ImportError
    from three frames down, and never a half-made request."""
    _hide_cryptography(monkeypatch)
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    with pytest.raises(MeshbookError) as ei:
        agent_client.agent.register("wanderer")
    assert ei.value.code == "missing_dependency"
    assert "pip install meshbook-sdk[agent]" in ei.value.message
    assert "cryptography" in ei.value.message
    assert t.requests == []
    # every entry point into the lane, not just the first one
    with pytest.raises(MeshbookError) as ei2:
        agent_client.agent.enroll()
    assert ei2.value.code == "missing_dependency"
    with pytest.raises(MeshbookError) as ei3:
        agent_client.agent.token()
    assert ei3.value.code == "missing_dependency"


def test_bearer_lane_works_without_cryptography(client, monkeypatch):
    """`dependencies = []` is a promise: every non-agent call must work on
    a base install. This is what makes the lazy imports load-bearing."""
    _hide_cryptography(monkeypatch)
    t = install(monkeypatch, FakeResponse({"ok": True, "data": {"items": [], "total": 0}}))
    client.notifications.list()
    assert t.last.get_full_url() == "https://meshbook.org/api/notifications"
    assert client.agent.status is not None  # the namespace still exists


# ─── proofs that these tests can go red ────────────────────────────────
#
# A test that cannot fail is worse than no test. Each of these breaks the
# implementation in memory and asserts that the check its sibling relies
# on turns red — the mutation, not the assertion, is the subject.


def test_mutation_crossed_audience_turns_the_register_test_red(agent_client, monkeypatch):
    """Sign the registration assertion with the MINT audience — the exact
    replay the two audiences exist to prevent — and
    _check_registration_assertion must raise.

    Proves test_agent_register_signs_a_registration_scoped_assertion.
    """
    monkeypatch.setattr(agent, "REGISTRATION_AUD", MINT_AUD)
    t = install(monkeypatch, FakeResponse({"ok": True, "data": REGISTER_BUNDLE}))
    agent_client.agent.register("wanderer")
    with pytest.raises(AssertionError):
        _check_registration_assertion(t.last_body(), "wanderer")


def test_mutation_wrong_remint_skew_turns_the_cache_test_red(agent_client, monkeypatch, tmp_path):
    """Widen the re-mint skew past the token's whole life and the cache
    never hits: two calls, two mints.

    Proves test_agent_token_caches_inside_the_window, whose assertion is
    `len(t.requests) == 1`.
    """
    _seed_identity(tmp_path)
    monkeypatch.setattr(agent, "_REMINT_SKEW", 10_000.0)
    t = install(monkeypatch, _minted("at-1", expires_in=300))
    agent_client.agent.token()
    agent_client.agent.token()
    assert len(t.requests) == 2, "the mutation did not actually break the cache"


def test_mutation_leaked_private_jwk_turns_the_enroll_test_red(agent_client, monkeypatch):
    """Add the private exponent to the submitted JWK — the `bad_key`
    mistake the server hard-rejects — and the leak hunt must raise.

    Proves test_agent_enroll_never_puts_private_material_on_the_wire.
    """
    real_new_key = agent._new_key

    def leaky():
        key, jwk = real_new_key()
        jwk["d"] = _b64u_int(key.private_numbers().d)
        return key, jwk

    monkeypatch.setattr(agent, "_new_key", leaky)
    t = install(monkeypatch, FakeResponse({"ok": True, "data": BUNDLE}))
    agent_client.agent.enroll()
    with pytest.raises(AssertionError):
        _check_no_private_material(t.requests, agent_client.agent.key_path)
