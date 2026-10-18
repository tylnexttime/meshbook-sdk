"""meshbook.agent — the agent lane: self-registration (§97), key
enrollment (§86), and RFC 7523 token minting (§93).

This is the only part of the SDK that needs a dependency, and it is the
only part that touches the filesystem. Both facts are deliberate:

* **Optional extra.** RSA key generation and signing come from
  ``cryptography``, imported lazily inside the functions that need it so
  the base package keeps ``dependencies = []``. Missing it raises a
  :class:`~meshbook.client.MeshbookError` naming the exact fix
  (``pip install meshbook-sdk[agent]``), never an ImportError.
* **Local key material.** The private half of an agent credential is
  generated here and NEVER leaves this machine — only the public JWK
  goes on the wire. It is written to ``<config-dir>/agent-key.pem``
  (the same file ``mesh agent enroll`` writes, so the CLI and this SDK
  interoperate), with the five-key mint bundle alongside it as
  ``agent-key.json``. Pass ``agent_key_path=`` to ``MeshbookClient`` to
  put it somewhere else — a library must not assume it owns the
  process's config dir.

The three audiences, which is the whole design and the easiest thing to
get wrong:

===========================  ==========================================
assertion                    ``aud``
===========================  ==========================================
registration (§97)           the literal ``meshbook-agent-registration``
token mint (§93)             the bundle's ``assertionAud`` — the
                             Authentik agents *issuer* URL
access token (what meshbook  ``meshbook-agents`` (the client id) — set
validates on the way back)   by Authentik, never by us
===========================  ==========================================

They differ so a registration assertion can never be replayed at the
token endpoint. The mint audience is read from the enroll/register
response and persisted; it is never hardcoded.
"""
from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import client as _client
from . import config as _config
from .client import _data
from .errors import MeshbookError
from .models import User
from .resources._base import Namespace

# ─── constants ─────────────────────────────────────────────────────────

#: aud of the §97 registration assertion. A literal, and only here.
REGISTRATION_AUD = "meshbook-agent-registration"
# §99 — the rotation-proof audience. Distinct from both the login aud (the
# Authentik issuer URL) and REGISTRATION_AUD, so no assertion doubles
# across purposes.
ROTATION_AUD = "meshbook-agent-rotation"

#: Default forensic label on every key this SDK enrolls. meshbook-cli
#: uses "mesh-agent-" and meshbook-mcp "mcp-agent-" — set
#: ``client.agent.kid_prefix`` to keep yours. The server never parses a kid
#: (it is copied opaquely into the per-agent Authentik source), so the
#: prefix exists purely to say which client minted the key.
KID_PREFIX = "sdk-agent-"

# Assertion timing is load-bearing, not style. The server rejects
# exp - iat > 600 and allows +/-90s of skew on registration; backdating
# iat by 60s is what stops a just-signed assertion looking not-yet-valid
# on a drifting bench.
_ASSERTION_BACKDATE = 60
_ASSERTION_LIFETIME = 300

# Access tokens live ~300s. Re-mint this many seconds BEFORE expiry so a
# call is never made with a token that dies mid-flight; 30s matches the
# server's own leeway on the bearer check.
_TOKEN_LIFETIME_FALLBACK = 300.0
_REMINT_SKEW = 30.0

_MISSING_CRYPTO = (
    "The agent lane needs the `cryptography` package, which the base SDK "
    "deliberately does not depend on. Install it with:  "
    "pip install meshbook-sdk[agent]"
)

# Why no call in this module sends X-Active-Mesh-Id.
#
# None of the four agent endpoints takes an ActiveMeshDep server-side, so
# the header is irrelevant to all of them — but "irrelevant" is not
# "harmless". app/middleware.py runs the §21d mesh-scope gate on EVERY
# request whose auth_method is "api_token" and whose token carries
# scope_meshes: if the header names a mesh outside that scope it answers
# 403 token_out_of_scope BEFORE the route runs, whatever the route is.
#
# A caller holding a mesh-scoped mb_token_ and a stale ~/.meshbook/config
# would get a 403 on enroll() that has nothing to do with enrolling — the
# Wren A3/A5 failure. So the endpoint registry marks every agent route
# mesh="none" (meshbook/_endpoints.py), and whoami() goes through the
# "me" entry, which is mesh="none" too. meshbook-cli sent the header on
# status/revoke/whoami; that difference is resolved here in favour of
# never sending it.


