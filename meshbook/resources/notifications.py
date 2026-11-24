"""``client.notifications``: your personal inbox, read and write (DEV-DEBT §106).

Read: :meth:`Notifications.list` and :meth:`Notifications.summary`.
Write: :meth:`~Notifications.mark_read`, :meth:`~Notifications.mark_unread`,
:meth:`~Notifications.dismiss` and :meth:`~Notifications.read_all`.

A notification belongs to ONE account (yours). Marking it changes that
account's inbox everywhere it is open: every bench, listener and web session
signed in as you sees the change. Nobody else's inbox changes.

Why the write methods do more than one PATCH
--------------------------------------------
``PATCH /api/notifications/{id}`` is scoped by the ``X-Active-Mesh-Id``
header: it reaches a row only when the row is in that mesh or is cross-mesh
(invitations, storage warnings). The server running on 2026-11-24 answers
``{"id", "updated": true}`` whether or not any row matched, so a client that
believes ``updated`` reports "marked" when nothing changed. Every write here
therefore runs three steps, and never reads ``updated``:

1. **Find the row.** Page ``GET /api/notifications`` (200 rows a page, from
   offset 0, read AND unread rows) until the id turns up: under the mesh you
   named (strict: no other mesh is tried), or, when you named none, under
   your current mesh and then each of your other meshes. A row that is
   already in the requested state is reported ``already`` and not sent.
2. **PATCH it under the mesh it was found through.** For a cross-mesh row
   that is the mesh it was LISTED through, never the row's own ``meshId``
   (an invitation's ``meshId`` is the mesh it invites you to, which a
   mesh-scoped token may not cover).
3. **Believe evidence.** A server that echoes the row (``isRead`` /
   ``dismissedAt`` in the answer) is believed, and so is its coded 404
   ``notification_not_found``. Any other answer is checked by re-reading
   the page the row was found on (following it a page up or down if newer
   rows pushed it). The read state is ``isRead``; ``readAt`` is used only
   when ``isRead`` is absent, because marking a row unread keeps its
   ``readAt``.

Each id gets one record with a ``status`` from :data:`NOTIFICATION_STATUSES`.
The methods raise :class:`~meshbook.errors.MeshbookError` only when NOTHING
was sent (a bad argument, no credential, the lookup itself failed); once a
PATCH may have gone out, every outcome is in the returned records.
"""

from __future__ import annotations

import datetime as _dt
import uuid as _uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .._times import aware
from ..client import _data, _item_list
from ..errors import MeshbookError
from ..models import ItemList
from ._base import Namespace, as_dict

#: The server's tab groups and the kinds each one filters to (mirrors
#: ``GROUP_KINDS`` in the server's ``app/routers/notifications.py``; a test
#: compares the two when a server checkout sits beside this one). The
#: server IGNORES a group it does not know and filters nothing, so an
#: unknown group is refused here, before the wire (critique C-03).
NOTIFICATION_GROUP_KINDS: dict[str, tuple[str, ...]] = {
    "mentions": ("Mention",),
    "invites": (
        "CalendarInvite",
        "CalendarChange",
        "MeshInvitationReceived",
        "MeshInvitationAccepted",
        "MeshInvitationDeclined",
    ),
    "assignments": (
        "TaskAssigned",
        "TaskDueSoon",
        "TaskOverdue",
        "LeadStageChange",
        "LeadAssigned",
        "LeadWon",
        "LeadLost",
        "ProjectAssigned",
        "ProjectStatusChange",
    ),
    "mesh": ("MeshRoleChanged", "MeshMemberRemoved", "MeshActivated", "MeshDormantReverted"),
    "system": (
        "SystemAnnouncement",
        "StorageThresholdWarning",
        "StorageQuotaExceeded",
        "ActivityAlert",
    ),
}

#: The valid ``group=`` values.
NOTIFICATION_GROUPS: tuple[str, ...] = tuple(NOTIFICATION_GROUP_KINDS)

#: The per-id status vocabulary every write method reports, with what each
#: status means. Stable: a status is never renamed or reused within a minor
#: release. meshbook-cli and meshbook-mcp expose these strings verbatim.
NOTIFICATION_STATUSES: dict[str, str] = {
    "applied": (
        "the row is now in the requested state, with evidence: the server echoed the row, "
        "or a re-read under the same mesh shows it"
    ),
    "already": "the row was already in the requested state; nothing was sent",
    "not_found": (
        "no row with this id is visible to you (every mesh searched to the end, or the "
        "server answered 404 notification_not_found): dismissed, not yours, or never "
        "existed; nothing changed"
    ),
    "not_in_mesh": (
        "you named a mesh and the row is not visible in it (that mesh was searched to the "
        "end); nothing was sent and no other mesh was tried"
    ),
    "unconfirmed": (
        "the client cannot show the row is in the requested state: the PATCH was answered "
        "but the evidence is missing or contrary, or (sent=false) the row was not found "
        "within the search limits"
    ),
    "maybe_applied": (
        "the PATCH may have reached the server but the answer was lost and a re-read could "
        "not settle it: check before retrying"
    ),
    "not_attempted": (
        "not sent: an earlier id hit an error that would fail every id (credential rejected, "
        "read-only, out of scope, server too old, repeated network failures)"
    ),
    "error": (
        "the server refused this id (see code and hint), or the PATCH failed and a re-read "
        "shows the row unchanged; nothing changed"
    ),
}

