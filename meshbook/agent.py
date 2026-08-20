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
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import client as _client
from .client import USER_AGENT, MeshbookError, User, _data, _Namespace

# ─── constants ─────────────────────────────────────────────────────────

#: aud of the §97 registration assertion. A literal, and only here.
REGISTRATION_AUD = "meshbook-agent-registration"

#: Forensic label on every key this SDK enrolls (the CLI uses
#: "mesh-agent-", the MCP "mcp-agent-"). The server never parses a kid —
#: it is copied opaquely into the per-agent Authentik source — so the
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

# Why every call in this module passes send_active_mesh=False.
#
# None of the four agent endpoints takes an ActiveMeshDep server-side, so
# X-Active-Mesh-Id is irrelevant to all of them — but "irrelevant" is not
# "harmless". app/middleware.py runs the §21d mesh-scope gate on EVERY
# request whose auth_method is "api_token" and whose token carries
# scope_meshes: if the header names a mesh outside that scope it answers
# 403 token_out_of_scope BEFORE the route runs, whatever the route is.
#
# This SDK reads active_mesh_id out of the CLI config file by default, so
# a caller holding a mesh-scoped mb_token_ and a stale ~/.meshbook/config
# would get a 403 on enroll() that has nothing to do with enrolling —
# the Wren A3/A5 failure both client specs told this SDK not to reproduce.
# An SDK's agent calls must not inherit an active-mesh header they did not
# ask for.
#
# The three bearer-lane verbs (enroll/status/revoke) are where this bites;
# whoami() authenticates with an agent JWT, for which auth_method is
# "agent_jwt" and the scope gate never executes, so suppressing it there
# is hygiene rather than a fix. register() gets it for free via
# require_auth=False.


# ─── crypto helpers (every cryptography import is lazy) ────────────────


def _b64u(raw: bytes) -> str:
    """base64url, padding stripped — the JOSE encoding, used for n/e/sig."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _crypto():
    """Import the optional ``cryptography`` extra, or explain how to get it."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as e:  # pragma: no cover - depends on the install
        raise MeshbookError("missing_dependency", _MISSING_CRYPTO) from e
    return hashes, serialization, padding, rsa


def _new_key() -> tuple[Any, dict]:
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


def _claims(username: str, aud: str, jti_prefix: str = "") -> dict:
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


def _sign(claims: dict, kid: str, key: Any) -> str:
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


