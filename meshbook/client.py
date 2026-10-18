"""meshbook.client — the HTTP core and :class:`MeshbookClient`.

Since 0.4.0 this SDK is the single Python core for meshbook: meshbook-cli
and meshbook-mcp call it instead of carrying their own copies of the
config code, the HTTP layer, envelope parsing, error mapping, the agent
lane and name resolution.

* Library, not a tool: nothing prints, nothing calls ``sys.exit()``. Every
  failure raises :class:`MeshbookError`.
* Every namespace method goes through ``_call(<registry key>)`` — see
  :mod:`meshbook._endpoints`, which is what the contract check reads.
* The config file is owned by :mod:`meshbook.config`; the client only
  writes it when asked to (``meshes.use(..., persist=True)`` and the agent
  lane's ``persist=True``).

Return shapes
-------------
Single objects are plain dicts exactly as the API sends them (camelCase
keys — ``displayName``, ``bodyMd``, ``createdAt`` …), with the ``{ok,
data}`` envelope stripped. Lists come back as :class:`ItemList` — a real
``list`` that also carries the server's ``total`` and the ``limit`` /
``offset`` of the call. A handful of stable shapes get frozen dataclasses
(:class:`User`, :class:`Mesh`, :class:`ExportJob`, :class:`Attachment`);
each keeps the full payload in ``.raw``.
"""
from __future__ import annotations

import email.utils
import http.client
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from . import config as _config
from ._endpoints import ENDPOINTS, Endpoint
from .errors import MeshbookError
from .models import (
    Attachment,
    DownloadedFile,
    ExportJob,
    ItemList,
    Mesh,
    SearchResults,
    User,
)

# Read from the INSTALLED package metadata, never hardcoded. A literal here
# goes stale the moment pyproject is bumped, and the value rides on every
# request's User-Agent -- so the server's logs would attribute traffic to a
# version that was never shipped.
#
# This is the THIRD instance of the same defect in this family, which is why
# it is worth a comment rather than a one-liner: meshbook-cli shipped 0.8.0
# self-reporting 0.6.0 (fixed as DEV-DEBT §92), meshbook-mcp shipped 0.5.0
# self-reporting 0.4.0 (found by Wren 2026-08-20), and this constant read
# 0.1.0 while pyproject said 0.2.0 -- caught by a pre-release gate about
# ninety seconds before publishing. The guarding test crosses to
# pyproject.toml, which is the only place the shipped truth lives.
try:
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version("meshbook-sdk")
except Exception:  # uninstalled dev checkout only
    VERSION = "0.0.0+uninstalled"
USER_AGENT = f"meshbook-sdk/{VERSION}"  # Cloudflare blocks default UAs
DEFAULT_BASE = "https://meshbook.org"

#: Methods the client may retry on 429/503. Never POST or PATCH: a retried
#: write that the server had in fact applied would apply twice.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
RETRYABLE_STATUSES = frozenset({429, 503})
#: Retry-After is honoured up to this many seconds, never longer.
MAX_RETRY_AFTER = 30.0

log = logging.getLogger("meshbook")

T = TypeVar("T")

__all__ = [
    "DEFAULT_BASE",
    "USER_AGENT",
    "VERSION",
    "Attachment",
    "Client",
    "DownloadedFile",
    "ExportJob",
    "ItemList",
    "Mesh",
    "MeshbookClient",
    "MeshbookError",
    "SearchResults",
    "User",
    "safe_filename",
]


# ─── envelope helpers ──────────────────────────────────────────────────


def _data(payload: Any) -> Any:
    """Strip the canonical envelope: {ok, data} → data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _item_list(
    payload: Any,
    *,
    limit: int | None = None,
    offset: int | None = None,
    cls: type[ItemList[Any]] = ItemList,
) -> ItemList[Any]:
    """Any list response → :class:`ItemList`, keeping the envelope.

    Handles every shape the API emits: a bare list, ``{data: [...]}``, and
    the ``ok_list`` shape ``{data: {items: [...], total, page, pageSize,
    …}}``. ``total`` is the server's (``None`` for a bare list).

    If the server ignored the pagination this call asked for — it sent
    back more than ``limit`` rows — the page is cut client-side
    (``[offset:offset+limit]``) and ``total`` is the full count it sent, so
    ``limit=`` means the same thing on every list method.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    total: int | None = None
    meta: dict[str, Any] = {}
    if isinstance(data, dict):
        items = data.get("items")
        total = _as_int(data.get("total"))
        meta = {k: v for k, v in data.items() if k not in ("items", "total")}
        srv_limit = _as_int(meta.get("limit"))
        srv_offset = _as_int(meta.get("offset"))
        if srv_limit is not None:
            limit = srv_limit
        if srv_offset is not None:
            offset = srv_offset
    else:
        items = data
    rows = list(items) if isinstance(items, list) else []
    exact: bool | None = None  # None: ItemList decides from the server's meta
    if limit is not None and len(rows) > limit:
        start = offset or 0
        if total is None or total == len(rows):
            total = len(rows)
            exact = True  # the server sent every row: the count is the count
        rows = rows[start : start + limit]
        meta["client_paginated"] = True
    return cls(rows, total=total, limit=limit, offset=offset, meta=meta, total_exact=exact)


