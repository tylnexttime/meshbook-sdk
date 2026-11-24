"""meshbook.testing: offline fakes for code built on this SDK (no network, ever).

:class:`FakeNotificationServer` is a STATEFUL stand-in for meshbook.org's
notification routes, made so a client that trusts the wrong signal goes red
(DEV-DEBT §106, critique C-08). It replaces ``meshbook.client._urlopen``, so
it serves meshbook-sdk and everything built on it (meshbook-cli,
meshbook-mcp) alike::

    from meshbook.testing import FakeNotificationServer, MESH_A, MESH_B

    def test_marks_through_the_right_mesh(monkeypatch):
        fake = FakeNotificationServer(mode="old").install(monkeypatch)
        nid = fake.add(mesh=MESH_A, title="hello")
        client = meshbook.Client(token="t", mesh=MESH_B)
        out = client.notifications.mark_read(nid)
        assert fake.row(nid).is_read and out["ok"]

What it models, from the server source (``app/routers/notifications.py``):

* **Visibility**: a row is seen by a request when it is the caller's, not
  dismissed, and cross-mesh OR in the mesh named by ``X-Active-Mesh-Id``
  (no header: cross-mesh rows only).
* **Listing**: newest first (createdAt, then id), ``limit`` 50 by default and
  200 at most (more is a 422), ``offset`` paging, ``unreadOnly``, ``kind``
  overriding ``group``; the envelope's ``total`` is the PAGE length, as in
  production.
* ``mode="old"`` is the server in production on 2026-11-24: PATCH answers
  ``{"id", "updated": true}`` whether or not a row matched, coerces the body
  with bool() on key presence, keeps ``readAt`` on mark-unread, re-stamps on
  repeat; an unknown ``group`` is silently ignored (list and read-all filter
  NOTHING); read-all answers ``{"marked": true}``.
* ``mode="fixed"`` is the s106 server branch (meshbook s106-notifications @
  499e281): a PATCH miss is 404 ``notification_not_found`` in the error
  envelope; a hit echoes ``{id, updated, isRead, readAt, dismissedAt}``
  (the row AFTER the change); a non-boolean ``isRead``/``is_read``,
  ``dismiss: false`` or disagreeing spellings are a 422; ``readAt`` is set
  only on an unread-to-read change and NULL on unread; a repeat dismiss
  keeps the first ``dismissedAt``; an unknown group is a 422
  ``unknown_notification_group`` on list and read-all; read-all adds
  ``count``.
* In both modes PATCH still reaches a DISMISSED row (dismissal hides a row
  from list, summary and read-all only), and an empty body is ``{"noop": true}``.
* **Tokens**: ``scope_meshes={...}`` answers 403 ``token_out_of_scope`` to
  any request whose header names another mesh; ``readonly=True`` answers 403
  ``token_readonly`` to every non-GET.
* Also ``GET /api/me`` and ``GET /api/meshes`` (from ``meshes=``).

Faults: :meth:`~FakeNotificationServer.fail` (an HTTP status or a lost
answer, optionally AFTER the write landed), :meth:`~FakeNotificationServer.drop`
(a PATCH that is answered like a hit but changes nothing) and the
``before_patch`` / ``after_patch`` hooks (rows arriving, vanishing or
changing in between). Every request is logged in ``.log``; an unexpected one
raises ``AssertionError``, so a test cannot pass on a call it did not plan.
"""

from __future__ import annotations

import datetime as _dt
import email.message
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ._endpoints import ENDPOINTS
from .resources.notifications import NOTIFICATION_GROUP_KINDS

#: Your own user id in the fake, and someone else's.
SELF_ID = "aaaaaaaa-0000-4000-8000-0000000000a1"
OTHER_USER_ID = "aaaaaaaa-0000-4000-8000-0000000000b2"
#: Three meshes you belong to by default, and one you do not.
MESH_A = "11111111-1111-4111-8111-111111111111"
MESH_B = "22222222-2222-4222-8222-222222222222"
MESH_C = "33333333-3333-4333-8333-333333333333"
FOREIGN_MESH = "44444444-4444-4444-8444-444444444444"
DEFAULT_MESHES: tuple[tuple[str, str], ...] = (
    (MESH_A, "Alpha"),
    (MESH_B, "Beta"),
    (MESH_C, "Gamma"),
)