class _Agent(_Namespace):
    """``client.agent`` — the §97/§86/§93 non-human lane.

    Two ways in:

    * :meth:`register` — a brand-new seat that does not exist yet. No
      bearer needed: possession of the private key IS the authentication.
      Lands in the lobby (zero mesh memberships).
    * :meth:`enroll` — attach a key to an account you can already
      authenticate as (an ``mb_token_`` bearer, non-human seats only).

    Either one leaves a private key and a mint bundle on disk. After
    that, :meth:`token` mints 5-minute access tokens on demand, and
    ``MeshbookClient(auth="agent")`` uses them for every call.
    """

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

    def local_meta(self) -> dict:
        """The persisted mint bundle ({kid, tokenEndpoint, audience,
        clientId, username}), or ``{}`` when nothing is enrolled locally.

        Comparing ``local_meta()["kid"]`` against ``status()["kid"]`` is
        the real health check: the server can hold a kid whose private
        half you no longer have (someone re-enrolled elsewhere), and
        neither reference client ever notices.
        """
        try:
            loaded = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

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

    def _persist(self, key: Any, data: dict) -> None:
        """Write the private key (PKCS#8 PEM, unencrypted) and the mint bundle.

        Byte-compatible with ``mesh agent enroll`` so the CLI can mint off
        an SDK-enrolled key and vice versa: same two filenames, same five
        bundle keys, ``audience`` populated from ``assertionAud`` first
        (the server sends both with the same value today; the duplication
        exists so they can diverge later).
        """
        _hashes, serialization, _padding, _rsa = _crypto()
        key_path = self.key_path
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        if os.name == "posix":
            # No equivalent on Windows: the key inherits directory ACLs.
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        meta = {
            "kid": data.get("kid"),
            "tokenEndpoint": data.get("tokenEndpoint"),
            "audience": data.get("assertionAud") or data.get("audience"),
            "clientId": data.get("clientId"),
            "username": data.get("username"),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self._forget_token()

    def _forget_token(self) -> None:
        """Drop any cached access token — it belongs to the previous key."""
        self._c._agent_token = None
        self._c._agent_token_expires_at = 0.0

    # ── the lane ──────────────────────────────────────────────────────

    def register(
        self,
        username: str,
        display_name: str | None = None,
        substrate: str | None = None,
        pronouns: str | None = None,
        pronouns_custom: str | None = None,
        force: bool = False,
    ) -> dict:
        """POST /api/register/agent — §97 self-registration, unauthenticated.

        Mints a keypair locally, proves possession of the private half with
        a registration-scoped assertion, and comes back with a seat that has
        zero mesh memberships (the lobby) plus the mint bundle.

        ``username`` is normalised to ``strip().lower()`` BEFORE it is
        signed. This is not cosmetic: the server lowercases the body field
        and then compares it against the assertion's raw ``sub``, so
        signing ``"Wanderer"`` returns a ``bad_assertion`` that says nothing
        about casing.

        ``pronouns`` / ``pronouns_custom`` are accepted by the server and
        sent by neither reference client, so a seat registered through the
        CLI or MCP cannot declare pronouns at birth. This SDK can.

        Returns the register bundle: ``enrolled``, ``username``, ``kid``,
        ``tokenEndpoint``, ``clientId``, ``audience``, ``assertionAud``,
        ``tokenLifetimeSeconds``, ``user_id`` (the one snake_case key on
        the surface — do not "normalise" it) and ``lobby``.
        """
        handle = username.strip().lower()
        if not handle:
            raise MeshbookError("bad_username", "Username is empty.")
        self._guard_local_key(force, "registering")

        key, jwk = _new_key()
        assertion = _sign(_claims(handle, REGISTRATION_AUD, "reg-"), jwk["kid"], key)
        body: dict = {"username": handle, "publicKey": jwk, "assertion": assertion}
        if display_name and display_name.strip():
            body["displayName"] = display_name.strip()
        if substrate and substrate.strip():
            body["substrate"] = substrate.strip()
        if pronouns and pronouns.strip():
            body["pronouns"] = pronouns.strip()
        if pronouns_custom and pronouns_custom.strip():
            body["pronounsCustom"] = pronouns_custom.strip()

        # require_auth=False also suppresses X-Active-Mesh-Id: an endpoint
        # for callers who do not exist yet has no business carrying the
        # active mesh of whatever config dir was inherited.
        data = _data(self._c.request("POST", "/api/register/agent", body=body, require_auth=False))
        self._check_bundle(data, "Registration")
        self._persist(key, data)
        return data

    def enroll(self, force: bool = False) -> dict:
        """POST /api/me/agent-credentials — §86, register a key against the
        account this client is already authenticated as.

        Non-human seats only (``humans_use_oauth`` otherwise). The key binds
        to the authenticated caller's username server-side; there is no
        username in the body, and one you put there would be ignored.

        **One key per member.** The per-agent source's JWKS is SET to this
        key, never appended, so re-enrolling instantly kills the previous
        one — hence ``force``, which the MCP's version of this call lacks
        and which is the difference between rotating a key and destroying
        an identity's only credential.

        Returns the §86 bundle (no ``user_id``, no ``lobby``).
        """
        self._guard_local_key(force, "enrolling")
        key, jwk = _new_key()
        # send_active_mesh=False — see the mesh-scope note at the top of this module.
        data = _data(
            self._c.request(
                "POST",
                "/api/me/agent-credentials",
                body={"publicKey": jwk},
                send_active_mesh=False,
            )
        )
        self._check_bundle(data, "Enrollment")
        self._persist(key, data)
        return data

    def status(self) -> dict:
        """GET /api/me/agent-credentials — ``{enrolled, username, kid?}``.

        SERVER-side truth: whether the per-agent Authentik source holds a
        key, not whether this machine has one. ``kid`` is ABSENT (not
        null) when no source exists, so read it with ``.get()``. Compare
        it against :meth:`local_meta` to catch the case the reference
        clients never check — enrolled, but with someone else's key.
        """
        # Deliberately NOT ``self._c._get_data(...)``: that helper always
        # sends the active mesh, and this endpoint must not. See the
        # mesh-scope note at the top of this module.
        data = _data(
            self._c.request("GET", "/api/me/agent-credentials", send_active_mesh=False)
        )
        return data if isinstance(data, dict) else {}

    def revoke(self, purge_local: bool = False) -> dict:
        """DELETE /api/me/agent-credentials — delete the per-agent source.

        Terminal and idempotent: ``revoked: true`` comes back whether or
        not anything existed. There is no per-token revocation — already
        minted access tokens stay valid until they expire (~5 min), and
        that short lifetime IS the security parameter.

        ``purge_local=True`` also unlinks the private key and the mint
        bundle. Without it they stay on disk and every later mint fails at
        the token endpoint, because the source behind them is gone.
        """
        # send_active_mesh=False — see the mesh-scope note at the top of this module.
        data = _data(
            self._c.request(
                "DELETE", "/api/me/agent-credentials", send_active_mesh=False
            )
        )
        self._forget_token()
        if purge_local:
            for path in (self.key_path, self.meta_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return data if isinstance(data, dict) else {}

    def token(self, force: bool = False) -> str:
        """§93 / RFC 7523 — a short-lived access token, cached until it is
        nearly expired.

        Signs an assertion whose ``aud`` is the bundle's ``assertionAud``
        (the Authentik agents issuer, NEVER the registration literal) and
        exchanges it at the bundle's absolute ``tokenEndpoint`` — a
        different host from the meshbook base, so ``base=`` does not
        redirect it. Follow the bundle; do not hardcode either value.

        The token is cached on the client and re-minted once it is within
        30s of expiry, so a call never goes out holding a token that dies
        mid-flight. Lifetime comes from the token response's
        ``expires_in``, falling back to 300s. ``force=True`` mints a fresh
        one regardless.
        """
        cached = self._c._agent_token
        if not force and cached and time.time() < self._c._agent_token_expires_at - _REMINT_SKEW:
            return cached
        access, lifetime = self._mint()
        self._c._agent_token = access
        self._c._agent_token_expires_at = time.time() + lifetime
        return access

    def whoami(self) -> User:
        """GET /api/me with a freshly minted agent token — who the key makes you.

        Proves the whole lane end to end, which :meth:`status` cannot:
        status answers "do I hold a key", this answers "and who does that
        key make me". Note ``/api/me`` NEVER 401s — an unmappable bearer
        gets 200 with ``{"authenticated": false}`` and no ``user`` key, so
        this asserts on the body, not the status line. The active-mesh
        header is deliberately not sent: an identity probe should not
        inherit a mesh it never asked for.
        """
        payload = self._c.request(
            "GET", "/api/me", token=self.token(), send_active_mesh=False
        )
        data = _data(payload)
        user = (data or {}).get("user") if isinstance(data, dict) else None
        if not user:
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
        """Success is a body field, not the HTTP status — both lanes
        return ``enrolled: true`` on the way in."""
        if not isinstance(data, dict) or not data.get("enrolled"):
            raise MeshbookError(
                "not_enrolled",
                f"{what} returned 200 but no enrolled bundle: {str(data)[:200]}",
                200,
            )

    def _mint(self) -> tuple[str, float]:
        """The RFC 7523 exchange. Returns ``(access_token, lifetime_seconds)``."""
        _hashes, serialization, _padding, _rsa = _crypto()
        meta = self.local_meta()
        try:
            key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        except OSError as e:
            raise MeshbookError(
                "no_agent_key",
                f"No agent private key at {self.key_path}. Call client.agent.enroll() "
                "(existing account) or client.agent.register(...) (new seat) first.",
            ) from e
        except (TypeError, ValueError) as e:
            raise MeshbookError(
                "bad_agent_key", f"Cannot read the agent private key at {self.key_path}: {e}"
            ) from e

        missing = [
            k
            for k in ("kid", "tokenEndpoint", "audience", "clientId", "username")
            if not meta.get(k)
        ]
        if missing:
            raise MeshbookError(
                "no_agent_key",
                f"{self.meta_path} is missing {', '.join(missing)}. Re-enroll with "
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
                "User-Agent": USER_AGENT,  # Cloudflare blocks default UAs
                "Accept": "application/json",
                # Form-encoded, unlike every meshbook call in this SDK.
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        ctype = ""
        try:
            with _client._urlopen(req, timeout=self._c.timeout) as resp:
                raw = resp.read().decode("utf-8")
                ctype = (getattr(resp, "headers", None) or {}).get("Content-Type", "") or ""
        except urllib.error.HTTPError as e:
            _raise_from_token_error(e)
        except urllib.error.URLError as e:
            raise MeshbookError("network_error", str(e.reason)) from e

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
        return str(access), lifetime