#: The statuses that mean "the row is in the state you asked for".
SETTLED_STATUSES: frozenset[str] = frozenset({"applied", "already"})

#: ``read_all(cross_mesh=)`` values.
CROSS_MESH_MODES: tuple[str, ...] = ("include", "exclude", "only")

_UTC = _dt.timezone.utc

# What each write sends. Exactly these keys, with JSON booleans: the server
# acts on key PRESENCE and coerces with bool(), so {"isRead": None} would
# mark a row UNREAD and {"dismiss": "false"} would dismiss it (C-21).
_BODIES: dict[str, dict[str, bool]] = {
    "mark_read": {"isRead": True},
    "mark_unread": {"isRead": False},
    "dismiss": {"dismiss": True},
}
_TARGET_READ: dict[str, bool] = {"mark_read": True, "mark_unread": False}
_STATE_WORD = {True: "read", False: "unread"}

# Error codes / hints after which every further PATCH would fail the same way.
_FATAL_CODES = frozenset(
    {
        "token_readonly",
        "token_out_of_scope",
        "not_authenticated",
        "no_agent_key",
        "mint_failed",
        "redirect_refused",
        "insecure_base",
    }
)
_FATAL_HINTS = frozenset({"server_too_old", "reauthenticate", "provide_credentials"})

_MISSING: Any = object()  # "the re-read never reached this row"


# ─── pure helpers (also the seams the mutation tests patch) ──────────────


def _row_id(row: Mapping[str, Any]) -> str | None:
    rid = row.get("id")
    return str(rid).lower() if rid else None


def _is_read(row: Mapping[str, Any]) -> bool | None:
    """The row's read state: ``isRead``, else ``readAt`` (critique C-02).

    ``isRead`` is authoritative. The server keeps ``readAt`` when a row is
    marked UNREAD, so a stale ``readAt`` must never mean "read" when
    ``isRead`` says otherwise. None when the row carries neither.
    """
    flag = row.get("isRead")
    if isinstance(flag, bool):
        return flag
    if "readAt" in row:
        return row.get("readAt") is not None
    return None


def _created(row: Mapping[str, Any]) -> _dt.datetime | None:
    raw = row.get("createdAt")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    return when


def _sort_key(row: Mapping[str, Any]) -> tuple[_dt.datetime, str] | None:
    """Where the row sits in a listing: the server orders by createdAt
    DESC, id DESC, so a LARGER key is nearer the top."""
    when, rid = _created(row), _row_id(row)
    if when is None or rid is None:
        return None
    return (when, rid)


def _normalise_ids(ids: Any, cap: int) -> list[str]:
    """Notification ids → canonical lower-case UUID strings, deduplicated,
    in the order given. A ``str`` (or ``uuid.UUID``) is ONE id, never a
    sequence of characters (critique C-05). Anything that is not a UUID,
    an empty batch and a batch over *cap* are refused before the wire
    (``bad_argument``), so a typo never reaches the server (C-20)."""
    if isinstance(ids, (str, _uuid.UUID)):
        items: list[Any] = [ids]
    elif ids is None or isinstance(ids, (bytes, bytearray, Mapping)):
        raise MeshbookError(
            "bad_argument",
            f"ids must be one notification id or a list of them, not {type(ids).__name__}. "
            "Nothing was sent.",
        )
    else:
        try:
            items = list(ids)
        except TypeError:
            raise MeshbookError(
                "bad_argument",
                f"ids must be one notification id or a list of them, not "
                f"{type(ids).__name__}. Nothing was sent.",
            ) from None
    out: list[str] = []
    seen: set[str] = set()
    bad: list[str] = []
    for item in items:
        if isinstance(item, _uuid.UUID):
            text = str(item)
        elif isinstance(item, str):
            text = item.strip()
        else:
            bad.append(repr(item))
            continue
        try:
            canon = str(_uuid.UUID(text))
        except ValueError:
            bad.append(repr(item))
            continue
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    if bad:
        shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise MeshbookError(
            "bad_argument",
            f"Not notification ids (UUIDs): {shown}. Nothing was sent.",
        )
    if not out:
        raise MeshbookError("bad_argument", "No notification ids given. Nothing was sent.")
    if len(out) > cap:
        raise MeshbookError(
            "bad_argument",
            f"{len(out)} notification ids in one call; the limit is {cap}. Split the batch. "
            "Nothing was sent.",
        )
    return out


def _check_group(group: Any) -> str | None:
    """A ``group=`` value → its canonical key, or ``bad_argument``.

    The server ignores a group it does not know and then filters NOTHING:
    ``read-all?group=invitations`` marks every visible unread row (C-03)."""
    if group is None:
        return None
    if isinstance(group, str):
        key = group.strip().lower()
        if key in NOTIFICATION_GROUP_KINDS:
            return key
    raise MeshbookError(
        "bad_argument",
        f"Unknown notification group {group!r}. Use one of: {', '.join(NOTIFICATION_GROUPS)} "
        "(or none, for every group). Nothing was sent.",
    )