_UTC = _dt.timezone.utc
_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_UTC)
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def _iso(when: _dt.datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


@dataclass
class FakeRow:
    """One notification row, as the server stores it."""

    id: str
    user: str
    mesh: str | None
    kind: str
    title: str | None
    cross_mesh: bool
    is_read: bool
    read_at: _dt.datetime | None
    dismissed_at: _dt.datetime | None
    created_at: _dt.datetime
    actor: str | None = None

    def to_api(self) -> dict[str, Any]:
        """The row as ``GET /api/notifications`` sends it (``_row_to_dict``)."""
        title = (self.title or "").strip()
        return {
            "id": self.id,
            "meshId": self.mesh,
            "kind": self.kind,
            "title": self.title,
            "summary": title or self.kind,
            "bodyMd": None,
            "linkEntityType": None,
            "linkEntityId": None,
            "linkSecondaryId": None,
            "severity": "info",
            "actorUserId": self.actor,
            "isCrossMesh": self.cross_mesh,
            "isRead": self.is_read,
            "readAt": _iso(self.read_at),
            "createdAt": _iso(self.created_at),
        }


@dataclass
class FakeRequest:
    """One request the fake received."""

    method: str
    path: str
    query: dict[str, str]
    mesh: str | None  # the X-Active-Mesh-Id header, or None when absent
    body: Any  # parsed JSON body, or None
    route: str | None  # registry key it matched ("notifications.mark" …)


@dataclass
class _Fault:
    method: str
    route: str | None
    nid: str | None
    query: dict[str, str] | None
    status: int
    payload: Any
    network: bool
    after: bool
    times: int


class _Response:
    def __init__(self, payload: Any, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Reply(Exception):
    """Internal: an HTTP error answer."""

    def __init__(self, status: int, payload: Any):
        super().__init__(status)
        self.status = status
        self.payload = payload


def _coded(status: int, code: str, message: str, **extra: Any) -> _Reply:
    """A coded HTTPException as the app's global handler sends it."""
    detail = {"code": code, "message": message, **extra}
    return _Reply(status, {"ok": False, "error": detail, "detail": detail})


def _middleware(status: int, code: str, message: str) -> _Reply:
    return _Reply(status, {"ok": False, "error": {"code": code, "message": message}})


def _invalid(loc: list[str], kind: str, msg: str) -> _Reply:
    return _Reply(
        422,
        {
            "ok": False,
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "fields": [{"loc": loc, "type": kind, "msg": msg}],
            },
        },
    )


def _template(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "([^/]+)", re.escape(path)) + "$")