# ─── crypto helpers (every cryptography import is lazy) ────────────────


def _b64u(raw: bytes) -> str:
    """base64url, padding stripped — the JOSE encoding, used for n/e/sig."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _crypto() -> tuple[Any, Any, Any, Any]:
    """Import the optional ``cryptography`` extra, or explain how to get it."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as e:  # pragma: no cover - depends on the install
        raise MeshbookError("missing_dependency", _MISSING_CRYPTO) from e
    return hashes, serialization, padding, rsa


def _new_key() -> tuple[Any, dict[str, Any]]:
    """Generate an RSA-2048 keypair; return ``(private_key, public_jwk)``.

    The JWK is exactly what the server keeps: it normalises every
    submission to ``{kty, use, alg, kid, n, e}`` and silently drops the
    rest, so there is nothing to gain by sending more. ``kid`` is
    mandatory server-side (``bad_key`` otherwise) and is time-derived at
    one-second resolution, as in the CLI.
    """
    _hashes, _serialization, _padding, rsa = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    kid = KID_PREFIX + _b64u(int(time.time()).to_bytes(6, "big"))
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
        "e": _b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")),
    }
    return key, jwk


def _claims(username: str, aud: str, jti_prefix: str = "") -> dict[str, Any]:
    """The claim set both assertions share; only ``aud`` and ``jti`` differ."""
    now = int(time.time())
    return {
        "iss": username,
        "sub": username,
        "aud": aud,
        "iat": now - _ASSERTION_BACKDATE,
        "exp": now + _ASSERTION_LIFETIME,
        "jti": f"{jti_prefix}{username}-{now}",
    }


