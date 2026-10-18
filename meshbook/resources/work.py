"""Tasks, time logs, projects and entity tags."""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping
from typing import Any

from .._times import due_fields, iso_date
from ..client import _is_uuid
from ..errors import MeshbookError
from ..models import ItemList
from ._base import UNSET, Namespace, as_dict, merge_fields, put, put_if

#: Task priorities, lowest first (the server's own vocabulary).
PRIORITIES = ("Low", "Normal", "High", "Urgent")
#: Task statuses (the server's own vocabulary).
STATUSES = ("NotStarted", "InProgress", "Blocked", "Review", "Done", "Cancelled")
_TERMINAL = ("Done", "Cancelled")
#: Project roles the project_members table accepts.
PROJECT_ROLES = ("Owner", "Member", "Watcher", "Contributor", "Reviewer")


def _tag_key(name: str) -> str:
    """The server's tag-name normalisation (tags.py attach_tag)."""
    return name.strip().lower().replace(" ", "-")


def _due_key(task: Mapping[str, Any]) -> str:
    return str(task.get("dueAt") or task.get("dueDate") or "")


def _sort_tasks(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Client-side sort of one page (the server orders by status, sort order)."""
    desc = sort.startswith("-")
    key = sort.lstrip("-+").lower()
    if key == "due":
        dated = [t for t in rows if _due_key(t)]
        undated = [t for t in rows if not _due_key(t)]
        dated.sort(key=_due_key, reverse=desc)
        return dated + undated  # undated last either way
    if key == "priority":
        rank = {p.lower(): i for i, p in enumerate(PRIORITIES)}
        return sorted(
            rows, key=lambda t: rank.get(str(t.get("priority") or "").lower(), -1), reverse=not desc
        )  # "priority" = most urgent first; "-priority" = least urgent first
    if key == "status":
        srank = {s.lower(): i for i, s in enumerate(STATUSES)}
        return sorted(
            rows, key=lambda t: srank.get(str(t.get("status") or "").lower(), 99), reverse=desc
        )
    if key == "title":
        return sorted(rows, key=lambda t: str(t.get("title") or "").casefold(), reverse=desc)
    if key in ("created", "createdat"):
        return sorted(rows, key=lambda t: str(t.get("createdAt") or ""), reverse=desc)
    raise MeshbookError(
        "bad_argument", f"sort must be one of due, priority, status, title, created (got {sort!r})."
    )


class Tags(Namespace):
    """``client.tags`` — the mesh's tag dictionary and tags on entities."""

    def list(
        self, *, limit: int | None = None, offset: int | None = None, mesh: str | None = None
    ) -> ItemList[dict[str, Any]]:
        """GET /api/tags — the mesh's tag dictionary."""
        return self._c._list("tags.list", limit=limit, offset=offset, mesh=mesh)

    def of(self, entity_type: str, entity_id: str, *, mesh: str | None = None) -> ItemList[dict[str, Any]]:
        """GET /api/entities/{type}/{id}/tags — the tags on one entity."""
        return self._c._list(
            "tags.entity_list",
            path={"entity_type": entity_type, "entity_id": entity_id},
            mesh=mesh,
        )

    def attach(
        self,
        entity_type: str,
        entity_id: str,
        name: str | None = None,
        *,
        tag_id: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """Attach a tag by NAME (created in the dictionary if new) or by id."""
        if (name is None) == (tag_id is None):
            raise MeshbookError("bad_argument", "Pass exactly one of name= or tag_id=.")
        body = {"tagId": tag_id} if tag_id else {"name": self._require_nonempty(name, "tag name")}
        return as_dict(
            self._c._call(
                "tags.attach",
                path={"entity_type": entity_type, "entity_id": entity_id},
                body=body,
                mesh=mesh,
            )
        )

    def detach(
        self, entity_type: str, entity_id: str, tag_id: str, *, mesh: str | None = None
    ) -> dict[str, Any]:
        """Remove one tag (by tag id) from an entity."""
        return as_dict(
            self._c._call(
                "tags.detach",
                path={"entity_type": entity_type, "entity_id": entity_id, "tag_id": tag_id},
                mesh=mesh,
            )
        )

    def add(
        self,
        entity_type: str,
        entity_id: str,
        names: Iterable[str],
        *,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """Attach each tag in *names* (by name; new names are created in the
        mesh's dictionary). Never removes a tag — this is what ``tags=`` on
        the create/update methods does, matching @meshbook/sdk. Returns the
        entity's tags afterwards."""
        clean = [n.strip() for n in names if n and n.strip()]
        for n in clean:
            self.attach(entity_type, entity_id, n, mesh=mesh)
        return self.of(entity_type, entity_id, mesh=mesh)

    def set(
        self,
        entity_type: str,
        entity_id: str,
        names: Iterable[str],
        *,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """Make the entity's tags exactly *names* (attach missing, DETACH
        extra) — the explicit, destructive form; ``tags=`` on update methods
        only ever adds.

        Names are compared the way the server stores them (trimmed,
        lower-case, spaces as hyphens). Returns the entity's tags afterwards.
        """
        wanted: dict[str, str] = {}
        for n in names:
            if n and n.strip():
                wanted.setdefault(_tag_key(n), n.strip())
        current = self.of(entity_type, entity_id, mesh=mesh)
        have = {str(t.get("name") or ""): t for t in current}
        for key, original in wanted.items():
            if key not in have:
                self.attach(entity_type, entity_id, original, mesh=mesh)
        for key, row in have.items():
            if key not in wanted and row.get("id"):
                self.detach(entity_type, entity_id, str(row["id"]), mesh=mesh)
        return self.of(entity_type, entity_id, mesh=mesh)


class Tasks(Namespace):
    """``client.tasks`` — project tasks (and time logged against them)."""

    def list(
        self,
        limit: int | None = 50,
        project_id: str | None = None,
        assignee_id: str | None = None,
        status: str | None = None,
        *,
        offset: int | None = None,
        project: str | None = None,
        assignee: str | None = None,
        search: str | None = None,
        sort: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/tasks. Rows carry ``priority``, ``dueDate`` / ``dueAt``.

        * Status values: NotStarted / InProgress / Blocked / Review / Done /
          Cancelled.
        * ``project`` / ``assignee`` take a name or id (resolved);
          ``project_id`` / ``assignee_id`` are ids sent as-is.
        * ``sort``: ``due`` (undated last), ``priority`` (most urgent
          first), ``status``, ``title`` or ``created``; prefix ``-`` to
          reverse. Applied to the page the server returned — the server
          itself orders by status and board position.
        """
        if project is not None:
            project_id = str(self._c.projects.resolve(project, mesh=mesh)["id"])
        if assignee is not None:
            assignee_id = self._user_id(assignee, mesh=mesh)
        params = {
            "projectId": project_id,
            "assigneeId": assignee_id,
            "status": status,
            "search": search,
        }
        rows = self._c._list("tasks.list", params=params, limit=limit, offset=offset, mesh=mesh)
        if sort:
            ordered = _sort_tasks(list(rows), sort)
            return ItemList(
                ordered, total=rows.total, limit=rows.limit, offset=rows.offset, meta=rows.meta
            )
        return rows

    def list_mine(
        self,
        status: str | None = None,
        limit: int | None = 50,
        *,
        open_only: bool = False,
        offset: int | None = None,
        sort: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """Tasks assigned to you (own id via /api/me, cached per client).

        ``open_only=True`` drops Done/Cancelled rows from the page (the
        MCP's ``list_my_tasks``); ``.total`` stays the server's figure.
        """
        rows = self.list(
            limit=limit,
            assignee_id=self._c._self_user_id(),
            status=status,
            offset=offset,
            sort=sort,
            mesh=mesh,
        )
        if open_only:
            return ItemList(
                (t for t in rows if t.get("status") not in _TERMINAL),
                total=rows.total,
                limit=rows.limit,
                offset=rows.offset,
                meta=dict(rows.meta, open_only=True),
            )
        return rows

    def get(self, task_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/tasks/{id} (with assignees, contacts and subtasks)."""
        return as_dict(self._c._call("tasks.get", path={"task_id": task_id}, mesh=mesh))

    def create(
        self,
        project_id: str | None,
        title: str,
        priority: str | None = None,
        description: str | None = None,
        *,
        project: str | None = None,
        due: Any = None,
        assignee: str | None = None,
        tags: Iterable[str] | None = None,
        parent: str | None = None,
        status: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/tasks.

        * ``project_id`` is an id sent as-is; or pass ``project_id=None,
          project="Name"`` to resolve by name or code.
        * ``due``: a ``date`` → ``dueDate``; a timezone-aware ``datetime``
          → ``dueAt`` (naive datetimes are refused).
        * ``assignee``: UUID, @username or display name.
        * ``parent``: the parent task's id (makes this a subtask).
        * ``tags``: attached by name after the task exists; the result
          then carries ``tags``.
        """
        if project is not None:
            project_id = str(self._c.projects.resolve(project, mesh=mesh)["id"])
        if not project_id:
            raise MeshbookError("bad_argument", "A task needs a project (project_id= or project=).")
        body: dict[str, Any] = {"projectId": project_id, "title": title}
        put_if(body, "priority", priority)
        put_if(body, "description", description)
        put_if(body, "status", status)
        if due is not None:
            body.update(due_fields(due))
        if assignee:
            body["assigneeUserId"] = self._user_id(assignee, mesh=mesh)
        put_if(body, "parentTaskId", parent)
        created = as_dict(self._c._call("tasks.create", body=body, mesh=mesh))
        if tags is not None and created.get("id"):
            created["tags"] = list(self._c.tags.add("task", str(created["id"]), tags, mesh=mesh))
        return created

    def update(
        self,
        task_id: str,
        *,
        title: Any = UNSET,
        priority: Any = UNSET,
        description: Any = UNSET,
        due: Any = UNSET,
        assignee: Any = UNSET,
        status: Any = UNSET,
        parent: Any = UNSET,
        tags: Iterable[str] | None = None,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/tasks/{id} — edit in place, keeping the task's history
        (§102.4: the old complete-and-recreate lost it).

        Only the arguments you pass are sent; ``None`` clears (``due=None``
        clears both dueDate and dueAt; ``assignee=None`` unassigns).
        ``tags`` are ADDED (never removed; use ``client.tags.set`` or
        ``client.tags.detach`` to take tags off).
        """
        body: dict[str, Any] = {}
        put(body, "title", title)
        put(body, "priority", priority)
        put(body, "description", description)
        put(body, "status", status)
        if due is not UNSET:
            body.update(due_fields(due))
            if due is not None:
                # Moving between a date and a date-time: clear the other one.
                body.setdefault("dueAt" if "dueDate" in body else "dueDate", None)
        if assignee is not UNSET:
            body["assigneeUserId"] = None if assignee is None else self._user_id(assignee, mesh=mesh)
        put(body, "parentTaskId", parent)
        merge_fields(body, fields)
        if not body and tags is None:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        out: dict[str, Any]
        if body:
            out = as_dict(self._c._call("tasks.update", path={"task_id": task_id}, body=body, mesh=mesh))
        else:
            out = self.get(task_id, mesh=mesh)
        if tags is not None:
            out["tags"] = list(self._c.tags.add("task", task_id, tags, mesh=mesh))
        return out

    def done(self, task_id: str, status: str = "Done", *, mesh: str | None = None) -> dict[str, Any]:
        """PATCH /api/tasks/{id} → a terminal status (default Done; e.g.
        ``status='Cancelled'``)."""
        return as_dict(
            self._c._call("tasks.update", path={"task_id": task_id}, body={"status": status}, mesh=mesh)
        )

    def delete(self, task_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/tasks/{id} (soft delete; restorable)."""
        return as_dict(self._c._call("tasks.delete", path={"task_id": task_id}, mesh=mesh))

    def log_time(
        self,
        task_id: str,
        hours: float,
        date: Any = None,
        note: str | None = None,
        *,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/task-time-logs. ``hours`` in (0, 24]; ``date`` is a
        ``date`` or ``'YYYY-MM-DD'`` (default: today in UTC, as the CLI did)."""
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0 < hours <= 24:
            raise MeshbookError("bad_argument", f"hours must be > 0 and <= 24, got {hours!r}.")
        logged = (
            iso_date(date, "date")
            if date is not None
            else _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        )
        body: dict[str, Any] = {"taskId": task_id, "hours": hours, "loggedForDate": logged}
        put_if(body, "note", note)
        return as_dict(self._c._call("time_logs.create", body=body, mesh=mesh))


class Projects(Namespace):
    """``client.projects`` — task-container projects."""

    def list(
        self,
        limit: int | None = 50,
        portfolio_id: str | None = None,
        status: str | None = None,
        *,
        offset: int | None = None,
        search: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/projects. ``status`` is the server's ``statusFilter``:
        ``active`` (Planning/Active/OnHold) or ``completed``
        (Complete/Cancelled)."""
        params = {"portfolioId": portfolio_id, "statusFilter": status, "search": search}
        return self._c._list("projects.list", params=params, limit=limit, offset=offset, mesh=mesh)

    def get(self, project_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/projects/{id} (with ``members`` and ``tasks``)."""
        return as_dict(self._c._call("projects.get", path={"project_id": project_id}, mesh=mesh))

    def create(
        self,
        name: str,
        code: str | None = None,
        description: str | None = None,
        portfolio_id: str | None = None,
        *,
        owner: str | None = None,
        status: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/projects. ``owner``: UUID, @username or display name."""
        body: dict[str, Any] = {"name": name}
        put_if(body, "code", code)
        put_if(body, "description", description)
        put_if(body, "portfolioId", portfolio_id)
        put_if(body, "status", status)
        if owner:
            body["ownerUserId"] = self._user_id(owner, mesh=mesh)
        return as_dict(self._c._call("projects.create", body=body, mesh=mesh))

    def update(
        self,
        project_id: str,
        *,
        name: Any = UNSET,
        code: Any = UNSET,
        description: Any = UNSET,
        status: Any = UNSET,
        owner: Any = UNSET,
        portfolio_id: Any = UNSET,
        start_date: Any = UNSET,
        target_end_date: Any = UNSET,
        progress_pct: Any = UNSET,
        custom_fields: Any = UNSET,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/projects/{id} — only the arguments you pass are sent.
        Statuses: Planning / Active / OnHold / Complete / Cancelled."""
        body: dict[str, Any] = {}
        put(body, "name", name)
        put(body, "code", code)
        put(body, "description", description)
        put(body, "status", status)
        if owner is not UNSET:
            body["ownerUserId"] = None if owner is None else self._user_id(owner, mesh=mesh)
        put(body, "portfolioId", portfolio_id)
        if start_date is not UNSET:
            body["startDate"] = None if start_date is None else iso_date(start_date, "start_date")
        if target_end_date is not UNSET:
            body["targetEndDate"] = (
                None if target_end_date is None else iso_date(target_end_date, "target_end_date")
            )
        put(body, "progressPct", progress_pct)
        put(body, "customFields", custom_fields)
        merge_fields(body, fields)
        if not body:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        return as_dict(
            self._c._call("projects.update", path={"project_id": project_id}, body=body, mesh=mesh)
        )

    def archive(
        self, project_id: str, status: str = "Complete", *, mesh: str | None = None
    ) -> dict[str, Any]:
        """Archive a project: move it to a closed status (default
        ``Complete``; pass ``"Cancelled"`` for abandoned work).

        meshbook has no ``Archived`` status: the closed statuses are what
        ``list(status="completed")`` returns and the active board hides. This
        is NOT a delete — :meth:`delete` soft-deletes into the recycle bin.
        """
        if status not in ("Complete", "Cancelled"):
            raise MeshbookError(
                "bad_argument", "archive() takes status='Complete' or status='Cancelled'."
            )
        return self.update(project_id, status=status, mesh=mesh)

    def add_member(
        self, project_id: str, user: str, role: str = "Member", *, mesh: str | None = None
    ) -> dict[str, Any]:
        """Add a mesh member to a project (§102.6).

        ``role``: Owner / Member / Watcher / Contributor / Reviewer.

        PENDING SERVER ROUTE: ``POST /api/projects/{id}/members`` is built
        to the 2026-10-19 contract; the server has the ``project_members``
        table but, as of this release, no route — the call returns 404/405
        until the server packet ships it.
        """
        if role not in PROJECT_ROLES:
            raise MeshbookError("bad_argument", f"role must be one of {', '.join(PROJECT_ROLES)}.")
        uid = self._user_id(user, mesh=mesh)
        return as_dict(
            self._c._call(
                "projects.add_member",
                path={"project_id": project_id},
                body={"userId": uid, "roleOnProject": role},
                mesh=mesh,
            )
        )

    def delete(self, project_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/projects/{id} (soft delete)."""
        return as_dict(self._c._call("projects.delete", path={"project_id": project_id}, mesh=mesh))

    def resolve(self, project: str, *, mesh: str | None = None) -> dict[str, Any]:
        """A project ref (UUID, exact name, or code) → a project row.
        ``project_not_found`` / ``project_ambiguous`` otherwise."""
        target = self._require_nonempty(project, "project")
        if _is_uuid(target):
            return {"id": target}
        rows = self.list(search=target, limit=100, mesh=mesh)
        folded = target.casefold()
        hits = [
            p
            for p in rows
            if (p.get("name") or "").casefold() == folded
            or (p.get("code") or "").casefold() == folded
        ]
        if len(hits) == 1:
            return dict(hits[0])
        if not hits:
            raise MeshbookError("project_not_found", f"No project named {target!r}.", 404)
        listing = "; ".join(f"{p.get('name')} ({p.get('id')})" for p in hits)
        raise MeshbookError(
            "project_ambiguous", f"Several projects match {target!r}: {listing}. Pass the id."
        )