class FakeNotificationServer:
    """A stateful fake of the notification routes. See the module docstring.

    Args:
        mode: ``"old"`` (production on 2026-11-24) or ``"fixed"`` (the s106
            server branch).
        user_id: who "you" are (rows of other users are never visible).
        meshes: ``(id, name)`` pairs ``GET /api/meshes`` returns.
        scope_meshes: a mesh-scoped token's meshes (None: unscoped).
        readonly: a read-only token (every non-GET answers 403).
        group_kinds: the server's tab → kinds table (default: the SDK's mirror).
    """

    def __init__(
        self,
        *,
        mode: str = "old",
        user_id: str = SELF_ID,
        meshes: tuple[tuple[str, str], ...] | list[tuple[str, str]] = DEFAULT_MESHES,
        scope_meshes: set[str] | frozenset[str] | None = None,
        readonly: bool = False,
        group_kinds: Mapping[str, tuple[str, ...] | list[str]] | None = None,
    ):
        if mode not in ("old", "fixed"):
            raise ValueError("mode must be 'old' or 'fixed'")
        self.mode = mode
        self.user_id = user_id
        self.meshes: list[tuple[str, str]] = list(meshes)
        self.scope_meshes = {m.lower() for m in scope_meshes} if scope_meshes else None
        self.readonly = readonly
        self.group_kinds: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (group_kinds or NOTIFICATION_GROUP_KINDS).items()
        }
        self.rows: dict[str, FakeRow] = {}
        self.log: list[FakeRequest] = []
        #: The server clock; every write stamps the current value, then ticks.
        self.clock = _dt.datetime(2026, 11, 24, 9, 0, tzinfo=_UTC)
        #: Called with (server, notification id) just before / after a PATCH
        #: is processed (after: only when it was a hit).
        self.before_patch: Callable[[FakeNotificationServer, str], None] | None = None
        self.after_patch: Callable[[FakeNotificationServer, str], None] | None = None
        self._faults: list[_Fault] = []
        self._dropped: set[str] = set()
        self._seq = 0
        self.sleeps: list[float] = []
        self._routes = {
            key: (ENDPOINTS[key].method, _template(ENDPOINTS[key].path))
            for key in (
                "me",
                "meshes.list",
                "notifications.list",
                "notifications.summary",
                "notifications.mark",
            )
        }
        # Not in the SDK's registry (the SDK never calls it); served so a
        # wrapper's test can prove it never does either, or exercise it.
        self._read_all_path = ENDPOINTS["notifications.list"].path + "/read-all"

    # ── building state ───────────────────────────────────────────────

    def add(
        self,
        *,
        mesh: str | None = None,
        cross_mesh: bool = False,
        kind: str = "Mention",
        title: str | None = None,
        is_read: bool = False,
        read_at: _dt.datetime | None = None,
        dismissed: bool = False,
        created_at: _dt.datetime | None = None,
        user: str | None = None,
        actor: str | None = None,
        id: str | None = None,
    ) -> str:
        """Add a row; returns its id. Rows added later are NEWER (listed
        first) unless ``created_at`` says otherwise. A read row with no
        ``read_at`` gets its creation time; pass ``is_read=False`` with a
        ``read_at`` for the stale-``readAt`` case (a row marked unread)."""
        if not cross_mesh and mesh is None:
            raise ValueError("a mesh-scoped row needs mesh= (or pass cross_mesh=True)")
        self._seq += 1
        nid = (id or f"{self._seq:08x}-0000-4000-8000-{self._seq:012x}").lower()
        when = created_at or (_EPOCH + _dt.timedelta(minutes=self._seq))
        self.rows[nid] = FakeRow(
            id=nid,
            user=user or self.user_id,
            mesh=mesh.lower() if mesh else None,
            kind=kind,
            title=title if title is not None else f"{kind} #{self._seq}",
            cross_mesh=cross_mesh,
            is_read=is_read,
            read_at=read_at if read_at is not None or not is_read else when,
            dismissed_at=self.clock if dismissed else None,
            created_at=when,
            actor=actor,
        )
        return nid

    def add_many(self, n: int, **kwargs: Any) -> list[str]:
        """Add *n* rows (oldest first); returns their ids in that order."""
        return [self.add(**kwargs) for _ in range(n)]

    def row(self, nid: str) -> FakeRow:
        return self.rows[nid.lower()]

    def remove(self, nid: str) -> None:
        """Delete a row outright (as if it never existed)."""
        self.rows.pop(nid.lower(), None)

    # ── faults ───────────────────────────────────────────────────────

    def fail(
        self,
        method: str,
        *,
        route: str | None = None,
        id: str | None = None,
        query: Mapping[str, str] | None = None,
        status: int = 503,
        payload: Any = None,
        network: bool = False,
        after: bool = False,
        times: int = 1,
    ) -> FakeNotificationServer:
        """Make the next *times* matching requests fail.

        ``status`` / ``payload``: the HTTP error (default 503 with a coded
        body). ``network=True`` loses the answer instead (a timeout, as the
        SDK sees it: the write may or may not have landed). ``after=True``
        processes the request FIRST, so the write lands and then the answer
        fails. ``route`` is a registry key; ``id`` narrows a PATCH to one row;
        ``query`` to requests whose query string holds those values
        (``{"offset": "200"}``).
        """
        self._faults.append(
            _Fault(
                method=method.upper(),
                route=route,
                nid=id.lower() if id else None,
                query=dict(query) if query else None,
                status=status,
                payload=payload,
                network=network,
                after=after,
                times=times,
            )
        )
        return self

    def drop(self, nid: str) -> FakeNotificationServer:
        """PATCHes of this row are answered like a hit but change nothing
        (the silent miss, as a test can force it whatever mesh is sent)."""
        self._dropped.add(nid.lower())
        return self

    # ── installing ───────────────────────────────────────────────────

    def install(self, monkeypatch: Any) -> FakeNotificationServer:
        """Route ``meshbook.client`` through this fake for one pytest test
        (also makes back-off sleeps instant, recorded in ``.sleeps``)."""
        from . import client as _client

        monkeypatch.setattr(_client, "_urlopen", self)
        monkeypatch.setattr(_client, "_sleep", self.sleeps.append)
        return self

    @contextmanager
    def patched(self) -> Iterator[FakeNotificationServer]:
        """The same as :meth:`install` for code without pytest's monkeypatch."""
        from unittest import mock

        from . import client as _client

        with (
            mock.patch.object(_client, "_urlopen", self),
            mock.patch.object(_client, "_sleep", self.sleeps.append),
        ):
            yield self

    # ── inspection ───────────────────────────────────────────────────

    def requests(self, method: str | None = None, route: str | None = None) -> list[FakeRequest]:
        return [
            r
            for r in self.log
            if (method is None or r.method == method.upper())
            and (route is None or r.route == route)
        ]

    @property
    def patches(self) -> list[FakeRequest]:
        """Every PATCH received, in order."""
        return self.requests("PATCH")

    # ── the transport ────────────────────────────────────────────────

    def __call__(self, req: urllib.request.Request, timeout: float) -> _Response:
        method = req.get_method().upper()
        split = urllib.parse.urlsplit(req.get_full_url())
        query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
        mesh = next((v for k, v in req.header_items() if k.lower() == "x-active-mesh-id"), None)
        raw = req.data if isinstance(req.data, bytes) else None
        body = json.loads(raw.decode("utf-8")) if raw else None
        route, params = self._match(method, split.path)
        entry = FakeRequest(method, split.path, query, mesh.lower() if mesh else None, body, route)
        self.log.append(entry)
        if route is None:
            raise AssertionError(f"FakeNotificationServer: unrouted {method} {split.path}")
        fault = self._fault_for(entry, params)
        try:
            if fault is not None and not fault.after:
                self._raise_fault(req, fault)
            payload = self._serve(entry, params)
            if fault is not None:
                self._raise_fault(req, fault)
        except _Reply as reply:
            raise urllib.error.HTTPError(
                req.get_full_url(),
                reply.status,
                "error",
                email.message.Message(),
                io.BytesIO(json.dumps(reply.payload).encode("utf-8")),
            ) from None
        return _Response(payload)

    def _match(self, method: str, path: str) -> tuple[str | None, tuple[str, ...]]:
        if method == "POST" and path == self._read_all_path:
            return "notifications.read_all", ()
        for key, (m, rx) in self._routes.items():
            hit = rx.match(path)
            if m == method and hit:
                return key, tuple(urllib.parse.unquote(g) for g in hit.groups())
        return None, ()

    def _fault_for(self, entry: FakeRequest, params: tuple[str, ...]) -> _Fault | None:
        for fault in self._faults:
            if fault.times <= 0 or fault.method != entry.method:
                continue
            if fault.route is not None and fault.route != entry.route:
                continue
            if fault.nid is not None and (not params or params[0].lower() != fault.nid):
                continue
            if fault.query is not None and any(
                entry.query.get(k) != v for k, v in fault.query.items()
            ):
                continue
            fault.times -= 1
            return fault
        return None

    def _raise_fault(self, req: urllib.request.Request, fault: _Fault) -> None:
        if fault.network:
            raise urllib.error.URLError(TimeoutError("timed out (FakeNotificationServer)"))
        payload = fault.payload
        if payload is None:
            payload = {
                "ok": False,
                "error": {"code": "service_unavailable", "message": "injected failure"},
            }
        raise _Reply(fault.status, payload)

    # ── routes ───────────────────────────────────────────────────────

    def _serve(self, entry: FakeRequest, params: tuple[str, ...]) -> Any:
        mesh = entry.mesh
        if self.readonly and entry.method not in ("GET", "HEAD", "OPTIONS"):
            raise _middleware(403, "token_readonly", "This API token is read-only.")
        if self.scope_meshes is not None and mesh and mesh not in self.scope_meshes:
            raise _middleware(
                403, "token_out_of_scope", "This API token isn't authorised for the active mesh."
            )
        if entry.route == "me":
            return {
                "ok": True,
                "data": {
                    "authenticated": True,
                    "user": {"id": self.user_id, "username": "fake-self", "displayName": "Fake"},
                    "activeMeshId": None,
                },
            }
        if entry.route == "meshes.list":
            items = [
                {"id": i, "name": n, "meshType": "family", "memberRole": "member"}
                for i, n in self.meshes
            ]
            return _ok_list(items)
        if entry.route == "notifications.list":
            return self._list(entry)
        if entry.route == "notifications.summary":
            return self._summary(mesh)
        if entry.route == "notifications.read_all":
            return self._read_all(entry)
        return self._patch(entry, params[0])

    def _visible(self, row: FakeRow, mesh: str | None) -> bool:
        return (
            row.user == self.user_id
            and row.dismissed_at is None
            and (row.cross_mesh or (mesh is not None and row.mesh == mesh))
        )

    def _group_kinds(self, query: Mapping[str, str]) -> tuple[str, ...] | None:
        group = query.get("group") or None
        if group is None:
            return None
        if group in self.group_kinds:
            return self.group_kinds[group]
        if self.mode == "fixed":
            allowed = list(self.group_kinds)
            raise _coded(
                422,
                "unknown_notification_group",
                f"Unknown notification group. Use one of: {', '.join(allowed)}.",
                allowed=allowed,
            )
        return None  # the old server ignores it: no filter at all

    def _list(self, entry: FakeRequest) -> Any:
        q = entry.query
        limit = _int_param(q, "limit", 50)
        offset = _int_param(q, "offset", 0)
        if limit > 200:
            raise _invalid(
                ["query", "limit"], "less_than_equal", "Input should be less than or equal to 200"
            )
        unread_only = _bool_param(q, "unreadOnly")
        kinds = self._group_kinds(q)  # validated even when kind= overrides it
        if q.get("kind"):
            kinds = (q["kind"],)
        rows = [r for r in self.rows.values() if self._visible(r, entry.mesh)]
        if unread_only:
            rows = [r for r in rows if not r.is_read]
        if kinds is not None:
            rows = [r for r in rows if r.kind in kinds]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return _ok_list([r.to_api() for r in rows[offset : offset + limit]])

    def _summary(self, mesh: str | None) -> Any:
        rows = [r for r in self.rows.values() if self._visible(r, mesh)]
        out: dict[str, Any] = {
            tab: sum(1 for r in rows if not r.is_read and r.kind in kinds)
            for tab, kinds in self.group_kinds.items()
        }
        out["unread"] = sum(1 for r in rows if not r.is_read)
        out["unreadCrossMesh"] = sum(1 for r in rows if not r.is_read and r.cross_mesh)
        out["total"] = len(rows)
        return {"ok": True, "data": out}

    def _read_all(self, entry: FakeRequest) -> Any:
        kinds = self._group_kinds(entry.query)
        changed = 0
        for r in self.rows.values():
            if (
                self._visible(r, entry.mesh)
                and not r.is_read
                and (kinds is None or r.kind in kinds)
            ):
                r.is_read, r.read_at = True, self._tick()
                changed += 1
        data: dict[str, Any] = {"marked": True}
        if self.mode == "fixed":
            data["count"] = changed
        return {"ok": True, "data": data}

    def _patch(self, entry: FakeRequest, raw_id: str) -> Any:
        try:
            nid = str(_uuid.UUID(raw_id))
        except ValueError:
            raise _invalid(
                ["path", "notification_id"], "uuid_parsing", "Input should be a valid UUID"
            ) from None
        body = entry.body
        if self.mode == "fixed":
            want_read, dismiss = self._strict_body(body)
        else:
            want_read, dismiss = _loose_body(body)
        if want_read is None and not dismiss:
            return {"ok": True, "data": {"noop": True}}
        if self.before_patch is not None:
            self.before_patch(self, nid)
        row = self.rows.get(nid)
        # The PATCH's own WHERE: id, user, and cross-mesh OR the header's
        # mesh. NOT dismissed_at: a dismissed row is still reachable.
        hit = (
            row is not None
            and row.user == self.user_id
            and (row.cross_mesh or (entry.mesh is not None and row.mesh == entry.mesh))
        )
        if hit and row is not None and nid not in self._dropped:
            self._apply(row, want_read, dismiss)
            if self.after_patch is not None:
                self.after_patch(self, nid)
        if self.mode == "old":
            return {"ok": True, "data": {"id": nid, "updated": True}}
        if not hit or row is None:
            raise _coded(
                404,
                "notification_not_found",
                "No notification with that id for this account in the active mesh. "
                "Check the row's mesh (cross-mesh rows match under any mesh).",
            )
        return {
            "ok": True,
            "data": {
                "id": nid,
                "updated": True,
                "isRead": row.is_read,
                "readAt": _iso(row.read_at),
                "dismissedAt": _iso(row.dismissed_at),
            },
        }

    def _strict_body(self, body: Any) -> tuple[bool | None, bool]:
        if not isinstance(body, dict):
            raise _invalid(["body"], "model_attributes_type", "Input should be a valid dictionary")
        values: list[bool] = []
        for key in ("isRead", "is_read"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise _invalid(["body", key], "bool_type", "Input should be a valid boolean")
                values.append(body[key])
        if len(values) == 2 and values[0] != values[1]:
            raise _invalid(["body"], "value_error", "isRead and is_read disagree")
        dismiss = False
        if "dismiss" in body:
            if body["dismiss"] is not True:
                raise _invalid(["body"], "value_error", "dismiss can only be true")
            dismiss = True
        return (values[0] if values else None), dismiss

    def _apply(self, row: FakeRow, want_read: bool | None, dismiss: bool) -> None:
        if want_read is not None:
            if self.mode == "old":
                # read_at = CASE WHEN :is_read THEN now() ELSE read_at END
                row.read_at = self._tick() if want_read else row.read_at
            elif want_read:
                row.read_at = row.read_at if row.is_read else self._tick()
            else:
                row.read_at = None
            row.is_read = want_read
        if dismiss:
            if self.mode == "old" or row.dismissed_at is None:
                row.dismissed_at = self._tick()

    def _tick(self) -> _dt.datetime:
        now = self.clock
        self.clock = now + _dt.timedelta(seconds=1)
        return now


def _loose_body(body: Any) -> tuple[bool | None, bool]:
    """The production server's reading: key presence, then bool()."""
    if not isinstance(body, dict):
        return None, False
    want: bool | None = None
    if "isRead" in body or "is_read" in body:
        want = bool(body.get("isRead", body.get("is_read")))
    return want, bool(body.get("dismiss"))


def _ok_list(items: list[Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {"items": items, "total": len(items), "page": 1, "pageSize": len(items)},
    }


def _int_param(query: Mapping[str, str], name: str, default: int) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _invalid(["query", name], "int_parsing", "Input should be a valid integer") from None
    if value < 0:
        raise _invalid(["query", name], "greater_than_equal", "Input should be >= 0")
    return value


def _bool_param(query: Mapping[str, str], name: str) -> bool:
    raw = (query.get(name) or "").lower()
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS or raw == "":
        return False
    raise _invalid(["query", name], "bool_parsing", "Input should be a valid boolean")


__all__ = [
    "DEFAULT_MESHES",
    "FOREIGN_MESH",
    "MESH_A",
    "MESH_B",
    "MESH_C",
    "OTHER_USER_ID",
    "SELF_ID",
    "FakeNotificationServer",
    "FakeRequest",
    "FakeRow",
]