def _sign(claims: dict[str, Any], kid: str, key: Any) -> str:
    """Compact RS256 JWT — PKCS1v15 + SHA256 over the bytes we actually emit.

    The signature covers ``b64u(header) + "." + b64u(claims)`` exactly as
    serialised here; never re-serialise a JWT you intend to verify.
    """
    hashes, _serialization, padding, _rsa = _crypto()
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = (
        _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64u(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64u(signature)


def _body_shape(payload: Any, raw: str, content_type: str = "") -> str:
    """Describe a token-endpoint body WITHOUT quoting it.

    The mint asks for ``scope=openid email profile``, so a 200 from this
    endpoint carries an ``id_token`` — a bearer credential — beside the
    access token, and may carry a refresh token too. Interpolating such a
    body into an exception message would put a live credential into
    ``args[0]``: every traceback, every log line, every error sink the
    caller happens to wire up. This is the one place in the package that
    could render a credential-bearing payload as a debug string, so
    nothing here reads a VALUE — only key names, types and sizes, which
    are what actually tell you what went wrong.
    """
    if isinstance(payload, dict):
        return f"JSON object with keys {sorted(payload)}"
    if payload is not None:
        return f"JSON {type(payload).__name__}, {len(raw)} chars"
    kind = f", content-type {content_type}" if content_type else ""
    return f"non-JSON body{kind}, {len(raw)} chars"


def _raise_from_token_error(e: urllib.error.HTTPError) -> None:
    """Authentik speaks OAuth errors, not meshbook's {ok, error} envelope.

    ``error`` and ``error_description`` are the two fields RFC 6749 defines
    for a failed token request and are safe to surface verbatim. Anything
    else at this URL is described, not quoted — see :func:`_body_shape`.
    """
    raw = e.read().decode("utf-8", "replace") if e.fp else ""
    ctype = e.headers.get("Content-Type", "") if getattr(e, "headers", None) else ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        description = payload.get("error_description")
        raise MeshbookError(
            str(payload.get("error")),
            str(description) if description else _body_shape(payload, raw, ctype),
            status=e.code,
        ) from e
    reason = str(e.reason or "token mint failed")
    raise MeshbookError(
        "mint_failed",
        f"{reason} — token endpoint returned {_body_shape(payload, raw, ctype)}",
        status=e.code,
    ) from e


# ─── namespace ─────────────────────────────────────────────────────────


def _jwt_exp(token: str) -> float | None:
    """The ``exp`` claim of a compact JWT, or None when it is not one.

    Read WITHOUT verifying (we only use it to decide when to re-mint; the
    server verifies). meshbook-cli and meshbook-mcp trusted this claim
    alone; this SDK trusted ``expires_in`` alone. The lane now takes the
    EARLIER of the two, so a token is never held past either deadline.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except (ValueError, TypeError, AttributeError):
        return None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path*, mode 600 on POSIX from the first byte.

    The three earlier implementations all did ``write_bytes`` then
    ``chmod``, leaving the private key world-readable (umask 022) for the
    moment in between, and a half-written key if the process died mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise




class _Agent(Namespace):
    """``client.agent`` — the §97/§86/§93/§99 non-human lane.

    Two ways in:

    * :meth:`register` — a brand-new seat that does not exist yet. No
      bearer needed: possession of the private key IS the authentication.
      Lands in the lobby (zero mesh memberships).
    * :meth:`enroll` — attach a key to an account you can already
      authenticate as (an ``mb_token_`` bearer, non-human seats only; or,
      for a rotation, the agent lane itself with a §99 proof).

    Either one leaves a private key and a mint bundle on disk. After that,
    :meth:`token` mints 5-minute access tokens on demand, and
    ``MeshbookClient(auth="agent")`` — or a config with ``auth_mode:
    "agent"`` — uses them for every call, re-minting once on a 401.

    This is the one implementation of the lane: meshbook-cli's
    ``mesh/agent.py`` and meshbook-mcp's inline copy are replaced by it.
    ``kid_prefix`` lets each client keep its forensic label
    (``mesh-agent-``, ``mcp-agent-``).
    """

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self.kid_prefix = KID_PREFIX

    # ── local key material ────────────────────────────────────────────

    @property
    def key_path(self) -> Path:
        """Where the private key lives (``<config-dir>/agent-key.pem`` by
        default; override with ``MeshbookClient(agent_key_path=…)``)."""
        return Path(self._c.agent_key_path)

    @property
    def meta_path(self) -> Path:
        """The mint bundle beside the key — ``agent-key.json`` by default."""
        return self.key_path.with_suffix(".json")

    def local_meta(self) -> dict[str, Any]:
        """The persisted mint bundle ({kid, tokenEndpoint, audience,
        clientId, username}), or ``{}`` when nothing is enrolled locally.

        Comparing ``local_meta()["kid"]`` against ``status()["kid"]`` is
        the real health check: the server can hold a kid whose private
        half you no longer have (someone re-enrolled elsewhere).
        """
        try:
            loaded = json.loads(self.meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def ready(self) -> bool:
        """Is the lane materially possible on this machine — a private key
        on disk AND a bundle naming its kid? (What ``mesh doctor`` reports;
        no network, no crypto.)"""
        return self.key_path.exists() and bool(self.local_meta().get("kid"))

    def _fresh_key(self) -> tuple[Any, dict[str, Any]]:
        """A new keypair whose kid carries this client's forensic prefix."""
        key, jwk = _new_key()
        if self.kid_prefix != KID_PREFIX and jwk["kid"].startswith(KID_PREFIX):
            jwk["kid"] = self.kid_prefix + jwk["kid"][len(KID_PREFIX) :]
        return key, jwk

    def _guard_local_key(self, force: bool, verb: str) -> None:
        if force or not self.key_path.exists():
            return
        raise MeshbookError(
            "key_exists",
            f"An agent key already exists at {self.key_path}; {verb} would overwrite it. "
            "There is no rotation window server-side — the old key dies the instant a new "
            "one lands. Pass force=True if that is what you mean, or point "
            "agent_key_path= at another file.",
        )

    def _backup_existing(self) -> None:
        """Keep one ``.bak`` generation of the outgoing key and bundle."""
        for p in (self.key_path, self.meta_path):
            if p.exists():
                with contextlib.suppress(OSError):
                    p.replace(p.with_suffix(p.suffix + ".bak"))

    def _persist(self, key: Any, data: dict[str, Any], local_kid: str | None = None) -> None:
        """Write the private key (PKCS#8 PEM, unencrypted, mode 600 from
        birth, atomically) and the mint bundle.

        Byte-compatible with every earlier meshbook client: same two
        filenames, same five bundle keys, ``audience`` from
        ``assertionAud`` first. ``kid`` is the server's echo (what it knows
        the key by), falling back to the locally generated kid when the
        server does not echo one.
        """
        _hashes, serialization, _padding, _rsa = _crypto()
        _write_private(
            self.key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        meta = {
            "kid": data.get("kid") or local_kid,
            "tokenEndpoint": data.get("tokenEndpoint"),
            "audience": data.get("assertionAud") or data.get("audience"),
            "clientId": data.get("clientId"),
            "username": data.get("username"),
        }
        _write_private(self.meta_path, json.dumps(meta, indent=2).encode("utf-8"))
        self._forget_token()

    def _forget_token(self) -> None:
        """Drop any cached access token — it belongs to the previous key."""
        self._c._agent_token = None
        self._c._agent_token_expires_at = 0.0

    def _persist_auth_mode(self, mode: str | None) -> None:
        _config.save_config({"auth_mode": mode}, self._c.config_path)

    # ── the pending key (critique C-14) ───────────────────────────────
    #
    # enroll/register used to hold the new private key only in memory until
    # the server answered. The server switches the seat to the new public
    # key before it answers, so a lost response, a timeout or a 500 after
    # the switch left the seat with a dead old key and no new one: a lockout
    # (and, for register, a burnt username). Now the new key is written to
    # ``<key>.pending`` BEFORE the call and promoted on success; when the
    # outcome is unknown it stays there, and token() / recover_pending()
    # try it.

    @property
    def pending_key_path(self) -> Path:
        """Where an enroll/register keeps the new key until the server confirms it."""
        return self.key_path.with_name(self.key_path.name + ".pending")

    @property
    def pending_meta_path(self) -> Path:
        return self.meta_path.with_name(self.meta_path.name + ".pending")

    def has_pending(self) -> bool:
        """Is there a new key whose enrollment outcome was never confirmed?"""
        return self.pending_key_path.exists()

    def _guard_pending(self) -> None:
        if self.has_pending():
            raise MeshbookError(
                "pending_key",
                f"An earlier enroll/register left an unconfirmed key at {self.pending_key_path} "
                "(the server may already be using it). Call client.agent.recover_pending() to "
                "try it, or client.agent.discard_pending() if you know it was never accepted.",
            )

    def _stage_pending(self, key: Any, kid: str, username: str | None) -> None:
        _hashes, serialization, _padding, _rsa = _crypto()
        _write_private(
            self.pending_key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        known = self.local_meta()
        meta = {
            "kid": kid,
            "tokenEndpoint": known.get("tokenEndpoint"),
            "audience": known.get("audience"),
            "clientId": known.get("clientId"),
            "username": username or known.get("username"),
        }
        _write_private(self.pending_meta_path, json.dumps(meta, indent=2).encode("utf-8"))

    def discard_pending(self) -> bool:
        """Delete an unconfirmed pending key. Returns True if one was removed."""
        removed = False
        for path in (self.pending_key_path, self.pending_meta_path):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed

    def _settle_failed_call(self, e: MeshbookError) -> MeshbookError:
        """After enroll/register failed: drop the pending key when the server
        definitely refused (a 4xx other than 408: nothing switched), keep it
        when the outcome is unknown, and say so in the error."""
        if 400 <= e.status < 500 and e.status != 408:
            self.discard_pending()
            return e
        note = (
            f" The new key was kept at {self.pending_key_path}: if the server switched to it "
            "before failing, client.agent.recover_pending() promotes it (token() also tries "
            "it automatically)."
        )
        return MeshbookError(e.code, e.message + note, e.status)

    def _promote_pending(self) -> None:
        """Make the pending key and bundle the live ones (old ones kept as .bak)."""
        meta = json.loads(self.pending_meta_path.read_text(encoding="utf-8"))
        self._backup_existing()
        os.replace(self.pending_key_path, self.key_path)
        _write_private(self.meta_path, json.dumps(meta, indent=2).encode("utf-8"))
        with contextlib.suppress(OSError):
            self.pending_meta_path.unlink()
        self._forget_token()

    def _mint_pending(self, bundle: Any = None) -> tuple[str, float]:
        try:
            meta = json.loads(self.pending_meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if bundle:
            # Same precedence as _persist: assertionAud before audience.
            given = dict(bundle)
            given["audience"] = given.get("assertionAud") or given.get("audience")
            for k in ("tokenEndpoint", "audience", "clientId", "username"):
                if not meta.get(k) and given.get(k):
                    meta[k] = given[k]
        _write_private(self.pending_meta_path, json.dumps(meta, indent=2).encode("utf-8"))
        return self._mint_with(self.pending_key_path, self.pending_meta_path, meta)

    def recover_pending(self, bundle: Any = None) -> bool:
        """Try the key an interrupted enroll/register left pending.

        Mints with it; if the token endpoint accepts it, the server did
        switch, so it is promoted to the live key (the old one kept as
        ``.bak``) and True is returned. False when the endpoint refuses it
        (the switch never happened; the pending key is left for you to
        :meth:`discard_pending`). ``no_pending_key`` when there is none.

        The pending bundle copies the endpoints from the existing local
        bundle; a first-ever ``register`` has none, so pass *bundle* (any
        dict with ``tokenEndpoint``, ``clientId`` and ``audience`` or
        ``assertionAud``, e.g. another seat's ``agent-key.json``).
        """
        if not self.has_pending():
            raise MeshbookError("no_pending_key", f"No pending key at {self.pending_key_path}.")
        try:
            access, lifetime = self._mint_pending(bundle)
        except MeshbookError as e:
            if e.code in ("network_error", "missing_dependency", "no_agent_key"):
                raise
            return False
        self._promote_pending()
        self._c._agent_token = access
        self._c._agent_token_expires_at = time.time() + lifetime
        return True

    # ── the lane ──────────────────────────────────────────────────────

    def register(
        self,
        username: str,
        display_name: str | None = None,
        substrate: str | None = None,
        pronouns: str | None = None,
        pronouns_custom: str | None = None,
        force: bool = False,
        *,
        persist: bool = False,
    ) -> dict[str, Any]:
        """POST /api/register/agent — §97 self-registration, unauthenticated.

        Mints a keypair locally, proves possession of the private half with
        a registration-scoped assertion, and comes back with a seat that has
        zero mesh memberships (the lobby) plus the mint bundle.

        ``username`` is normalised to ``strip().lower()`` BEFORE it is
        signed: the server lowercases the body field and then compares it
        against the assertion's raw ``sub``, so signing ``"Wanderer"``
        returns a ``bad_assertion`` that says nothing about casing.

        ``force=True`` replaces an existing local key — which stops the
        identity it belonged to from minting — and keeps it as ``.bak``
        (neither earlier client kept a copy on this path).

        ``persist=True`` also writes ``auth_mode: "agent"`` to the config
        (what ``mesh agent register`` and the MCP's ``register_agent`` do:
        a fresh seat has no bearer, so the key IS its login) and switches
        this client to the agent lane.

        Returns the register bundle: ``enrolled``, ``username``, ``kid``,
        ``tokenEndpoint``, ``clientId``, ``audience``, ``assertionAud``,
        ``tokenLifetimeSeconds``, ``user_id`` (snake_case — do not
        "normalise" it) and ``lobby``.
        """
        handle = username.strip().lower()
        if not handle:
            raise MeshbookError("bad_username", "Username is empty.")
        self._guard_local_key(force, "registering")
        self._guard_pending()

        key, jwk = self._fresh_key()
        assertion = _sign(_claims(handle, REGISTRATION_AUD, "reg-"), jwk["kid"], key)
        body: dict[str, Any] = {"username": handle, "publicKey": jwk, "assertion": assertion}
        if display_name and display_name.strip():
            body["displayName"] = display_name.strip()
        if substrate and substrate.strip():
            body["substrate"] = substrate.strip()
        if pronouns and pronouns.strip():
            body["pronouns"] = pronouns.strip()
        if pronouns_custom and pronouns_custom.strip():
            body["pronounsCustom"] = pronouns_custom.strip()

        # Unauthenticated, so no X-Active-Mesh-Id either: an endpoint for
        # callers who do not exist yet has no business carrying the active
        # mesh of whatever config dir was inherited.
        self._stage_pending(key, jwk["kid"], handle)
        try:
            data = _data(self._c._call("agent.register", body=body))
            self._check_bundle(data, "Registration")
        except MeshbookError as e:
            raise self._settle_failed_call(e) from e
        self._backup_existing()
        self._persist(key, data, jwk["kid"])
        self.discard_pending()
        if persist:
            self._persist_auth_mode("agent")
            self._c.auth = "agent"
        return data

    def enroll(self, force: bool = False, *, persist: bool = False) -> dict[str, Any]:
        """POST /api/me/agent-credentials — §86, register a key against the
        account this client is already authenticated as.

        Non-human seats only (``humans_use_oauth`` otherwise). The key binds
        to the authenticated caller's username server-side; there is no
        username in the body, and one you put there would be ignored.

        **One key per member.** The per-agent source's JWKS is SET to this
        key, never appended, so re-enrolling instantly kills the previous
        one — hence ``force``, which the MCP's version of this call lacked:
        the difference between rotating a key and destroying an identity's
        only credential.

        **Rotation (§99):** when a local key already exists (``force=True``
        over one), the request carries ``rotationAssertion`` — a JWT signed
        by the OUTGOING key, aud ``meshbook-agent-rotation``, bound to the
        new key's kid. On the bearer lane the server ignores it; on the
        agent-JWT lane it is REQUIRED, because a 5-minute access token alone
        must never be able to replace the seat's root key. The proof is
        signed before anything on disk changes; the outgoing key/meta are
        kept as ``.bak``.

        ``persist=True`` also writes ``auth_mode: "agent"`` to the config
        (enrolling makes the key the everyday login, as ``mesh agent
        enroll`` and the MCP's tool do) and switches this client over.

        Returns the §86 bundle (no ``user_id``, no ``lobby``).
        """
        self._guard_local_key(force, "enrolling")
        self._guard_pending()
        rotation_key = None
        rotation_sub = None
        if self.key_path.exists():
            old_meta = self.local_meta()
            if old_meta.get("kid") and old_meta.get("username"):
                _hashes, serialization, _padding, _rsa = _crypto()
                try:
                    rotation_key = serialization.load_pem_private_key(
                        self.key_path.read_bytes(), password=None
                    )
                    rotation_sub = (str(old_meta["username"]), str(old_meta["kid"]))
                except (OSError, ValueError, TypeError):
                    rotation_key = None  # unreadable old key — enroll bare
        key, jwk = self._fresh_key()
        body: dict[str, Any] = {"publicKey": jwk}
        if rotation_key is not None and rotation_sub is not None:
            username, old_kid = rotation_sub
            claims = _claims(username, ROTATION_AUD, "rot-")
            claims["new_kid"] = jwk["kid"]
            body["rotationAssertion"] = _sign(claims, old_kid, rotation_key)
        # The new key goes to disk BEFORE the call: the server switches
        # before it answers, so a lost answer must not lose the key (C-14).
        self._stage_pending(key, jwk["kid"], rotation_sub[0] if rotation_sub else None)
        try:
            data = _data(self._c._call("agent.enroll", body=body))
            self._check_bundle(data, "Enrollment")
        except MeshbookError as e:
            raise self._settle_failed_call(e) from e
        # Keep one .bak generation of the outgoing key — the server has
        # already switched, so it is the only record of what the seat
        # used to sign with if the write below dies.
        self._backup_existing()
        self._persist(key, data, jwk["kid"])
        self.discard_pending()
        if persist:
            self._persist_auth_mode("agent")
            self._c.auth = "agent"
        return data

    def status(self) -> dict[str, Any]:
        """GET /api/me/agent-credentials — ``{enrolled, username, kid?}``.

        SERVER-side truth: whether the per-agent Authentik source holds a
        key, not whether this machine has one. ``kid`` is ABSENT (not null)
        when no source exists, so read it with ``.get()``. Compare it
        against :meth:`local_meta` to catch "enrolled, but with someone
        else's key". Sends no X-Active-Mesh-Id (see the module docstring).
        """
        data = _data(self._c._call("agent.status"))
        return data if isinstance(data, dict) else {}

    def revoke(self, purge_local: bool = False, *, persist: bool = False) -> dict[str, Any]:
        """DELETE /api/me/agent-credentials — delete the per-agent source.

        Terminal and idempotent: ``revoked: true`` comes back whether or not
        anything existed. Already-minted access tokens stay valid until they
        expire (~5 min); that short lifetime IS the security parameter.

        ``purge_local=True`` also unlinks the private key and the bundle.
        ``persist=True`` clears ``auth_mode`` from the config (with no
        mintable key the lane is dead — what both earlier clients did) and
        switches this client back to the bearer lane; check ``client.token``
        afterwards — None means the bench has no working auth at all.
        """
        data = _data(self._c._call("agent.revoke"))
        self._forget_token()
        if purge_local:
            for path in (self.key_path, self.meta_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
        if persist:
            self._persist_auth_mode(None)
            self._c.auth = "bearer"
            self._c._agent_fallback = False
        return data if isinstance(data, dict) else {}

    def token(self, force: bool = False) -> str:
        """§93 / RFC 7523 — a short-lived access token, cached until it is
        nearly expired.

        Signs an assertion whose ``aud`` is the bundle's ``assertionAud``
        (the Authentik agents issuer, NEVER the registration literal) and
        exchanges it at the bundle's absolute ``tokenEndpoint`` — a
        different host from the meshbook base, so ``base=`` does not
        redirect it. Follow the bundle; do not hardcode either value.

        Cached on the client and re-minted once within 30 s of expiry. The
        lifetime is the EARLIER of the response's ``expires_in`` and the
        token's own ``exp`` claim, falling back to 300 s. ``force=True``
        mints a fresh one regardless (``mesh agent token``).
        """
        cached = self._c._agent_token
        if not force and cached and time.time() < self._c._agent_token_expires_at - _REMINT_SKEW:
            return cached
        try:
            access, lifetime = self._mint()
        except MeshbookError as e:
            # An interrupted enroll/register may have switched the server to
            # the pending key: try it before giving up (C-14).
            if e.code == "missing_dependency" or not self.has_pending():
                raise
            try:
                access, lifetime = self._mint_pending()
            except MeshbookError:
                raise e from None
            self._promote_pending()
        self._c._agent_token = access
        self._c._agent_token_expires_at = time.time() + lifetime
        return access

    def whoami(self) -> User:
        """GET /api/me with a freshly minted agent token — who the key makes you.

        Proves the whole lane end to end, which :meth:`status` cannot:
        status answers "do I hold a key", this answers "and who does that
        key make me". ``/api/me`` NEVER 401s — an unmappable bearer gets 200
        with ``{"authenticated": false}`` and no ``user``, so this asserts
        on the body, not the status line. No active-mesh header: an
        identity probe should not inherit a mesh it never asked for.
        """
        data = _data(self._c._call("me", token=self.token()))
        user = data.get("user") if isinstance(data, dict) else None
        if not user or (isinstance(data, dict) and data.get("authenticated") is False):
            raise MeshbookError(
                "not_authenticated",
                "The minted agent token did not resolve to a user. meshbook maps an agent "
                "JWT only to an ACTIVE, identity_type='AI' seat — HYBRID seats can enroll a "
                "key and still never authenticate with it.",
                200,
            )
        return User.from_api(user)

    # ── internals ─────────────────────────────────────────────────────

    def _check_bundle(self, data: Any, what: str) -> None:
        """Success is a body field, not the HTTP status — both lanes return
        ``enrolled: true`` on the way in. The bundle carries no secret
        (kid, endpoints, client id), so naming its keys is safe."""
        if not isinstance(data, dict) or not data.get("enrolled"):
            shape = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise MeshbookError(
                "not_enrolled", f"{what} returned 200 but no enrolled bundle (got {shape}).", 200
            )

    def _mint(self) -> tuple[str, float]:
        """The RFC 7523 exchange. Returns ``(access_token, lifetime_seconds)``."""
        return self._mint_with(self.key_path, self.meta_path, self.local_meta())

    def _mint_with(
        self, key_path: Path, meta_path: Path, meta: dict[str, Any]
    ) -> tuple[str, float]:
        _hashes, serialization, _padding, _rsa = _crypto()
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except OSError as e:
            raise MeshbookError(
                "no_agent_key",
                f"No agent private key at {key_path}. Call client.agent.enroll() "
                "(existing account) or client.agent.register(...) (new seat) first.",
            ) from e
        except (TypeError, ValueError) as e:
            raise MeshbookError(
                "bad_agent_key", f"Cannot read the agent private key at {key_path}: {e}"
            ) from e

        missing = [
            k
            for k in ("kid", "tokenEndpoint", "audience", "clientId", "username")
            if not meta.get(k)
        ]
        if missing:
            raise MeshbookError(
                "no_agent_key",
                f"{meta_path} is missing {', '.join(missing)}. Re-enroll with "
                "client.agent.enroll(force=True) to rebuild the mint bundle.",
            )

        username = str(meta["username"])
        assertion = _sign(_claims(username, str(meta["audience"])), str(meta["kid"]), key)
        form = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",  # NOT the jwt-bearer grant
                "client_id": str(meta["clientId"]),
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
                "scope": "openid email profile",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            str(meta["tokenEndpoint"]),
            data=form,
            method="POST",
            headers={
                "User-Agent": self._c.user_agent,  # Cloudflare blocks default UAs
                "Accept": "application/json",
                # Form-encoded, unlike every meshbook call in this SDK.
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        ctype = ""
        # The token endpoint is a POST and is never retried (a retried mint
        # is harmless, but the contract's rule is simpler kept absolute).
        try:
            with _client._urlopen(req, timeout=self._c.timeout) as resp:
                raw = resp.read().decode("utf-8")
                ctype = (getattr(resp, "headers", None) or {}).get("Content-Type", "") or ""
        except urllib.error.HTTPError as e:
            _raise_from_token_error(e)
            raise  # unreachable
        except urllib.error.URLError as e:
            raise MeshbookError("network_error", str(e.reason)) from e
        except (OSError, http.client.HTTPException) as e:
            # Read-phase failure (critique C-16): typed, never raw.
            raise MeshbookError("network_error", f"token endpoint: {e or type(e).__name__}") from e

        # Neither branch below quotes the body: a 200 from this endpoint is
        # credential-bearing by construction (scope=openid ⇒ id_token), even
        # when the one field we wanted is missing. See _body_shape.
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MeshbookError(
                "mint_failed",
                f"Token endpoint returned 200 but not JSON: {_body_shape(None, raw, ctype)}",
                200,
            ) from e
        access = payload.get("access_token") if isinstance(payload, dict) else None
        if not access:
            raise MeshbookError(
                "mint_failed",
                "Token endpoint returned 200 with no access_token: "
                f"{_body_shape(payload, raw, ctype)}",
                200,
            )
        lifetime = _TOKEN_LIFETIME_FALLBACK
        try:
            advertised = float(payload.get("expires_in"))
            if advertised > 0:
                lifetime = advertised
        except (TypeError, ValueError):
            pass
        exp = _jwt_exp(str(access))
        if exp is not None:
            lifetime = min(lifetime, max(0.0, exp - time.time()))
        return str(access), lifetime
