"""meshbook.client — the whole SDK in one file.

Extracted from the proven HTTP core of meshbook-cli (mesh/cli.py):
same envelope unwrap, same config file, same bearer-token auth, same
X-Active-Mesh-Id header contract. Differences from the CLI:

* Library, not a tool — nothing prints, nothing calls sys.exit().
  Every failure raises :class:`MeshbookError`.
* Namespaced methods (``client.contacts.list()``) instead of argparse
  subcommands.
* Never writes the config file. ``client.meshes.use()`` only changes
  the in-memory active mesh for this client instance; the CLI remains
  the owner of ~/.meshbook/config.

Return shapes
-------------
Most methods return plain dicts/lists exactly as the API sends them
(camelCase keys — ``displayName``, ``bodyMd``, ``createdAt`` …), after
stripping the canonical ``{ok, data}`` envelope and the paginated
``{items, total}`` wrapper. A handful of stable shapes get cheap frozen
dataclasses (:class:`User`, :class:`Mesh`, :class:`ExportJob`,
:class:`Attachment`); each keeps the full server payload in ``.raw``
so nothing is ever lost in translation.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
USER_AGENT = f"meshbook-sdk/{VERSION}"  # Cloudflare blocks default UAs
DEFAULT_BASE = "https://meshbook.org"


# ─── errors ────────────────────────────────────────────────────────────


class MeshbookError(Exception):
    """Any failure talking to meshbook.org.

    Attributes:
        status:  HTTP status code (0 for network / local errors).
        code:    machine code from the API error envelope
                 (e.g. ``forbidden``, ``not_found``, ``network_error``).
        message: human-readable message.
    """

    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{status}] {code}: {message}")


# ─── dataclasses (cheap typed views; .raw always holds the full dict) ──


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    identity_type: str
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, d: dict) -> User:
        return cls(
            id=d.get("id", ""),
            username=d.get("username", ""),
            display_name=d.get("displayName", ""),
            identity_type=d.get("identityType", ""),
            raw=d,
        )


@dataclass(frozen=True)
class Mesh:
    id: str
    name: str
    mesh_type: str
    member_role: str
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, d: dict) -> Mesh:
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            mesh_type=d.get("meshType") or d.get("type") or "",
            member_role=d.get("memberRole") or "",
            raw=d,
        )


@dataclass(frozen=True)
class ExportJob:
    id: str
    mesh_id: str
    status: str  # pending | running | ready | failed
    byte_size: int | None
    download_url: str | None
    expires_at: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, d: dict) -> ExportJob:
        return cls(
            id=d.get("id", ""),
            mesh_id=d.get("meshId", ""),
            status=d.get("status", ""),
            byte_size=d.get("byteSize"),
            download_url=d.get("downloadUrl"),
            expires_at=d.get("expiresAt"),
            raw=d,
        )


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    mime_type: str
    byte_size: int | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, d: dict) -> Attachment:
        return cls(
            id=d.get("id", ""),
            filename=d.get("filename", ""),
            mime_type=d.get("mimeType", ""),
            byte_size=d.get("byteSize"),
            raw=d,
        )


# ─── envelope helpers (verbatim semantics from meshbook-cli) ───────────


def _data(payload: Any) -> Any:
    """Strip the canonical envelope: {ok, data} → data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _items(payload: Any) -> list:
    """Normalise a list response through the envelope to a plain list.

    Handles all three shapes the API emits: a bare list, {data: [...]},
    and the ok_list paginated shape {data: {items: [...], total}}.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    return data or []


# ─── config-file loading (same file the CLI writes) ────────────────────


def _default_config_path() -> Path:
    """Same resolution order as meshbook-cli: MESHBOOK_CONFIG_DIR env,
    else existing ~/.meshbook, else $XDG_CONFIG_HOME/meshbook, else
    ~/.meshbook."""
    explicit = os.environ.get("MESHBOOK_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser() / "config"
    legacy = Path.home() / ".meshbook"
    if legacy.exists():
        return legacy / "config"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "meshbook" / "config"
    return legacy / "config"


def _load_config(config_path: str | os.PathLike | None) -> dict:
    path = Path(config_path) if config_path else _default_config_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ─── transport seam ────────────────────────────────────────────────────
#
# All HTTP flows through this one function so tests (and exotic runtimes)
# can monkeypatch `meshbook.client._urlopen` and never touch the network.


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


_FILENAME_STAR_RE = re.compile(r"filename\*=UTF-8''([^;]+)")
_FILENAME_RE = re.compile(r'filename="([^"]+)"')


def _filename_from_disposition(disp: str) -> str:
    """Content-Disposition → filename; the RFC 5987 filename*= form wins."""
    m = _FILENAME_STAR_RE.search(disp)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = _FILENAME_RE.search(disp)
    if m:
        return m.group(1)
    return "attachment"


def _raise_from_http_error(e: urllib.error.HTTPError) -> None:
    raw = e.read().decode("utf-8", "replace") if e.fp else "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise MeshbookError("http_error", raw[:200], status=e.code) from e
    err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
    raise MeshbookError(
        err.get("code", "http_error"), err.get("message", raw[:200]), status=e.code
    ) from e


# ─── the client ────────────────────────────────────────────────────────


class MeshbookClient:
    """Synchronous meshbook.org API client.

    Args:
        token: bearer API token (``mb_token_…``, minted in the web UI at
            /v2/#/account/api-tokens). Resolution order:
            explicit arg → ``MESHBOOK_TOKEN`` env var → the CLI config
            file (``~/.meshbook/config`` by default).
        base: API base URL. Defaults to ``https://meshbook.org``; pass
            ``None`` to fall back to ``MESHBOOK_BASE`` env var, then the
            config file's ``base``, then the production URL. IMPORTANT:
            always the apex domain — www.meshbook.org 301s and the
            redirect downgrades POST to GET.
        active_mesh_id: mesh UUID sent as ``X-Active-Mesh-Id`` on every
            request. Falls back to the config file's value, or use
            ``client.meshes.use(...)`` after construction.
        config_path: explicit path to a CLI-format config file (JSON
            with ``token`` / ``base`` / ``active_mesh_id`` keys).
        timeout: per-request timeout in seconds (default 30; downloads 120).
    """

    def __init__(
        self,
        token: str | None = None,
        base: str | None = DEFAULT_BASE,
        active_mesh_id: str | None = None,
        config_path: str | os.PathLike | None = None,
        timeout: float = 30.0,
    ):
        cfg = _load_config(config_path)
        self.token = token or os.environ.get("MESHBOOK_TOKEN") or cfg.get("token")
        resolved_base = base or os.environ.get("MESHBOOK_BASE") or cfg.get("base") or DEFAULT_BASE
        self.base = resolved_base.rstrip("/")
        self.active_mesh_id = active_mesh_id or cfg.get("active_mesh_id")
        self.timeout = timeout

        self.meshes = _Meshes(self)
        self.contacts = _Contacts(self)
        self.leads = _Leads(self)
        self.tasks = _Tasks(self)
        self.chat = _Chat(self)
        self.channels = _Channels(self)
        self.notifications = _Notifications(self)
        self.files = _Files(self)
        self.exports = _Exports(self)

        self._me_cache: dict | None = None

    # ── plumbing ──────────────────────────────────────────────────────

    def _headers(self, *, require_auth: bool = True) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if require_auth:
            if not self.token:
                raise MeshbookError(
                    "not_authenticated",
                    "No API token. Pass token=, set MESHBOOK_TOKEN, or run `mesh login`.",
                )
            headers["Authorization"] = f"Bearer {self.token}"
        if self.active_mesh_id:
            headers["X-Active-Mesh-Id"] = str(self.active_mesh_id)
        return headers

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        require_auth: bool = True,
    ) -> Any:
        """Raw escape hatch: call any /api path, get the parsed JSON
        payload back (envelope NOT stripped — use ``meshbook.client._data``
        / ``_items`` or the namespaced methods for that)."""
        headers = self._headers(require_auth=require_auth)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._url(path, params), data=data, method=method, headers=headers
        )
        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            _raise_from_http_error(e)
        except urllib.error.URLError as e:
            raise MeshbookError("network_error", str(e.reason)) from e
        if not raw:
            return {}
        payload = json.loads(raw)
        # Defensive: a 200 body carrying {ok: false, error: {...}} is
        # still an error under the envelope contract.
        if isinstance(payload, dict) and payload.get("ok") is False:
            err = payload.get("error") or {}
            raise MeshbookError(
                err.get("code", "api_error"), err.get("message", "API reported ok=false"), 200
            )
        return payload

    def _get_items(self, path: str, params: dict | None = None) -> list:
        return _items(self.request("GET", path, params=params))

    def _get_data(self, path: str, params: dict | None = None) -> Any:
        return _data(self.request("GET", path, params=params))

    def download_raw(self, path: str) -> tuple[bytes, str]:
        """Raw-bytes GET (downloads are not JSON). Returns
        ``(bytes, server_filename)`` where the filename comes from
        Content-Disposition when present."""
        req = urllib.request.Request(
            self._url(path), method="GET", headers=self._headers()
        )
        try:
            with _urlopen(req, timeout=max(self.timeout, 120.0)) as resp:
                raw = resp.read()
                disp = resp.headers.get("Content-Disposition", "") or ""
        except urllib.error.HTTPError as e:
            _raise_from_http_error(e)
        except urllib.error.URLError as e:
            raise MeshbookError("network_error", str(e.reason)) from e
        return raw, _filename_from_disposition(disp)

    def _download_to(self, path: str, out_path: str | os.PathLike | None) -> Path:
        raw, server_name = self.download_raw(path)
        out = Path(out_path) if out_path else Path(server_name)
        if out.is_dir():
            out = out / server_name
        out.write_bytes(raw)
        return out

    # ── identity ──────────────────────────────────────────────────────

    def me(self) -> dict:
        """GET /api/me — full payload dict (user, activeMeshId, …).
        NOTE: /api/me is permissive and answers 200 with
        ``{authenticated: false}`` for a bad token; this method converts
        that into a MeshbookError so callers never see a half-auth state."""
        me = self._get_data("/api/me")
        if isinstance(me, dict) and me.get("authenticated") is False:
            raise MeshbookError(
                "not_authenticated",
                "/api/me reports authenticated=false — token invalid or revoked.",
                401,
            )
        return me if isinstance(me, dict) else {}

    def whoami(self) -> User:
        """The signed-in user as a typed :class:`User`."""
        me = self.me()
        user = me.get("user") or {}
        if not user:
            raise MeshbookError("no_user", "Authenticated but /api/me returned no user.", 200)
        return User.from_api(user)

    def _self_user_id(self) -> str:
        if self._me_cache is None:
            self._me_cache = self.me()
        return ((self._me_cache or {}).get("user") or {}).get("id") or ""

    def _require_mesh(self) -> str:
        if not self.active_mesh_id:
            raise MeshbookError(
                "no_active_mesh",
                "No active mesh. Pass active_mesh_id= or call client.meshes.use(...).",
            )
        return str(self.active_mesh_id)


# ─── namespaces ────────────────────────────────────────────────────────


class _Namespace:
    def __init__(self, client: MeshbookClient):
        self._c = client


class _Meshes(_Namespace):
    def list_mine(self) -> list[Mesh]:
        """GET /api/meshes — every mesh you're a member of."""
        return [Mesh.from_api(m) for m in self._c._get_items("/api/meshes")]

    def use(self, mesh: str) -> Mesh:
        """Set the client's active mesh by UUID or name (case-insensitive).
        In-memory only — never writes the CLI config file."""
        target = mesh.strip()
        found: Mesh | None = None
        try:
            _uuid.UUID(target)
            is_uuid = True
        except ValueError:
            is_uuid = False
        for m in self.list_mine():
            if (is_uuid and m.id == target) or m.name == target:
                found = m
                break
        if found is None:
            low = target.lower()
            for m in self.list_mine():
                if m.name.lower() == low:
                    found = m
                    break
        if found is None:
            raise MeshbookError("mesh_not_found", f"No mesh matching {mesh!r}.", 404)
        self._c.active_mesh_id = found.id
        return found


class _Contacts(_Namespace):
    def list(self, q: str | None = None, limit: int = 50) -> list[dict]:
        """GET /api/contacts — contact dicts (displayName, primaryEmail, …)."""
        return self._c._get_items("/api/contacts", {"limit": limit, "search": q})

    def create(
        self,
        first_name: str,
        last_name: str | None = None,
        email: str | None = None,
        company: str | None = None,
    ) -> dict:
        """POST /api/contacts. `company` is free text, resolved
        server-side (§22c) — check `primaryCompanyId` on the result to
        see whether it matched."""
        body: dict = {"firstName": first_name, "lastName": last_name}
        if email:
            body["primaryEmail"] = email
        if company:
            body["company"] = company
        return _data(self._c.request("POST", "/api/contacts", body=body))


class _Leads(_Namespace):
    def list(
        self,
        limit: int = 50,
        pipeline_id: str | None = None,
        stage_id: str | None = None,
        company_id: str | None = None,
    ) -> list[dict]:
        """GET /api/leads — lead dicts (title, valueAmount, stageName, …)."""
        params = {
            "limit": limit,
            "pipelineId": pipeline_id,
            "stageId": stage_id,
            "companyId": company_id,
        }
        return self._c._get_items("/api/leads", params)

    def create(
        self,
        title: str,
        pipeline_id: str,
        stage_id: str,
        value: float | None = None,
        description: str | None = None,
    ) -> dict:
        body: dict = {"title": title, "pipelineId": pipeline_id, "stageId": stage_id}
        if value is not None:
            body["valueAmount"] = value
        if description:
            body["description"] = description
        return _data(self._c.request("POST", "/api/leads", body=body))

    def move_stage(self, lead_id: str, stage_id: str) -> dict:
        """POST /api/leads/{id}/move-stage."""
        return _data(
            self._c.request(
                "POST", f"/api/leads/{lead_id}/move-stage", body={"stageId": stage_id}
            )
        )


class _Tasks(_Namespace):
    def list(
        self,
        limit: int = 50,
        project_id: str | None = None,
        assignee_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """GET /api/tasks. Status values: NotStarted / InProgress /
        Blocked / Review / Done / Cancelled."""
        params = {
            "limit": limit,
            "projectId": project_id,
            "assigneeId": assignee_id,
            "status": status,
        }
        return self._c._get_items("/api/tasks", params)

    def list_mine(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Tasks assigned to the signed-in user (resolves own id via
        /api/me, cached for the client's lifetime)."""
        return self.list(limit=limit, assignee_id=self._c._self_user_id(), status=status)

    def create(
        self,
        project_id: str,
        title: str,
        priority: str | None = None,
        description: str | None = None,
    ) -> dict:
        body: dict = {"projectId": project_id, "title": title}
        if priority:
            body["priority"] = priority
        if description:
            body["description"] = description
        return _data(self._c.request("POST", "/api/tasks", body=body))

    def done(self, task_id: str, status: str = "Done") -> dict:
        """PATCH /api/tasks/{id} → terminal status (default Done;
        pass e.g. status='Cancelled' for another terminal)."""
        return _data(
            self._c.request("PATCH", f"/api/tasks/{task_id}", body={"status": status})
        )


class _Chat(_Namespace):
    """Entity chat on the ACTIVE MESH (the mesh-wide room). For named
    channels and DMs use ``client.channels``."""

    def post(self, message: str, reply_to: str | None = None) -> dict:
        """POST /api/entities/mesh/{active}/chat. `reply_to` threads the
        message under an existing message's UUID (parentMessageId)."""
        mesh_id = self._c._require_mesh()
        body: dict = {"bodyMd": message}
        if reply_to:
            body["parentMessageId"] = reply_to
        return _data(
            self._c.request("POST", f"/api/entities/mesh/{mesh_id}/chat", body=body)
        )

    def list(self, limit: int = 20) -> list[dict]:
        """GET /api/entities/mesh/{active}/chat — newest first."""
        mesh_id = self._c._require_mesh()
        return self._c._get_items(f"/api/entities/mesh/{mesh_id}/chat", {"limit": limit})

    def attach(
        self,
        message_id: str,
        file_path: str | os.PathLike,
        filename: str | None = None,
        mime: str | None = None,
    ) -> Attachment:
        """Attach a local file to an existing chat message via the
        §26d-json endpoint (base64 in, no multipart)."""
        path = Path(file_path)
        raw = path.read_bytes()
        if not raw:
            raise MeshbookError("empty_file", f"File is empty: {path}")
        body = {
            "filename": filename or path.name,
            "mimeType": mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            "base64Bytes": base64.b64encode(raw).decode("ascii"),
        }
        data = _data(
            self._c.request(
                "POST", f"/api/chat-messages/{message_id}/attachments/json", body=body
            )
        )
        return Attachment.from_api(data or {})

    def download(
        self, attachment_id: str, out_path: str | os.PathLike | None = None
    ) -> Path:
        """Download a chat attachment. Returns the written Path
        (server-supplied filename in cwd when out_path is omitted;
        out_path may be a directory)."""
        return self._c._download_to(
            f"/api/chat-attachments/{attachment_id}/download", out_path
        )

    def react(self, message_id: str, emoji: str) -> dict:
        """POST /api/chat-messages/{id}/reactions — works for entity,
        channel, and DM messages alike."""
        return _data(
            self._c.request(
                "POST", f"/api/chat-messages/{message_id}/reactions", body={"emoji": emoji}
            )
        )


class _Channels(_Namespace):
    def list(self) -> list[dict]:
        """GET /api/meshes/{active}/channels — channel dicts
        (name, channelType, unreadCount, …)."""
        mesh_id = self._c._require_mesh()
        return self._c._get_items(f"/api/meshes/{mesh_id}/channels")

    def _resolve(self, channel: str) -> dict:
        """Channel ref → row. Accepts a raw UUID or a name (leading '#'
        stripped, case-insensitive) within the active mesh."""
        target = channel.lstrip("#").strip()
        if not target:
            raise MeshbookError("bad_channel", "Empty channel reference.")
        try:
            _uuid.UUID(target)
            detail = self._c._get_data(f"/api/channels/{target}")
            if detail:
                return detail
            raise MeshbookError("channel_not_found", f"No channel {channel!r}.", 404)
        except ValueError:
            pass
        low = target.lower()
        for ch in self.list():
            if (ch.get("name") or "").lower() == low:
                return ch
        raise MeshbookError(
            "channel_not_found", f"No channel matching {channel!r} in active mesh.", 404
        )

    def read(self, channel: str, limit: int = 20) -> list[dict]:
        """GET /api/channels/{id}/messages — newest first."""
        ch = self._resolve(channel)
        return self._c._get_items(f"/api/channels/{ch['id']}/messages", {"limit": limit})

    def post(self, channel: str, message: str, reply_to: str | None = None) -> dict:
        """POST /api/channels/{id}/messages. Broadcast channels are
        admin-post-only server-side (403 → MeshbookError)."""
        ch = self._resolve(channel)
        body: dict = {"bodyMd": message}
        if reply_to:
            body["parentMessageId"] = reply_to
        return _data(
            self._c.request("POST", f"/api/channels/{ch['id']}/messages", body=body)
        )


class _Notifications(_Namespace):
    def list(self) -> list[dict]:
        """GET /api/notifications — notification dicts (kind, summary,
        readAt, createdAt, …)."""
        return self._c._get_items("/api/notifications")


class _Files(_Namespace):
    """Entity attachments (§78) — files on any entity:
    company | contact | lead | project | task | portfolio |
    calendar_event | mesh."""

    def attach(
        self,
        entity_type: str,
        entity_id: str,
        file_path: str | os.PathLike,
        filename: str | None = None,
        mime: str | None = None,
    ) -> Attachment:
        """POST /api/entities/{type}/{id}/attachments/json (base64)."""
        path = Path(file_path)
        raw = path.read_bytes()
        if not raw:
            raise MeshbookError("empty_file", f"File is empty: {path}")
        body = {
            "filename": filename or path.name,
            "mimeType": mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            "base64Bytes": base64.b64encode(raw).decode("ascii"),
        }
        data = _data(
            self._c.request(
                "POST", f"/api/entities/{entity_type}/{entity_id}/attachments/json", body=body
            )
        )
        return Attachment.from_api(data or {})

    def list(self, entity_type: str, entity_id: str) -> list[dict]:
        """GET /api/entities/{type}/{id}/attachments — dicts with
        kind='file' (filename/byteSize/mimeType) or kind='link'
        (externalUrl)."""
        return self._c._get_items(f"/api/entities/{entity_type}/{entity_id}/attachments")

    def download(
        self, attachment_id: str, out_path: str | os.PathLike | None = None
    ) -> Path:
        """Download an entity attachment; returns the written Path."""
        return self._c._download_to(
            f"/api/entity-attachments/{attachment_id}/download", out_path
        )

    def delete(self, attachment_id: str) -> dict:
        """DELETE /api/entity-attachments/{id} (uploader or mesh admin)."""
        return _data(
            self._c.request("DELETE", f"/api/entity-attachments/{attachment_id}")
        )


class _Exports(_Namespace):
    """Mesh data exports (§58). Admin/account-manager only; exports
    expire after 24 h server-side."""

    def start(self, mesh_id: str) -> ExportJob:
        """POST /api/meshes/{id}/export — start (or return the in-flight)
        export job. The server requires mesh_id == your active mesh;
        this method sends the matching X-Active-Mesh-Id automatically."""
        prev = self._c.active_mesh_id
        self._c.active_mesh_id = mesh_id
        try:
            data = _data(self._c.request("POST", f"/api/meshes/{mesh_id}/export"))
        finally:
            self._c.active_mesh_id = prev
        return ExportJob.from_api(data or {})

    def list(self, mesh_id: str) -> list[ExportJob]:
        """GET /api/meshes/{id}/exports — recent jobs, newest first.
        Poll until status == 'ready', then download."""
        items = self._c._get_items(f"/api/meshes/{mesh_id}/exports")
        return [ExportJob.from_api(d) for d in items]

    def download(self, export_id: str, out_path: str | os.PathLike) -> Path:
        """GET /api/mesh-exports/{id}/download → zip written to out_path
        (a directory keeps the server filename). 409 while not ready,
        410 once expired — both raise MeshbookError with that status."""
        return self._c._download_to(
            f"/api/mesh-exports/{export_id}/download", out_path
        )