def _check_kinds(kinds: Any) -> frozenset[str] | None:
    if kinds is None:
        return None
    items = [kinds] if isinstance(kinds, str) else kinds
    try:
        values = list(items)
    except TypeError:
        values = [items]
    if not values or not all(isinstance(k, str) and k.strip() for k in values):
        raise MeshbookError(
            "bad_argument",
            "kinds= must be a notification kind (e.g. 'Mention') or a non-empty list of them.",
        )
    return frozenset(k.strip() for k in values)


def _cutoff(older_than: Any) -> _dt.datetime | None:
    if older_than is None:
        return None
    if isinstance(older_than, _dt.timedelta):
        if older_than < _dt.timedelta(0):
            raise MeshbookError("bad_argument", "older_than= must not be a negative timedelta.")
        return _dt.datetime.now(_UTC) - older_than
    return aware(older_than, "older_than")


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise MeshbookError(
            "bad_argument", f"{name}= must be True or False, got {value!r}. Nothing was sent."
        )
    return value


def _judge(data: Any, action: str) -> str:
    """What a 2xx PATCH answer proves: ``"applied"`` (a fixed server echoed
    the row in the requested state), ``"mismatch"`` (it echoed the row in
    another state) or ``"unknown"`` (an older server's ``{"id",
    "updated": true}``, which it sends whether or not a row matched; the
    caller must re-read). ``updated`` is never evidence."""
    if not isinstance(data, Mapping):
        return "unknown"
    if action == "dismiss":
        if "dismissedAt" not in data:
            return "unknown"
        return "applied" if data.get("dismissedAt") else "mismatch"
    if not isinstance(data.get("isRead"), bool):
        return "unknown"
    return "applied" if data["isRead"] is _TARGET_READ[action] else "mismatch"


def _is_fatal(err: MeshbookError) -> bool:
    """True when every further PATCH would fail the same way."""
    if err.status == 401 or err.code in _FATAL_CODES or err.hint in _FATAL_HINTS:
        return True
    # Refused locally before the wire (no credential, a broken config, a
    # missing agent key): the next call would be refused the same way.
    return err.status == 0 and err.code != "network_error"


def _outcome_unknown(err: MeshbookError) -> bool:
    """The PATCH may have been applied although no 2xx came back."""
    return err.code == "network_error" or err.status >= 500


def _compact(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _row_id(row),
        "kind": row.get("kind"),
        "title": row.get("title") or row.get("summary"),
        "createdAt": row.get("createdAt"),
        "meshId": row.get("meshId"),
        "isCrossMesh": bool(row.get("isCrossMesh")),
    }


@dataclass
class _Found:
    """A row located in a listing."""

    row: dict[str, Any]
    via: str | None  # the X-Active-Mesh-Id it was listed through (None: no header)
    offset: int  # page offset in the listing below
    group: str | None  # the listing's group filter (None: every group)
    exact: bool = True  # False: offset is from a filtered scan; re-read from 0

    @property
    def id(self) -> str:
        return _row_id(self.row) or ""


def _record(
    nid: str,
    status: str,
    *,
    sent: bool,
    mesh: str | None,
    row: Mapping[str, Any] | None = None,
    message: str,
    err: MeshbookError | None = None,
) -> dict[str, Any]:
    return {
        "id": nid,
        "status": status,
        "sent": sent,
        "mesh": mesh,
        "kind": row.get("kind") if row else None,
        "title": (row.get("title") or row.get("summary")) if row else None,
        "message": message,
        "code": err.code if err is not None else None,
        "hint": (err.hint if err is not None else None)
        or ("check_before_retry" if status == "maybe_applied" else None),
    }


