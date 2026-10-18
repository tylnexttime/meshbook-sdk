"""Meshes, membership and the user directory."""
from __future__ import annotations

from typing import Any

from .. import config as _config
from ..client import _is_uuid
from ..errors import MeshbookError
from ..models import ItemList, Mesh
from ._base import Namespace, as_dict, no_offset


class Meshes(Namespace):
    """``client.meshes`` — which meshes you are in, switching, and membership."""

    def list_mine(self, *, limit: int | None = None, offset: int | None = None) -> ItemList[Mesh]:
        """GET /api/meshes — every mesh you're a member of.

        Sends no ``X-Active-Mesh-Id``: listing your meshes must keep working
        when the current active mesh is stale or outside your token's scope
        (that is exactly when you need the list).
        """
        rows = self._c._list("meshes.list", limit=limit, offset=offset)
        out: ItemList[Mesh] = ItemList(
            (Mesh.from_api(m) for m in rows),
            total=rows.total,
            limit=rows.limit,
            offset=rows.offset,
            meta=rows.meta,
        )
        if limit is None and offset is None:
            self._c._meshes_cache = list(out)
        return out

    def resolve(self, mesh: str) -> Mesh:
        """A mesh you are a member of, by UUID or name (case-insensitive).

        Raises ``mesh_not_found`` / ``mesh_ambiguous``. Unlike per-call
        ``mesh=`` (where a UUID is sent as-is and the server judges it),
        this always checks the UUID against your mesh list.
        """
        return self._c._mesh_lookup(mesh, require_membership=True)

    def use(self, mesh: str, verify: bool = True, persist: bool = False) -> Mesh:
        """Make *mesh* (UUID or name) this client's active mesh.

        Verifies with the server first (``POST /api/meshes/active``): the
        server gates requests on the TOKEN's mesh scope, not on membership,
        so a member holding a narrower token would otherwise get a Mesh back
        and then have every later call fail ``token_out_of_scope``. A
        refusal changes nothing locally. (Wren, 2026-08-20.)

        ``persist=True`` also writes ``active_mesh_id`` to the config file —
        what ``mesh meshes use`` and the MCP's ``set_active_mesh`` do, and
        the ONLY path in the SDK that persists a mesh (per-call ``mesh=``,
        ``Client(mesh=)`` and ``MESHBOOK_MESH`` never do). Default False, so
        a library caller never rewrites a shared config file by accident.

        ``verify=False`` skips the server round-trip (offline use).
        """
        self._c._meshes_cache = None  # membership may have changed: refetch once
        found = self.resolve(mesh)
        if verify:
            self._c._call("meshes.activate", body={"meshId": found.id})
        self._c.active_mesh_id = found.id
        self._c.mesh_source = "argument"
        if persist:
            _config.save_config({"active_mesh_id": found.id}, self._c.config_path)
        return found

    def members(self, mesh: str | None = None) -> dict[str, Any]:
        """Who is in a mesh: members, plus pending invites and join requests.

        ``GET /api/meshes/<id>/detail`` (gated on membership). Returns
        ``{"mesh", "meshId", "count", "members", "pendingInvites",
        "pendingRequests"}`` — an explicit count, so "found zero" and
        "silently did nothing" cannot look the same to a caller.

        Since 0.4.0 passing ``mesh`` no longer switches the client's active
        mesh as a side effect (0.3 did, via ``use(verify=False)``).
        """
        mesh_id = self._c._require_mesh(mesh)
        data = as_dict(self._c._call("meshes.detail", path={"mesh_id": mesh_id}))
        members = data.get("members") or []
        return {
            "mesh": (data.get("mesh") or {}).get("name"),
            "meshId": mesh_id,
            "count": len(members),
            "members": members,
            "pendingInvites": data.get("pendingInvites") or [],
            "pendingRequests": data.get("pendingRequests") or [],
        }

    def pending_invitations(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> ItemList[dict[str, Any]]:
        """GET /api/meshes/my-pending — meshes you've been invited to but
        haven't answered (each row's ``id`` is what :meth:`respond` takes)."""
        return self._c._list("meshes.pending", limit=limit, offset=offset)

    def invite(
        self, username: str, role: str | None = None, *, mesh: str | None = None
    ) -> dict[str, Any]:
        """Invite a user (by username, leading '@' stripped) to a mesh.

        ``role``: ``admin`` | ``member`` | ``reader``; omit for the mesh's
        default invite role.
        """
        mesh_id = self._c._require_mesh(mesh)
        body: dict[str, Any] = {"username": self._require_nonempty(username, "username").lstrip("@")}
        if role:
            body["role"] = role
        return as_dict(self._c._call("meshes.invite", path={"mesh_id": mesh_id}, body=body))

    def respond(self, mesh: str, accept: bool = True) -> dict[str, Any]:
        """Accept (or, ``accept=False``, decline) a pending invitation.

        *mesh* is the invited mesh's UUID, or its NAME as listed by
        :meth:`pending_invitations` (it is not one of your meshes yet, so it
        is matched against your pending invitations, not your mesh list).
        """
        target = self._require_nonempty(mesh, "mesh")
        if not _is_uuid(target):
            folded = target.casefold()
            hits = [
                m for m in self.pending_invitations() if (m.get("name") or "").casefold() == folded
            ]
            if not hits:
                raise MeshbookError(
                    "mesh_not_found", f"No pending invitation to a mesh named {target!r}.", 404
                )
            if len(hits) > 1:
                raise MeshbookError(
                    "mesh_ambiguous",
                    f"{len(hits)} pending invitations are to meshes named {target!r}; pass the id.",
                )
            target = str(hits[0]["id"])
        action = "accept" if accept else "decline"
        return as_dict(
            self._c._call("meshes.respond", path={"mesh_id": target}, body={"action": action})
        )

    def set_role(self, user: str, role: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Change a member's role (user: UUID, @username or display name)."""
        mesh_id = self._c._require_mesh(mesh)
        uid = self._user_id(user, mesh=mesh_id)
        return as_dict(
            self._c._call(
                "meshes.set_role", path={"mesh_id": mesh_id}, body={"userId": uid, "role": role}
            )
        )

    def remove_member(self, user: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Remove a member from a mesh (admin)."""
        mesh_id = self._c._require_mesh(mesh)
        uid = self._user_id(user, mesh=mesh_id)
        return as_dict(
            self._c._call("meshes.remove", path={"mesh_id": mesh_id}, body={"userId": uid})
        )

    def leave(self, *, mesh: str | None = None) -> dict[str, Any]:
        """Leave a mesh yourself (§49: an Account Manager cannot leave a mesh
        on their own subscription — the server's 403 is raised as-is)."""
        mesh_id = self._c._require_mesh(mesh)
        uid = self._c._self_user_id()
        if not uid:
            raise MeshbookError("no_user", "Could not resolve your own user id from /api/me.")
        return as_dict(
            self._c._call("meshes.remove", path={"mesh_id": mesh_id}, body={"userId": uid})
        )


class Users(Namespace):
    """``client.users`` — the active mesh's member directory."""

    def list(
        self,
        q: str | None = None,
        *,
        lite: bool = False,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/users — members of the mesh (``q`` is a substring filter
        over display name / username; ``lite`` trims each row). The route
        takes ``limit`` only: ``offset=`` raises ``bad_argument``."""
        no_offset(offset, "users.list()", cursor="limit=")
        params: dict[str, Any] = {"q": q, "lite": True if lite else None}
        return self._c._list("users.list", params=params, limit=limit, mesh=mesh)

    def resolve(self, user: str, *, mesh: str | None = None) -> dict[str, Any]:
        """A user ref → a user row.

        A UUID comes back as ``{"id": uuid}`` without a lookup (the server
        validates it where it is used). Otherwise ``@username`` / username
        is matched first (case-insensitive; usernames are unique), then the
        display name — where more than one match raises ``user_ambiguous``
        instead of picking one. ``user_not_found`` when nothing matches.
        """
        target = self._require_nonempty(user, "user").lstrip("@").strip()
        if _is_uuid(target):
            return {"id": target}
        rows = self.list(q=target, lite=True, limit=500, mesh=mesh)
        folded = target.casefold()
        for u in rows:
            if (u.get("username") or "").casefold() == folded:
                return dict(u)
        hits = [
            u
            for u in rows
            if (u.get("displayName") or u.get("display_name") or "").casefold() == folded
        ]
        if len(hits) == 1:
            return dict(hits[0])
        if not hits:
            raise MeshbookError("user_not_found", f"No mesh member matching {user!r}.", 404)
        listing = "; ".join(f"@{u.get('username')} ({u.get('id')})" for u in hits)
        raise MeshbookError(
            "user_ambiguous", f"{len(hits)} members are named {target!r}: {listing}. Pass @username."
        )