def _items(payload: Any) -> ItemList[Any]:
    """Normalise a list response through the envelope (kept for 0.3 callers)."""
    return _item_list(payload)


def _page(limit: int | None, offset: int | None) -> dict[str, Any]:
    """The pagination query params, validated."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise MeshbookError("bad_argument", f"limit must be a positive int, got {limit!r}.")
    if offset is not None and (
        not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
    ):
        raise MeshbookError("bad_argument", f"offset must be an int >= 0, got {offset!r}.")
    return {"limit": limit, "offset": offset}


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


# ─── transport seams ───────────────────────────────────────────────────
#
# All HTTP flows through `_urlopen` so tests (and exotic runtimes) can
# monkeypatch `meshbook.client._urlopen` and never touch the network; the
# agent lane's token mint reaches it the same way. Back-off sleeps go
# through `_sleep` so tests can record them instead of waiting.


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"https": 443, "http": 80}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it is safe to.

    CPython's stock handler copies every header (Authorization and
    X-Active-Mesh-Id included) to ANY Location, and turns a redirected POST
    into a bodiless GET, so ``base="https://www.meshbook.org"`` made
    ``tasks.create`` return a list and create nothing. Here a redirect is
    followed only for GET/HEAD to the SAME origin (scheme, host, port);
    anything else raises ``redirect_refused`` naming the Location.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        method = req.get_method()
        target = urllib.parse.urljoin(req.full_url, newurl)
        if method not in ("GET", "HEAD") or _origin(target) != _origin(req.full_url):
            try:
                fp.close()
            except Exception:
                pass
            path = urllib.parse.urlsplit(req.full_url).path
            raise MeshbookError(
                "redirect_refused",
                f"{method} {path} was redirected ({code}) to {target}; not followed (a "
                "redirect may only stay on the same origin, and only for GET/HEAD). "
                "Check the base URL: meshbook is served from the apex https://meshbook.org.",
                code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_RedirectGuard)


def _urlopen(req: urllib.request.Request, timeout: float) -> Any:
    return _opener.open(req, timeout=timeout)


#: Hosts a plain-http base may name (local development only).
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _check_base(base: str) -> str:
    """The base URL, stripped of a trailing slash, or ``insecure_base``.

    https is required: the bearer rides on every request. Plain http is
    allowed only for a loopback host (``localhost``, ``127.0.0.1``,
    ``::1``, ``*.localhost``), i.e. a dev server on this machine.
    """
    clean = base.strip().rstrip("/")
    parts = urllib.parse.urlsplit(clean)
    host = (parts.hostname or "").lower()
    if not host or parts.scheme.lower() not in ("https", "http"):
        raise MeshbookError("insecure_base", f"Base URL {base!r} is not an http(s) URL.")
    if parts.scheme.lower() == "http" and not (host in _LOOPBACK or host.endswith(".localhost")):
        raise MeshbookError(
            "insecure_base",
            f"Base URL {base!r} is plain http: the API token would cross the network "
            "unencrypted. Use https (plain http is allowed only for localhost).",
        )
    return clean


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


#: Optional observer called with (method, url) for every request the client
#: sends. The test-suite uses it to assert that every request matches an
#: entry in the endpoint registry. None in normal use.
_on_request: Callable[[str, str], None] | None = None


_DISP_EXT = re.compile(r"filename\*\s*=\s*([A-Za-z0-9!#$%&+^_`{}~.-]+)'[^']*'([^;\s]+)", re.I)
_DISP_QUOTED = re.compile(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.I)
_DISP_TOKEN = re.compile(r"filename\s*=\s*([^;\s\"]+)", re.I)


def _filename_from_disposition(disp: str) -> str:
    """Content-Disposition → the server's filename, UNSANITISED.

    The RFC 5987 ``filename*=charset'lang'value`` form wins (charset and
    parameter names case-insensitive), then a quoted ``filename="…"``, then
    a bare token ``filename=…``. Returns ``"attachment"`` when none is
    present. The result is attacker-influenced (an uploader chose it): pass
    it through :func:`safe_filename` before it goes anywhere near a path.
    """
    m = _DISP_EXT.search(disp or "")
    if m:
        charset = m.group(1).lower()
        try:
            return urllib.parse.unquote(m.group(2), encoding=charset, errors="replace")
        except LookupError:  # unknown charset label
            return urllib.parse.unquote(m.group(2), errors="replace")
    m = _DISP_QUOTED.search(disp or "")
    if m:
        return re.sub(r"\\(.)", r"\1", m.group(1))
    m = _DISP_TOKEN.search(disp or "")
    if m:
        return m.group(1)
    return "attachment"


# ─── safe download names ───────────────────────────────────────────────

#: Device names Windows reserves in every directory, with or without an
#: extension (``CON``, ``con.txt`` and ``NUL.tar.gz`` all name the device).
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in "123456789\u00b9\u00b2\u00b3"}
)
#: Characters that are a separator, a drive/stream marker or invalid on
#: Windows. Replaced on EVERY OS, so a name sanitises the same everywhere.
_UNSAFE_CHARS = frozenset('<>:"/\\|?*')
#: Longest name produced (bytes on most filesystems cap at 255).
MAX_FILENAME = 200


def safe_filename(name: str | None, fallback: str = "attachment") -> str:
    """Reduce an untrusted filename (e.g. a server's Content-Disposition) to
    a single safe path component. Pure string work: the same answer on
    Windows and POSIX.

    * Only the last component survives, splitting on BOTH ``/`` and ``\\``
      (``../../x``, ``..\\..\\x``, ``C:\\x`` and ``/etc/passwd`` all lose
      their directories).
    * ``< > : " | ? *``, control characters (NUL included) and Unicode
      format characters (the right-to-left override that disguises an
      extension) become ``_``; ``C:x`` becomes ``C_x``.
    * Leading and trailing dots and spaces are stripped (Windows drops the
      trailing ones itself; leading ones hide a file or spell ``..``).
    * A Windows device name (``CON``, ``nul.txt``, ``COM1`` …) gets a ``_``
      prefix.
    * Empty, ``.`` or ``..`` after all that → *fallback* (itself sanitised).
    * At most :data:`MAX_FILENAME` characters, keeping a short extension.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = re.split(r"[\\/]", text)[-1]
    text = "".join(
        "_"
        if ch in _UNSAFE_CHARS or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")
        else ch
        for ch in text
    )
    text = text.strip(" .")
    if not text:
        if fallback == "attachment":
            return "attachment"
        return safe_filename(fallback)
    stem = text.split(".", 1)[0].rstrip(" ")
    if stem.upper() in _WINDOWS_RESERVED:
        text = "_" + text
    if len(text) > MAX_FILENAME:
        root, dot, ext = text.rpartition(".")
        if dot and root and 0 < len(ext) <= 16:
            text = root[: MAX_FILENAME - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            text = text[:MAX_FILENAME].rstrip(" .")
    return text or "attachment"


def _is_dir_hint(out_path: str | os.PathLike[str]) -> bool:
    """True for an out_path that names a directory: an existing one, or a
    string ending in a path separator (``"downloads/"``)."""
    raw = os.fspath(out_path)
    seps = tuple(s for s in (os.sep, os.altsep) if s)
    return Path(raw).expanduser().is_dir() or (isinstance(raw, str) and raw.endswith(seps))


def _download_target(
    out_path: str | os.PathLike[str] | None, server_name: str | None
) -> tuple[Path, Path | None]:
    """(target file, containing dir or None) for a download.

    With no out_path, or an out_path that is a directory, the file is named
    by the server, so the name is sanitised and the resolved target must
    sit DIRECTLY inside that directory (a symlink planted under the safe
    name cannot redirect the write elsewhere): returns the directory as the
    second element. An explicit file path is the caller's own choice and is
    used as given (second element None).
    """
    if out_path is not None and not _is_dir_hint(out_path):
        return Path(out_path).expanduser(), None
    directory = Path(out_path).expanduser() if out_path is not None else Path.cwd()
    return directory / safe_filename(server_name), directory


def _assert_contained(target: Path, directory: Path) -> None:
    root = directory.resolve()
    resolved = target.resolve()
    if resolved.parent != root:
        raise MeshbookError(
            "unsafe_path",
            f"Refusing to write {target.name!r}: it resolves outside {root}.",
        )


def _refuse_existing(target: Path) -> None:
    raise MeshbookError(
        "file_exists",
        f"{target} already exists; not overwritten.",
    )


def _atomic_write(target: Path, content: bytes, *, overwrite: bool) -> None:
    """Write *content* to *target* via a temp file in the same directory.

    ``overwrite=True`` → ``os.replace`` (atomic; replaces a symlink itself,
    never writes through it). ``overwrite=False`` → a hard link, which fails
    if anything (a file, a dangling symlink) appeared at *target* in the
    meantime: an atomic no-clobber. Filesystems without hard links fall back
    to check-then-replace. The temp file is always removed. OSErrors become
    ``MeshbookError("write_failed")``.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".meshbook-", suffix=".part", dir=target.parent)
    except OSError as e:
        raise MeshbookError("write_failed", f"Cannot write into {target.parent}: {e}") from e
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp, target)
            return
        try:
            os.link(tmp, target)
        except FileExistsError:
            _refuse_existing(target)
        except OSError:
            # No hard links here (FAT, some network shares): best effort.
            if os.path.lexists(target):
                _refuse_existing(target)
            os.replace(tmp, target)
    except OSError as e:
        raise MeshbookError("write_failed", f"Cannot write {target}: {e}") from e
    finally:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _disposition(headers: Any) -> str:
    """The Content-Disposition header of a raw response, or ``""``."""
    try:
        return str(headers.get("Content-Disposition", "") or "")
    except Exception:
        return ""


def _err_fields(payload: Any, raw: str) -> tuple[str, str]:
    """(code, message) out of any of the three error shapes the API emits.

    Ours is ``{"error": {code, message}}``. Anything raised as a FastAPI
    HTTPException skips the envelope entirely and arrives as
    ``{"detail": {code, message}}`` or ``{"detail": "string"}`` — which is
    every 401, because ``require_user`` raises bare. Reading only ``error``
    reports those as ``http_error: {"detail": …}``, leaking an
    implementation detail to the caller (the MCP did exactly that).
    """
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err:
            return str(err.get("code") or "http_error"), str(err.get("message") or raw[:200])
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return (
                str(detail.get("code") or "http_error"),
                str(detail.get("message") or raw[:200]),
            )
        if isinstance(detail, str) and detail:
            return "http_error", detail
        if isinstance(detail, list) and detail:
            # FastAPI's own 422 shape: [{loc, msg, type}, ...]
            first = detail[0] if isinstance(detail[0], dict) else {}
            loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
            msg = str(first.get("msg") or "validation failed")
            return "validation_error", f"{loc}: {msg}" if loc else msg
    return "http_error", raw[:200]


def _network_message(method: str, path: str, reason: Any, maybe_sent: bool) -> str:
    """A network_error message; says so when a write's outcome is unknown."""
    text = f"{method} {path}: {reason or type(reason).__name__}"
    if maybe_sent and method not in ("GET", "HEAD"):
        text += (
            " The request may have reached the server, so this write may or may not have"
            " been applied: check before retrying."
        )
    return text


def _network_hint(method: str, maybe_sent: bool) -> str:
    return "check_before_retry" if maybe_sent and method not in ("GET", "HEAD") else "retry_later"


def _raise_from_http_error(e: urllib.error.HTTPError) -> None:
    try:
        raw = e.read().decode("utf-8", "replace") if e.fp else "{}"
    except (OSError, http.client.HTTPException):
        raw = ""  # the error body itself was cut off; the status still stands
    finally:
        _close_quietly(e)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise MeshbookError("http_error", raw[:200], status=e.code) from e
    code, message = _err_fields(payload, raw)
    raise MeshbookError(code, message, status=e.code) from e


def _retry_after(headers: Any) -> float | None:
    """Parse Retry-After (delta-seconds or an HTTP date) → seconds, capped.

    None when the header is absent or unparseable.
    """
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            import datetime as _dt

            when = when.replace(tzinfo=_dt.timezone.utc)
        seconds = when.timestamp() - time.time()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def _backoff(attempt: int) -> float:
    """Delay before retry *attempt* (1-based) when no Retry-After was sent."""
    return min(0.5 * (2 ** (attempt - 1)), MAX_RETRY_AFTER)


# ─── the client ────────────────────────────────────────────────────────


class MeshbookClient:
    """Synchronous meshbook.org API client (also importable as ``meshbook.Client``).

    Args:
        token: bearer API token (``mb_token_…``, minted in the web UI at
            /v2/#/account/api-tokens). Resolution: explicit arg →
            ``MESHBOOK_TOKEN`` → the config file's ``token``.
        base: API base URL. Resolution: explicit arg → ``MESHBOOK_BASE`` →
            the config file's ``base`` → ``https://meshbook.org``.
            IMPORTANT: always the apex domain — www.meshbook.org 301s and
            the redirect downgrades POST to GET.
        mesh: the mesh this client works in, by UUID or NAME. Resolution:
            per-call ``mesh=`` (on every mesh-scoped method) → this →
            ``MESHBOOK_MESH`` → the config file's ``active_mesh_id``. A
            name is resolved case-insensitively over your meshes (one
            ``GET /api/meshes``, cached per client); unknown →
            ``mesh_not_found``, more than one match → ``mesh_ambiguous``.
            Never persisted — only ``meshes.use(..., persist=True)`` writes
            the config.
        active_mesh_id: the 0.3 spelling — a mesh UUID, sent as-is. Same
            precedence slot as ``mesh=``.
        config_path: explicit path to the config file (default
            ``meshbook.config.config_path()``).
        timeout: per-request timeout in seconds (default 30).
        download_timeout: timeout for file downloads (default
            ``max(timeout, 120)``).
        max_attempts: attempts per request when the server answers 429 or
            503 (default 3; 1 disables retrying). Only GET/HEAD/PUT/DELETE
            are retried, never POST/PATCH; ``Retry-After`` is honoured
            (seconds or an HTTP date, capped at 30 s).
        auth: ``"bearer"`` or ``"agent"`` (mint §93 tokens from the
            enrolled key for every call). Default ``None`` resolves it: an
            explicit ``token=`` or ``MESHBOOK_TOKEN`` means bearer;
            otherwise the config's ``auth_mode: "agent"`` turns the agent
            lane on — with the stored bearer as the fallback if a mint
            fails, exactly as meshbook-cli and meshbook-mcp behaved (§96).
            An explicit ``auth="agent"`` never falls back.
        agent_key_path: where the agent private key lives. Defaults to
            ``agent-key.pem`` beside the config file — the same file every
            meshbook client writes — with the mint bundle alongside as
            ``agent-key.json``. One config dir means one agent identity.
        user_agent: a product token to put in front of the SDK's own
            (e.g. ``"meshbook-cli/0.12.0"``), so server logs can tell the
            CLI's traffic from the MCP's.
    """

    def __init__(
        self,
        token: str | None = None,
        base: str | None = None,
        active_mesh_id: str | None = None,
        config_path: str | os.PathLike[str] | None = None,
        timeout: float = 30.0,
        auth: str | None = None,
        agent_key_path: str | os.PathLike[str] | None = None,
        *,
        mesh: str | None = None,
        download_timeout: float | None = None,
        max_attempts: int = 3,
        user_agent: str | None = None,
    ):
        if auth not in (None, "bearer", "agent"):
            raise MeshbookError(
                "bad_auth", f"Unknown auth mode {auth!r}. Use 'bearer' (default) or 'agent'."
            )
        if max_attempts < 1:
            raise MeshbookError("bad_argument", "max_attempts must be >= 1.")
        self.config_path: Path = (
            Path(config_path).expanduser() if config_path else _config.config_path()
        )
        #: Why the config file could not be used, when it exists but is
        #: broken (a MeshbookError, code config_unreadable or
        #: config_write_failed); None otherwise. A call that then has no
        #: token raises THIS instead of "no API token" (critique C-40):
        #: the token may well be in the broken file, and "run mesh login"
        #: was the advice that overwrote it.
        self.config_error: MeshbookError | None = None
        try:
            cfg = _config.load_config(self.config_path, strict=True)
        except MeshbookError as e:
            cfg = {}
            self.config_error = e
            log.warning("meshbook config ignored: %s", e.message)
        self.config: dict[str, Any] = cfg

        env_token = os.environ.get("MESHBOOK_TOKEN") or None
        cfg_token = cfg.get("token") if isinstance(cfg.get("token"), str) else None
        self.token: str | None = token or env_token or cfg_token

        cfg_base = cfg.get("base") if isinstance(cfg.get("base"), str) else None
        resolved_base = base or os.environ.get("MESHBOOK_BASE") or cfg_base or DEFAULT_BASE
        self.base = _check_base(resolved_base)

        # ── mesh precedence: Client(mesh/active_mesh_id) > env > config ──
        if mesh and active_mesh_id and mesh != active_mesh_id:
            raise MeshbookError(
                "bad_argument", "Pass mesh= or active_mesh_id=, not both with different values."
            )
        # A SET-but-empty mesh (mesh="", MESHBOOK_MESH="" from an unset
        # shell variable) is a caller bug, not "no preference": falling
        # through to the config's persisted mesh would write somewhere the
        # caller never named (critique C-10). Refuse it.
        for label, given in (("mesh", mesh), ("active_mesh_id", active_mesh_id)):
            if given is not None and not str(given).strip():
                raise MeshbookError(
                    "bad_argument",
                    f"{label}= is empty. Give a mesh id or name, or leave it out (None).",
                )
        raw_env_mesh = os.environ.get("MESHBOOK_MESH")
        if raw_env_mesh is not None and not raw_env_mesh.strip():
            raise MeshbookError(
                "bad_argument",
                "MESHBOOK_MESH is set but empty. Unset it, or set it to a mesh id or name.",
            )
        self._active_mesh_id: str | None = None
        self._pending_mesh_ref: str | None = None
        self.mesh_source: str | None = None
        env_mesh = (raw_env_mesh or "").strip() or None
        cfg_mesh = cfg.get("active_mesh_id") if isinstance(cfg.get("active_mesh_id"), str) else None
        if active_mesh_id:
            self._active_mesh_id, self.mesh_source = str(active_mesh_id), "argument"
        elif mesh:
            self._set_mesh_ref(mesh, "argument")
        elif env_mesh:
            self._set_mesh_ref(env_mesh, "env")
        elif cfg_mesh:
            # The config key is an id by definition (only meshes.use writes it).
            self._active_mesh_id, self.mesh_source = cfg_mesh, "config"

        # ── auth lane ──
        self._agent_fallback = False
        if auth is not None:
            self.auth: str = auth
            self.auth_source = "argument"
        elif token or env_token:
            self.auth, self.auth_source = "bearer", ("argument" if token else "env")
        elif cfg.get("auth_mode") == "agent":
            self.auth, self.auth_source = "agent", "config"
            self._agent_fallback = True
        else:
            self.auth, self.auth_source = "bearer", "default"

        self.timeout = timeout
        self.download_timeout = (
            download_timeout if download_timeout is not None else max(timeout, 120.0)
        )
        self.max_attempts = max_attempts
        self.user_agent = f"{user_agent} {USER_AGENT}" if user_agent else USER_AGENT
        self.agent_key_path: Path = (
            Path(agent_key_path).expanduser()
            if agent_key_path
            else self.config_path.parent / "agent-key.pem"
        )

        self._me_cache: dict[str, Any] | None = None
        self._meshes_cache: list[Mesh] | None = None
        self._agent_token: str | None = None
        self._agent_token_expires_at: float = 0.0

        # Namespaces. _Agent lives in meshbook/agent.py (the one surface with
        # an optional dependency) and the resources import this module for
        # typing only, so both are imported here, not at module scope.
        from . import resources as _r
        from .agent import _Agent

        self.meshes = _r.Meshes(self)
        self.users = _r.Users(self)
        self.contacts = _r.Contacts(self)
        self.companies = _r.Companies(self)
        self.pipelines = _r.Pipelines(self)
        self.leads = _r.Leads(self)
        self.tasks = _r.Tasks(self)
        self.projects = _r.Projects(self)
        self.tags = _r.Tags(self)
        self.chat = _r.Chat(self)
        self.channels = _r.Channels(self)
        self.dms = _r.Dms(self)
        self.notifications = _r.Notifications(self)
        self.files = _r.Files(self)
        self.exports = _r.Exports(self)
        self.custom_fields = _r.CustomFields(self)
        self.saved_views = _r.SavedViews(self)
        self.events = _r.Events(self)
        self.agent = _Agent(self)

    # ── mesh resolution ───────────────────────────────────────────────

    def _set_mesh_ref(self, ref: str, source: str) -> None:
        ref = ref.strip()
        self.mesh_source = source
        if _is_uuid(ref):
            self._active_mesh_id, self._pending_mesh_ref = ref, None
        else:
            self._active_mesh_id, self._pending_mesh_ref = None, ref

    @property
    def active_mesh_id(self) -> str | None:
        """The client-level mesh id (resolving a pending NAME on first read)."""
        if self._pending_mesh_ref is not None:
            ref = self._pending_mesh_ref
            self._active_mesh_id = self._mesh_ref_to_id(ref)
            self._pending_mesh_ref = None
        return self._active_mesh_id

    @active_mesh_id.setter
    def active_mesh_id(self, value: str | None) -> None:
        self._active_mesh_id = str(value) if value else None
        self._pending_mesh_ref = None

    def _my_meshes(self, refresh: bool = False) -> list[Mesh]:
        if self._meshes_cache is None or refresh:
            payload = self._call("meshes.list")
            self._meshes_cache = [Mesh.from_api(m) for m in _item_list(payload)]
        return self._meshes_cache

    def _mesh_ref_to_id(self, ref: str) -> str:
        """A mesh UUID (returned as-is) or NAME (resolved) → id."""
        return self._mesh_lookup(ref, require_membership=False).id

    def _mesh_lookup(self, ref: str, *, require_membership: bool) -> Mesh:
        target = (ref or "").strip()
        if not target:
            raise MeshbookError(
                "bad_argument", "mesh= is empty. Give a mesh id or name, or leave it out (None)."
            )
        if _is_uuid(target) and not require_membership:
            return Mesh(id=target, name="", mesh_type="", member_role="", raw={"id": target})
        mine = self._my_meshes()
        if _is_uuid(target):
            low_id = target.lower()
            for m in mine:
                if m.id.lower() == low_id:
                    return m
            raise MeshbookError("mesh_not_found", f"No mesh with id {target} among yours.", 404)
        folded = target.casefold()
        hits = [m for m in mine if m.name.casefold() == folded]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            names = ", ".join(m.name for m in mine) or "(none)"
            raise MeshbookError(
                "mesh_not_found", f"No mesh matching {target!r}. Your meshes: {names}", 404
            )
        listing = "; ".join(f"{m.name} ({m.id})" for m in hits)
        raise MeshbookError(
            "mesh_ambiguous",
            f"{len(hits)} meshes are named {target!r}: {listing}. Pass the id instead.",
        )

    def resolve_mesh(self, mesh: str | None = None) -> str | None:
        """The mesh id a call would act in: per-call *mesh* (id or name) if
        given, else the client-level mesh. None when there is none."""
        if mesh is not None:
            return self._mesh_ref_to_id(mesh)
        return self.active_mesh_id

    def _require_mesh(self, mesh: str | None = None) -> str:
        mid = self.resolve_mesh(mesh)
        if not mid:
            raise MeshbookError(
                "no_active_mesh",
                "No active mesh. Pass mesh= (id or name), set MESHBOOK_MESH, or call "
                "client.meshes.use(...).",
            )
        return mid

    # ── credentials ───────────────────────────────────────────────────

    def _credential(self) -> tuple[str, bool]:
        """(bearer, minted) for an authenticated call.

        ``auth="bearer"`` → the long-lived ``mb_token_``. ``auth="agent"`` →
        a §93 token minted from the enrolled key (cached, re-minted 30 s
        before expiry). When the agent lane came from the config file
        (``auth_mode: "agent"``), a bench with no usable key, or a failed
        mint, falls back to the stored bearer if there is one — the §96
        contract both reference clients implement.
        """
        if self.auth == "agent":
            if self._agent_fallback and not self.agent.ready():
                if self.token:
                    return self.token, False
                raise MeshbookError(
                    "not_authenticated",
                    f"auth_mode is 'agent' but there is no usable agent key at "
                    f"{self.agent_key_path}, and no bearer is stored. Enroll a key or run "
                    "`mesh login`.",
                )
            try:
                return self.agent.token(), True
            except MeshbookError as e:
                if self._agent_fallback and self.token:
                    log.warning(
                        "agent-token mint failed (%s); falling back to the stored bearer "
                        "for this call.",
                        e.code,
                    )
                    return self.token, False
                raise
        if not self.token:
            if self.config_error is not None:
                raise MeshbookError(
                    self.config_error.code,
                    self.config_error.message,
                    self.config_error.status,
                    hint=self.config_error.hint,
                )
            raise MeshbookError(
                "not_authenticated",
                "No API token. Pass token=, set MESHBOOK_TOKEN, or run `mesh login`.",
            )
        return self.token, False

    def _bearer(self) -> str:
        """The credential for an authenticated call (0.3 helper)."""
        return self._credential()[0]

    @property
    def auth_mode(self) -> str:
        """The lane the next call will actually use: ``"agent"`` or ``"bearer"``."""
        if self.auth == "agent" and (not self._agent_fallback or self.agent.ready()):
            return "agent"
        return "bearer"

    # ── transport ─────────────────────────────────────────────────────

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = self.base + path
        if params:
            clean = {k: _query_value(v) for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        require_auth: bool = True,
        token: str | None = None,
        mesh_id: str | None = None,
        raw: bool = False,
    ) -> Any:
        """One logical request: auth, headers, 429/503 retries, one agent re-mint.

        Returns parsed JSON, or ``(bytes, headers)`` when *raw*.
        """
        url = self._url(path, params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        method = method.upper()
        attempt = 0
        reminted = False
        while True:
            attempt += 1
            headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
            minted = False
            if require_auth:
                if token is not None:
                    bearer = token
                else:
                    bearer, minted = self._credential()
                headers["Authorization"] = f"Bearer {bearer}"
                # Unauthenticated endpoints never carry the active mesh.
                if mesh_id:
                    headers["X-Active-Mesh-Id"] = str(mesh_id)
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            if _on_request is not None:
                _on_request(method, url)
            timeout = self.download_timeout if raw else self.timeout
            try:
                with _urlopen(req, timeout=timeout) as resp:
                    content = resp.read()
                    resp_headers = getattr(resp, "headers", None) or {}
            except urllib.error.HTTPError as e:
                # §96: a 401 on a self-minted token usually means it died
                # mid-flight (expiry race, source rotation). Re-mint ONCE.
                # Safe for any method: a 401 is refused before the route runs.
                if e.code == 401 and minted and not reminted:
                    reminted = True
                    attempt -= 1
                    self.agent._forget_token()
                    _close_quietly(e)
                    continue
                if (
                    e.code in RETRYABLE_STATUSES
                    and method in RETRYABLE_METHODS
                    and attempt < self.max_attempts
                ):
                    delay = _retry_after(getattr(e, "headers", None))
                    _close_quietly(e)
                    _sleep(delay if delay is not None else _backoff(attempt))
                    continue
                _raise_from_http_error(e)
                raise  # unreachable; keeps type checkers honest
            except urllib.error.URLError as e:
                maybe_sent = isinstance(e.reason, (TimeoutError, ConnectionResetError))
                raise MeshbookError(
                    "network_error",
                    _network_message(method, path, e.reason, maybe_sent),
                    hint=_network_hint(method, maybe_sent),
                ) from e
            except (OSError, http.client.HTTPException) as e:
                # Read-phase failures (a timeout mid-body, a reset, a short
                # read) arrive raw, not wrapped in URLError. The request was
                # sent, so for a write the outcome is unknown.
                raise MeshbookError(
                    "network_error",
                    _network_message(method, path, e, True),
                    hint=_network_hint(method, True),
                ) from e
            break
        if raw:
            return content, resp_headers
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise MeshbookError(
                "bad_response", f"{method} {path} returned a non-JSON body ({len(text)} chars)."
            ) from e
        # Defensive: a 200 body carrying {ok: false, error: {...}} is
        # still an error under the envelope contract.
        if isinstance(payload, dict) and payload.get("ok") is False:
            err = payload.get("error") or {}
            raise MeshbookError(
                str(err.get("code", "api_error")),
                str(err.get("message", "API reported ok=false")),
                200,
            )
        return payload

    def _endpoint_mesh(
        self, ep: Endpoint, path_params: Mapping[str, Any] | None, mesh: str | None
    ) -> str | None:
        if ep.mesh == "none" or not ep.auth:
            return None
        if ep.mesh == "path":
            return str((path_params or {})["mesh_id"])
        return self.resolve_mesh(mesh)

    def _call(
        self,
        key: str,
        *,
        path: Mapping[str, Any] | None = None,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        mesh: str | None = None,
        token: str | None = None,
    ) -> Any:
        """The ONLY way namespace code reaches the network: a registry key
        from :mod:`meshbook._endpoints`, never a free-form path."""
        ep = ENDPOINTS[key]
        return self._send(
            ep.method,
            ep.render(dict(path) if path else None),
            body=body,
            params=params,
            require_auth=ep.auth,
            token=token,
            mesh_id=self._endpoint_mesh(ep, path, mesh),
        )

    def _list(
        self,
        key: str,
        *,
        path: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
        cls: type[ItemList[Any]] = ItemList,
        lead: Mapping[str, Any] | None = None,
    ) -> ItemList[Any]:
        # Query order: `lead` (e.g. a search term), then pagination, then
        # filters: the exact order the 0.3 wire used, so URLs are stable.
        query: dict[str, Any] = dict(lead or {})
        query.update(_page(limit, offset))
        query.update(params or {})
        payload = self._call(key, path=path, params=query, mesh=mesh)
        return _item_list(payload, limit=limit, offset=offset, cls=cls)

    def _download_bytes(
        self, key: str, *, path: Mapping[str, Any], mesh: str | None = None
    ) -> DownloadedFile:
        """GET a download route → :class:`DownloadedFile` (sanitised name, bytes)."""
        ep = ENDPOINTS[key]
        content, headers = self._send(
            ep.method,
            ep.render(dict(path)),
            require_auth=ep.auth,
            mesh_id=self._endpoint_mesh(ep, path, mesh),
            raw=True,
        )
        return DownloadedFile(
            safe_filename(_filename_from_disposition(_disposition(headers))), bytes(content)
        )

    def _download(
        self,
        key: str,
        *,
        path: Mapping[str, Any],
        out_path: str | os.PathLike[str] | None,
        mesh: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Download and write to disk, safely.

        * *out_path* ``None`` → the current directory; a directory (existing,
          or a string ending in a separator) → the server's filename inside
          it, reduced by :func:`safe_filename` and checked to resolve
          directly inside that directory (``unsafe_path`` otherwise); any
          other path → exactly that file.
        * An existing file is never replaced unless ``overwrite=True``
          (``file_exists``). An explicit file path is checked BEFORE the
          request, so a refusal costs no download.
        * Written atomically: a temp file in the target directory, then a
          rename (``overwrite=True``) or a no-clobber hard link.
        """
        if out_path is not None and not overwrite and not _is_dir_hint(out_path):
            early = Path(out_path).expanduser()
            if os.path.lexists(early):
                _refuse_existing(early)
        got = self._download_bytes(key, path=path, mesh=mesh)
        target, directory = _download_target(out_path, got.filename)
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise MeshbookError("write_failed", f"Cannot create {directory}: {e}") from e
            _assert_contained(target, directory)
        if not overwrite and os.path.lexists(target):
            _refuse_existing(target)
        _atomic_write(target, got.content, overwrite=overwrite)
        return target

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        require_auth: bool = True,
        token: str | None = None,
        send_active_mesh: bool = True,
        mesh: str | None = None,
    ) -> Any:
        """Raw escape hatch: call any /api path, get the parsed JSON payload
        back (envelope NOT stripped — use ``meshbook.client._data`` /
        ``_item_list`` or the namespaced methods for that).

        Unchecked by the contract check by design: namespace code never uses
        it (a test enforces that). ``token=`` overrides this call's
        Authorization; ``send_active_mesh=False`` suppresses
        X-Active-Mesh-Id; ``mesh=`` sends that mesh (id or name) instead of
        the client's.
        """
        mesh_id = self.resolve_mesh(mesh) if (send_active_mesh and require_auth) else None
        return self._send(
            method,
            path,
            body=body,
            params=params,
            require_auth=require_auth,
            token=token,
            mesh_id=mesh_id,
        )

    def download_raw(self, path: str, *, mesh: str | None = None) -> tuple[bytes, str]:
        """Raw-bytes GET of any path (downloads are not JSON). Returns
        ``(bytes, server_filename)`` from Content-Disposition when present.

        The filename is the server's, UNSANITISED (0.3 behaviour, kept): run
        it through :func:`meshbook.safe_filename` before using it in a path,
        or use the namespaced ``download_bytes()`` methods, which do.
        ``mesh=`` (id or name) sends that mesh instead of the client's.
        """
        content, headers = self._send("GET", path, mesh_id=self.resolve_mesh(mesh), raw=True)
        return content, _filename_from_disposition(_disposition(headers))

    # ── identity ──────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """GET /api/health — unauthenticated reachability probe (``mesh doctor``)."""
        out = _data(self._call("health"))
        return out if isinstance(out, dict) else {"ok": True}

    def me(self) -> dict[str, Any]:
        """GET /api/me — full payload dict (user, activeMeshId, …).

        /api/me is permissive and answers 200 with ``{authenticated: false}``
        for a bad token; this converts that into a MeshbookError so callers
        never see a half-auth state. Sends no active mesh: who you are does
        not depend on where you are (and a stale out-of-scope mesh must not
        be able to break the one call that diagnoses it).
        """
        me = _data(self._call("me"))
        if isinstance(me, dict) and me.get("authenticated") is False:
            raise MeshbookError(
                "not_authenticated",
                "/api/me reports authenticated=false — token invalid or revoked.",
                401,
                hint="reauthenticate",
            )
        if isinstance(me, dict):
            self._me_cache = me
            return me
        return {}

    def whoami(self) -> User:
        """The signed-in user as a typed :class:`User`."""
        me = self.me()
        user = me.get("user") or {}
        if not user:
            raise MeshbookError("no_user", "Authenticated but /api/me returned no user.", 200)
        return User.from_api(user)

    def me_id(self, *, refresh: bool = False) -> str:
        """Your own user id, from ``GET /api/me``, cached per client.

        The first call (or ``refresh=True``) costs one request; later calls
        are free, as is any call after :meth:`me` / :meth:`whoami` already
        fetched it. Raises ``no_user`` when /api/me names no user, and
        ``not_authenticated`` for a bad token.
        """
        if refresh or self._me_cache is None:
            self.me()
        uid = str(((self._me_cache or {}).get("user") or {}).get("id") or "")
        if not uid:
            raise MeshbookError("no_user", "Authenticated but /api/me returned no user id.", 200)
        return uid

    def _self_user_id(self) -> str:
        """0.4.0 private spelling of :meth:`me_id`, kept for callers that used it."""
        return self.me_id()


#: The contract's spelling.
Client = MeshbookClient


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _close_quietly(e: urllib.error.HTTPError) -> None:
    try:
        e.close()
    except Exception:
        pass