class Notifications(Namespace):
    """``client.notifications``: list, count, mark read/unread, dismiss.

    See the module docstring for how a write proves itself, and
    :data:`NOTIFICATION_STATUSES` for what each per-id status means.
    """

    #: Valid ``group=`` values (also :data:`NOTIFICATION_GROUPS`).
    GROUPS = NOTIFICATION_GROUPS
    #: Per-id statuses and their meanings (also :data:`NOTIFICATION_STATUSES`).
    STATUSES = NOTIFICATION_STATUSES

    #: Rows per lookup page: the server's maximum ``limit``.
    PAGE_SIZE = 200
    #: Most ids one write call accepts.
    MAX_IDS = 200
    #: Lookup pages per mesh listing (5,000 rows), and for the whole lookup.
    MAX_PAGES_PER_MESH = 25
    MAX_PAGES = 100
    #: Pages a post-PATCH re-read may fetch per mesh.
    MAX_VERIFY_PAGES = 30
    #: Unread pages ``read_all()`` scans (2,000 rows) before it says ``truncated``.
    READ_ALL_MAX_PAGES = 10
    #: Consecutive transport failures (network, 429, 5xx) that end a batch.
    MAX_TRANSIENT = 3

    # ── read ─────────────────────────────────────────────────────────

    def list(
        self,
        *,
        unread_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
        group: str | None = None,
        kind: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/notifications: notification dicts (``id``, ``kind``,
        ``title``, ``summary``, ``isRead``, ``readAt``, ``meshId``,
        ``isCrossMesh``, ``createdAt`` …), newest first.

        Shows the rows of ONE mesh (``mesh=``, else the client's) plus every
        cross-mesh row; with no mesh at all, only the cross-mesh rows. The
        server sends 50 rows unless ``limit=`` says otherwise (200 at most).
        ``unread_only`` filters server-side; ``group`` is a tab key
        (:data:`NOTIFICATION_GROUPS`; anything else raises ``bad_argument``
        since 0.4.3, because the server silently ignores an unknown group).
        Read state is ``isRead``: ``readAt`` survives a mark-unread.
        """
        params: dict[str, Any] = {
            "unreadOnly": True if unread_only else None,
            "group": _check_group(group),
            "kind": kind,
        }
        return self._c._list(
            "notifications.list", params=params, limit=limit, offset=offset, mesh=mesh
        )

    def summary(self, *, mesh: str | None = None, all_meshes: bool = False) -> dict[str, Any]:
        """GET /api/notifications/summary: UNREAD counts per tab
        (``mentions``, ``invites``, ``assignments``, ``mesh``, ``system``)
        plus ``unread``, ``unreadCrossMesh`` and ``total`` (read rows too).

        The server counts ONE mesh plus every cross-mesh row, so this adds
        ``unreadInMesh`` (``unread - unreadCrossMesh``), ``meshId`` (the
        mesh counted; None means cross-mesh rows only) and ``allMeshes``.
        Do NOT add up ``unread`` over several meshes: each cross-mesh row
        would be counted once per mesh (critique C-16).

        ``all_meshes=True`` gives that total correctly: one call with no
        mesh (the cross-mesh rows alone) plus one per mesh you belong to,
        every count = cross-mesh once + each mesh's own rows. It adds
        ``meshes`` (per mesh: ``meshId``, ``name``, ``unreadInMesh``,
        ``totalInMesh``), ``skipped`` (meshes whose count was refused, e.g.
        outside a scoped token; their rows are missing) and ``complete``.
        A snapshot over several calls: a row that changes meanwhile can be
        off by one.
        """
        if _strict_bool(all_meshes, "all_meshes"):
            if mesh is not None:
                raise MeshbookError("bad_argument", "Pass mesh= or all_meshes=True, not both.")
            return self._summary_all()
        mid = self._c.resolve_mesh(mesh)
        return self._summary_one(mid)

    def _summary_raw(self, mid: str | None) -> dict[str, Any]:
        return as_dict(self._c._call("notifications.summary", mesh=mid, send_mesh=mid is not None))

    def _summary_one(self, mid: str | None) -> dict[str, Any]:
        out = dict(self._summary_raw(mid))
        unread, cross = out.get("unread"), out.get("unreadCrossMesh")
        out["unreadInMesh"] = unread - cross if _is_count(unread) and _is_count(cross) else None
        out["meshId"] = mid
        out["allMeshes"] = False
        return out

    def _summary_all(self) -> dict[str, Any]:
        cross = self._summary_raw(None)  # no header: the cross-mesh rows only
        keys = [k for k, v in cross.items() if _is_count(v)]
        totals: dict[str, int] = {k: int(cross[k]) for k in keys}
        meshes_out: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for m in self._c._my_meshes():
            try:
                per = self._summary_raw(m.id)
            except MeshbookError as e:
                if e.status == 403:
                    skipped.append({"meshId": m.id, "name": m.name, "code": e.code})
                    continue
                raise
            own = {k: int(per[k]) - int(cross[k]) for k in keys if _is_count(per.get(k))}
            for k, v in own.items():
                if k != "unreadCrossMesh":
                    totals[k] += v
            meshes_out.append(
                {
                    "meshId": m.id,
                    "name": m.name,
                    "unreadInMesh": own.get("unread"),
                    "totalInMesh": own.get("total"),
                }
            )
        out: dict[str, Any] = dict(totals)
        if "unread" in totals and "unreadCrossMesh" in totals:
            out["unreadInMesh"] = totals["unread"] - totals["unreadCrossMesh"]
        out.update(
            {
                "meshId": None,
                "allMeshes": True,
                "meshes": meshes_out,
                "skipped": skipped,
                "complete": not skipped,
            }
        )
        return out

    # ── write ────────────────────────────────────────────────────────

    def mark_read(self, ids: str | Iterable[str], *, mesh: str | None = None) -> dict[str, Any]:
        """Mark notifications READ for your account (every bench and web
        session signed in as you sees it; nobody else's inbox changes).

        *ids*: one id or a list (a ``str`` is one id). Deduplicated,
        lower-cased and checked locally: a non-UUID, an empty list or more
        than :attr:`MAX_IDS` raises ``bad_argument`` with nothing sent.

        *mesh*: where the rows are. Named (id or name): strict, a row not
        visible in that mesh is ``not_in_mesh`` and no other mesh is tried.
        Omitted: your current mesh first, then each of your other meshes.

        Returns ``{"action", "ok", "counts", "results", "server",
        "searched"}``: ``results`` holds one record per id, in the order
        given, ``{"id", "status", "sent", "mesh", "kind", "title",
        "message", "code", "hint"}`` (``status`` from
        :data:`NOTIFICATION_STATUSES`; ``mesh`` is the mesh the PATCH went
        under); ``ok`` is True when every status is ``applied`` or
        ``already``; ``server`` is ``"fixed"`` (it echoed rows), ``"old"``
        (it answered ``updated``, so the result was checked by re-reading),
        ``"mixed"`` or ``"unknown"`` (nothing sent); ``searched`` lists each
        mesh listing read (``mesh``, ``rows``, ``exhausted``, ``skipped``).
        """
        return self._write("mark_read", ids, mesh)

    def mark_unread(self, ids: str | Iterable[str], *, mesh: str | None = None) -> dict[str, Any]:
        """Mark notifications UNREAD for your account. Arguments and return
        exactly as :meth:`mark_read`. (The server keeps ``readAt`` on a row
        marked unread; ``isRead`` is what counts.)"""
        return self._write("mark_unread", ids, mesh)

    def dismiss(
        self, ids: str | Iterable[str], *, mesh: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """DISMISS notifications: hide them from your account's inbox for
        good. Irreversible: no route un-dismisses, and a dismissed row
        vanishes from every listing, count and client. To record that you
        have HANDLED something, use :meth:`mark_read` instead. (The web
        page's x button marks read; it does not dismiss.)

        Refused (``bad_argument``, hint ``confirm_irreversible``, nothing
        sent) unless ``confirm=True``. Arguments and return as
        :meth:`mark_read`. A row that is already dismissed is invisible, so
        it reports ``not_found``, never ``already``. Against a server that
        does not echo rows, ``applied`` means the row is no longer visible
        where it was found, under the same mesh (the only change that hides
        a visible row is dismissal).
        """
        if confirm is not True:
            raise MeshbookError(
                "bad_argument",
                "dismiss() hides notifications permanently and cannot be undone; pass "
                "confirm=True to proceed (use mark_read() to record that you handled them). "
                "Nothing was sent.",
                hint="confirm_irreversible",
            )
        return self._write("dismiss", ids, mesh)

    def read_all(
        self,
        *,
        group: str | None = None,
        mesh: str | None = None,
        apply: bool = False,
        older_than: Any = None,
        kinds: str | Iterable[str] | None = None,
        cross_mesh: str = "include",
    ) -> dict[str, Any]:
        """Mark every matching UNREAD notification read. A DRY RUN unless
        ``apply=True``: it lists, filters and returns exactly the ids it
        would mark, and sends nothing.

        Built from the listing plus one PATCH per row, never the server's
        blanket ``POST /read-all`` (which marks rows you have not seen,
        ignores an unknown group and returns no ids; critique C-04).

        * ``mesh``: the mesh to clear (id or name), else the client's. With
          no mesh at all this raises ``no_active_mesh``, unless
          ``cross_mesh="only"``.
        * ``group``: one of :data:`NOTIFICATION_GROUPS` (else
          ``bad_argument``). ``kinds``: a kind or list of kinds.
        * ``older_than``: a ``timedelta`` (age) or an aware datetime / ISO
          string (cutoff); only rows created before it are marked.
        * ``cross_mesh``: ``"include"`` (default: the mesh's rows and the
          cross-mesh rows, as the web page does), ``"exclude"`` or
          ``"only"``. Cross-mesh rows show in EVERY mesh, so a loop over
          your meshes with "include" clears them on its first pass.

        Returns ``{"dry_run", "mesh", "filters", "ids", "count",
        "matched", "remaining", "rows", "by_kind", "cross_mesh_rows",
        "scanned", "truncated", "outcome"}``. ``ids`` (newest first) are
        the rows this call marks or would mark, at most :attr:`MAX_IDS`;
        ``remaining`` matched beyond that cap (run it again);
        ``truncated`` means the scan stopped at
        :attr:`READ_ALL_MAX_PAGES` pages and more unread rows may exist.
        ``outcome`` is None in a dry run, else the :meth:`mark_read` result
        for ``ids``. Rows arriving between a dry run and ``apply=True`` are
        included by the second call; to mark exactly the rows you reviewed,
        pass the dry run's ``ids`` to :meth:`mark_read`.
        """
        g = _check_group(group)
        kind_set = _check_kinds(kinds)
        cutoff = _cutoff(older_than)
        if cross_mesh not in CROSS_MESH_MODES:
            raise MeshbookError(
                "bad_argument",
                f"cross_mesh= must be one of {', '.join(CROSS_MESH_MODES)}, got {cross_mesh!r}.",
            )
        do_apply = _strict_bool(apply, "apply")
        mid = self._c.resolve_mesh(mesh)
        if mid is None and cross_mesh != "only":
            raise MeshbookError(
                "no_active_mesh",
                "read_all() needs a mesh: without one the server sees only cross-mesh rows. "
                "Pass mesh= (id or name), set a default mesh, or pass cross_mesh='only' to "
                "clear cross-mesh rows alone. Nothing was sent.",
            )
        group_kinds = set(NOTIFICATION_GROUP_KINDS[g]) if g else None
        plan: list[_Found] = []
        seen: set[str] = set()
        scanned = 0
        truncated = True
        offset = 0
        for _ in range(self.READ_ALL_MAX_PAGES):
            page = self._page(mid, offset, unread_only=True, group=g)
            scanned += len(page)
            for row in page:
                nid = _row_id(row)
                if nid is None or nid in seen:
                    continue
                seen.add(nid)
                if _is_read(row) is not False:
                    continue
                kind = row.get("kind")
                # Belt and braces: if a server ever ignored the group, its
                # extra kinds are left alone rather than marked.
                if group_kinds is not None and kind not in group_kinds:
                    continue
                if kind_set is not None and kind not in kind_set:
                    continue
                crossing = bool(row.get("isCrossMesh"))
                if (cross_mesh == "exclude" and crossing) or (
                    cross_mesh == "only" and not crossing
                ):
                    continue
                if cutoff is not None:
                    when = _created(row)
                    if when is None or not when < cutoff:
                        continue
                plan.append(_Found(row=row, via=mid, offset=offset, group=g, exact=False))
            if len(page) < self.PAGE_SIZE:
                truncated = False
                break
            offset += self.PAGE_SIZE
        chosen = plan[: self.MAX_IDS]
        by_kind: dict[str, int] = {}
        for f in chosen:
            k = str(f.row.get("kind"))
            by_kind[k] = by_kind.get(k, 0) + 1
        out: dict[str, Any] = {
            "dry_run": not do_apply,
            "mesh": mid,
            "filters": {
                "group": g,
                "kinds": sorted(kind_set) if kind_set is not None else None,
                "older_than": cutoff.isoformat() if cutoff is not None else None,
                "cross_mesh": cross_mesh,
            },
            "ids": [f.id for f in chosen],
            "count": len(chosen),
            "matched": len(plan),
            "remaining": len(plan) - len(chosen),
            "rows": [_compact(f.row) for f in chosen],
            "by_kind": by_kind,
            "cross_mesh_rows": sum(1 for f in chosen if f.row.get("isCrossMesh")),
            "scanned": scanned,
            "truncated": truncated,
            "outcome": None,
        }
        if do_apply and chosen:
            results: dict[str, dict[str, Any]] = {}
            server = self._apply("mark_read", chosen, results)
            searched = [{"mesh": mid, "rows": scanned, "exhausted": not truncated, "skipped": None}]
            out["outcome"] = self._report(
                "mark_read", [f.id for f in chosen], results, server, searched
            )
        elif do_apply:
            out["outcome"] = self._report("mark_read", [], {}, set(), [])
        return out

    # ── the engine ───────────────────────────────────────────────────

    def _page(
        self,
        via: str | None,
        offset: int,
        *,
        unread_only: bool = False,
        group: str | None = None,
    ) -> Sequence[dict[str, Any]]:
        """One listing page under exactly *via* (None: no header at all)."""
        params: dict[str, Any] = {
            "limit": self.PAGE_SIZE,
            "offset": offset,
            "unreadOnly": True if unread_only else None,
            "group": group,
        }
        payload = self._c._call(
            "notifications.list", params=params, mesh=via, send_mesh=via is not None
        )
        rows = _item_list(payload, limit=self.PAGE_SIZE, offset=offset)
        return [r for r in rows if isinstance(r, dict)]

    def _patch_mesh(self, found: _Found) -> str | None:
        """The mesh a PATCH goes under: the one the row was listed through."""
        return found.via

    def _write(self, action: str, ids: Any, mesh: str | None) -> dict[str, Any]:
        wanted = _normalise_ids(ids, self.MAX_IDS)
        strict = mesh is not None
        mid = self._c.resolve_mesh(mesh) if strict else None
        found, searched, settled = self._locate(wanted, strict=strict, mesh_id=mid)
        results: dict[str, dict[str, Any]] = {}
        to_send: list[_Found] = []
        target = _TARGET_READ.get(action)
        for nid in wanted:
            f = found.get(nid)
            if f is None:
                results[nid] = self._unlocated(nid, strict=strict, mesh_id=mid, settled=settled)
            elif target is not None and _is_read(f.row) is target:
                results[nid] = _record(
                    nid,
                    "already",
                    sent=False,
                    mesh=f.via,
                    row=f.row,
                    message=f"Already {_STATE_WORD[target]}; nothing sent.",
                )
            else:
                to_send.append(f)
        server = self._apply(action, to_send, results)
        return self._report(action, wanted, results, server, searched)

    def _locate(
        self, wanted: Sequence[str], *, strict: bool, mesh_id: str | None
    ) -> tuple[dict[str, _Found], Sequence[dict[str, Any]], bool]:
        """Find each id by paging listings from offset 0 (read and unread
        rows). Returns (found, searched, settled): *settled* is True when
        every listing tried was read to its end and none was refused, so an
        id still missing is really not visible."""
        pending = set(wanted)
        found: dict[str, _Found] = {}
        searched: list[dict[str, Any]] = []
        budget = [self.MAX_PAGES]

        def scan(via: str | None, primary: bool) -> bool:
            offset = rows = pages = 0
            exhausted = False
            skipped: str | None = None
            while pending and pages < self.MAX_PAGES_PER_MESH and budget[0] > 0:
                try:
                    page = self._page(via, offset)
                except MeshbookError as e:
                    # Hunting through your OTHER meshes: one a scoped token
                    # cannot reach is skipped, not fatal (C-09).
                    if not primary and e.status == 403:
                        skipped = e.code
                        break
                    raise
                pages += 1
                budget[0] -= 1
                rows += len(page)
                for row in page:
                    nid = _row_id(row)
                    if nid is not None and nid in pending:
                        found[nid] = _Found(row=row, via=via, offset=offset, group=None)
                        pending.discard(nid)
                if len(page) < self.PAGE_SIZE:
                    exhausted = True
                    break
                offset += self.PAGE_SIZE
            searched.append({"mesh": via, "rows": rows, "exhausted": exhausted, "skipped": skipped})
            return exhausted

        if strict:
            settled = scan(mesh_id, True)
            return found, searched, settled or not pending
        ambient = self._c.active_mesh_id
        settled = scan(ambient, True)
        if pending:
            for m in self._c._my_meshes():
                if not pending:
                    break
                if ambient and m.id.lower() == ambient.lower():
                    continue
                settled = scan(m.id, False) and settled
        return found, searched, settled or not pending

    def _unlocated(
        self, nid: str, *, strict: bool, mesh_id: str | None, settled: bool
    ) -> dict[str, Any]:
        if not settled:
            return _record(
                nid,
                "unconfirmed",
                sent=False,
                mesh=mesh_id if strict else None,
                message=(
                    "Not found within the search limits (a listing was cut short or refused); "
                    "nothing sent. Name its mesh to search there to the end."
                ),
            )
        if strict:
            return _record(
                nid,
                "not_in_mesh",
                sent=False,
                mesh=mesh_id,
                message=(
                    "Not visible in that mesh (it belongs to another mesh, is dismissed, or is "
                    "not yours); nothing sent, no other mesh tried."
                ),
            )
        return _record(
            nid,
            "not_found",
            sent=False,
            mesh=None,
            message=(
                "Not visible in any of your meshes (dismissed, not yours, or never existed); "
                "nothing sent."
            ),
        )

    def _apply(
        self, action: str, items: Sequence[_Found], results: dict[str, dict[str, Any]]
    ) -> set[str]:
        """PATCH each row; record what the answer proves; re-read the rest.
        Returns the server styles seen ({"fixed", "old"})."""
        server: set[str] = set()
        recheck: list[tuple[_Found, MeshbookError | None]] = []
        abort: MeshbookError | None = None
        transient = 0
        for f in items:
            nid = f.id
            via = self._patch_mesh(f)
            if abort is not None:
                results[nid] = _record(
                    nid,
                    "not_attempted",
                    sent=False,
                    mesh=via,
                    row=f.row,
                    message=f"Not sent: an earlier id failed with {abort.code}.",
                )
                continue
            try:
                payload = self._c._call(
                    "notifications.mark",
                    path={"notification_id": nid},
                    body=dict(_BODIES[action]),
                    mesh=via,
                    send_mesh=via is not None,
                )
            except MeshbookError as e:
                if e.status == 404 and e.code == "notification_not_found":
                    server.add("fixed")
                    transient = 0
                    results[nid] = _record(
                        nid,
                        "not_found",
                        sent=True,
                        mesh=via,
                        row=f.row,
                        message="The server found no such row under this mesh; nothing changed.",
                        err=e,
                    )
                    continue
                if _is_fatal(e):
                    abort = e
                    results[nid] = _record(
                        nid,
                        "error",
                        sent=True,
                        mesh=via,
                        row=f.row,
                        message=f"Refused ({e.code}): {e.message}",
                        err=e,
                    )
                    continue
                transient = transient + 1 if (_outcome_unknown(e) or e.status == 429) else 0
                if _outcome_unknown(e):
                    recheck.append((f, e))
                else:
                    results[nid] = _record(
                        nid,
                        "error",
                        sent=True,
                        mesh=via,
                        row=f.row,
                        message=f"Refused ({e.code}): {e.message}",
                        err=e,
                    )
                if transient >= self.MAX_TRANSIENT:
                    abort = e
                continue
            transient = 0
            verdict = _judge(_data(payload), action)
            if verdict == "unknown":
                server.add("old")
                recheck.append((f, None))
                continue
            server.add("fixed")
            if verdict == "applied":
                echo = _data(payload)
                hidden = (
                    action != "dismiss" and isinstance(echo, Mapping) and echo.get("dismissedAt")
                )
                results[nid] = _record(
                    nid,
                    "applied",
                    sent=True,
                    mesh=via,
                    row=f.row,
                    message=(
                        "Applied; the server echoed the row (it is dismissed, so no list shows it)."
                        if hidden
                        else "Applied; the server echoed the row."
                    ),
                )
            else:
                results[nid] = _record(
                    nid,
                    "unconfirmed",
                    sent=True,
                    mesh=via,
                    row=f.row,
                    message="The server echoed the row in a different state than requested.",
                )
        if recheck:
            self._recheck(action, recheck, results)
        return server

    def _recheck(
        self,
        action: str,
        items: Sequence[tuple[_Found, MeshbookError | None]],
        results: dict[str, dict[str, Any]],
    ) -> None:
        """Settle every answer that proves nothing by re-reading listings
        under the mesh each PATCH went under."""
        groups: dict[tuple[str | None, str | None, bool], list[tuple[_Found, MeshbookError | None]]]
        groups = {}
        for f, err in items:
            groups.setdefault((self._patch_mesh(f), f.group, f.exact), []).append((f, err))
        for (via, group, exact), batch in groups.items():
            now = self._seek(via, [f for f, _ in batch], group=group, exact=exact)
            for f, err in batch:
                results[f.id] = self._settle(action, f, via, now.get(f.id, _MISSING), err)

    def _settle(
        self,
        action: str,
        f: _Found,
        via: str | None,
        current: Any,
        err: MeshbookError | None,
    ) -> dict[str, Any]:
        nid = f.id

        def rec(status: str, message: str, e: MeshbookError | None = None) -> dict[str, Any]:
            return _record(nid, status, sent=True, mesh=via, row=f.row, message=message, err=e)

        lost = err is not None
        if action == "dismiss":
            if current is _MISSING:
                if lost:
                    return rec(
                        "maybe_applied",
                        "The answer was lost and a re-read could not reach the row.",
                        err,
                    )
                return rec(
                    "unconfirmed", "A re-read could not reach the row to confirm the dismiss."
                )
            if current is None:
                return rec(
                    "applied",
                    "Confirmed: the row is no longer visible where it was, under the same mesh.",
                )
            if lost:
                return rec("error", "The PATCH failed and the row is still visible.", err)
            return rec("unconfirmed", "Still visible after the PATCH: the dismiss did not land.")
        want = _TARGET_READ[action]
        word = _STATE_WORD[want]
        if current is _MISSING:
            if lost:
                return rec(
                    "maybe_applied",
                    "The answer was lost and a re-read could not reach the row.",
                    err,
                )
            return rec("unconfirmed", "A re-read could not reach the row to confirm it.")
        if current is None:
            if lost:
                return rec(
                    "maybe_applied", "The answer was lost and the row is no longer visible.", err
                )
            return rec(
                "unconfirmed",
                "The row is no longer visible where it was (dismissed elsewhere?); "
                "its state is unknown.",
            )
        if _is_read(current) is want:
            return rec("applied", f"Confirmed {word} by re-reading the listing.")
        if lost:
            return rec("error", f"The PATCH failed and the row is still not {word}.", err)
        return rec(
            "unconfirmed",
            f"Still not {word} after the PATCH (the server reported success).",
        )

    def _seek(
        self, via: str | None, targets: Sequence[_Found], *, group: str | None, exact: bool
    ) -> dict[str, dict[str, Any] | None]:
        """Re-read each target under *via*. id → the row as it is now, or
        None when the listing provably no longer holds it (its sort place
        was read and it was not there). An id left out could not be settled.

        Starts at the page each row was found on (or at 0 when that offset
        came from a filtered scan) and follows the row a page up or down
        if rows were added or removed above it meanwhile."""
        cache: dict[int, Sequence[dict[str, Any]]] = {}
        budget = [self.MAX_VERIFY_PAGES]
        out: dict[str, dict[str, Any] | None] = {}

        def page(off: int) -> Sequence[dict[str, Any]] | None:
            if off not in cache:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                try:
                    cache[off] = self._page(via, off, group=group)
                except MeshbookError:
                    budget[0] = 0  # stop reading; what is settled so far stands
                    return None
            return cache[off]

        size = self.PAGE_SIZE
        for f in sorted(targets, key=lambda t: t.offset if t.exact else 0):
            nid, key = f.id, _sort_key(f.row)
            off = f.offset if exact else 0
            visited: set[int] = set()
            while True:
                visited.add(off)
                rows = page(off)
                if rows is None:
                    break  # out of budget: unsettled
                hit = next((r for r in rows if _row_id(r) == nid), None)
                if hit is not None:
                    out[nid] = hit
                    break
                if key is None:
                    break  # cannot place it: unsettled
                nxt: int | None
                if not rows:
                    nxt = off - size if off > 0 else None
                else:
                    first, last = _sort_key(rows[0]), _sort_key(rows[-1])
                    if first is None or last is None:
                        break  # cannot place it: unsettled
                    if key > first and off > 0:
                        nxt = off - size
                    elif key < last and len(rows) >= size:
                        nxt = off + size
                    else:
                        nxt = None
                if nxt is None or nxt in visited:
                    out[nid] = None  # its place was read and it is not there
                    break
                off = nxt
        return out

    def _report(
        self,
        action: str,
        order: Sequence[str],
        results: dict[str, dict[str, Any]],
        server: set[str],
        searched: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        ordered = [results[nid] for nid in order]
        counts: dict[str, int] = {}
        for r in ordered:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        style = "mixed" if len(server) > 1 else next(iter(server)) if server else "unknown"
        return {
            "action": action,
            "ok": all(r["status"] in SETTLED_STATUSES for r in ordered),
            "counts": counts,
            "results": ordered,
            "server": style,
            "searched": list(searched),
        }


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "CROSS_MESH_MODES",
    "NOTIFICATION_GROUPS",
    "NOTIFICATION_GROUP_KINDS",
    "NOTIFICATION_STATUSES",
    "SETTLED_STATUSES",
    "Notifications",
]
