"""The endpoint registry: every (method, path) this SDK can put on the wire.

Every namespace method reaches the network through
``MeshbookClient._call(<key>, ...)``, which looks the key up HERE. That is
the whole point of the registry: the contract check
(``scripts/contract_check.py``) reads this table instead of guessing at
call sites, so it cannot miss a call, and a test asserts that every request
the test-suite sends matches an entry (and that no module under
``meshbook/`` calls the raw ``request()`` escape hatch itself).

Fields:

* ``method`` / ``path`` — the path template uses ``{name}`` placeholders;
  values are percent-encoded when rendered (an emoji in a reaction path,
  for instance).
* ``mesh`` — what ``X-Active-Mesh-Id`` carries:

  - ``"active"`` — the resolved mesh (per-call ``mesh=`` > ``Client(mesh=)``
    > ``MESHBOOK_MESH`` > config ``active_mesh_id``), when there is one;
  - ``"path"``   — the ``{mesh_id}`` in the path itself (the route acts on
    that mesh, so the header must agree with it);
  - ``"none"``   — never sent. Account-level calls (``/api/me``, the mesh
    list, the agent lane) must not inherit a mesh they did not ask for: the
    server's mesh-scope gate answers 403 ``token_out_of_scope`` on ANY route
    when the header names a mesh outside a scoped token's reach.

* ``auth`` — False for the two unauthenticated routes (health, §97 register).
* ``pending`` — set (to the reason) when the route is part of today's
  contract but not yet in the published OpenAPI spec. The contract check
  fails on these unless run with ``--allow-pending``, and lists them either
  way.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

MeshMode = Literal["active", "path", "none"]


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    mesh: MeshMode = "active"
    auth: bool = True
    pending: str | None = None

    def render(self, params: dict[str, Any] | None = None) -> str:
        """Fill the path template, percent-encoding every value."""
        if not params:
            if "{" in self.path:
                raise KeyError(f"{self.path} needs path parameters")
            return self.path
        encoded = {k: urllib.parse.quote(str(v), safe="") for k, v in params.items()}
        return self.path.format(**encoded)


def _ep(
    method: str,
    path: str,
    mesh: MeshMode = "active",
    auth: bool = True,
    pending: str | None = None,
) -> Endpoint:
    return Endpoint(method, path, mesh, auth, pending)


_CAL = "/api/calendar-events"
_CAL_PENDING = "calendar contract 2026-10-19 (server packet; not in prod OpenAPI yet)"

ENDPOINTS: dict[str, Endpoint] = {
    # ── infrastructure / identity ──
    "health": _ep("GET", "/api/health", mesh="none", auth=False),
    "me": _ep("GET", "/api/me", mesh="none"),
    # ── agent lane (§97 / §86 / §93 / §99) ──
    "agent.register": _ep("POST", "/api/register/agent", mesh="none", auth=False),
    "agent.enroll": _ep("POST", "/api/me/agent-credentials", mesh="none"),
    "agent.status": _ep("GET", "/api/me/agent-credentials", mesh="none"),
    "agent.revoke": _ep("DELETE", "/api/me/agent-credentials", mesh="none"),
    # ── meshes + membership ──
    "meshes.list": _ep("GET", "/api/meshes", mesh="none"),
    "meshes.activate": _ep("POST", "/api/meshes/active", mesh="none"),
    "meshes.pending": _ep("GET", "/api/meshes/my-pending", mesh="none"),
    "meshes.detail": _ep("GET", "/api/meshes/{mesh_id}/detail", mesh="path"),
    "meshes.invite": _ep("POST", "/api/meshes/{mesh_id}/invite", mesh="path"),
    # Responding to an invitation: you are not a member yet, so the target
    # mesh cannot be the active one; send none.
    "meshes.respond": _ep("POST", "/api/meshes/{mesh_id}/respond", mesh="none"),
    "meshes.set_role": _ep("POST", "/api/meshes/{mesh_id}/set-role", mesh="path"),
    "meshes.remove": _ep("POST", "/api/meshes/{mesh_id}/remove", mesh="path"),
    "users.list": _ep("GET", "/api/users"),
    # ── exports (§58) ──
    "exports.start": _ep("POST", "/api/meshes/{mesh_id}/export", mesh="path"),
    "exports.list": _ep("GET", "/api/meshes/{mesh_id}/exports", mesh="path"),
    "exports.download": _ep("GET", "/api/mesh-exports/{export_id}/download"),
    # ── contacts / companies ──
    "contacts.list": _ep("GET", "/api/contacts"),
    "contacts.create": _ep("POST", "/api/contacts"),
    "contacts.get": _ep("GET", "/api/contacts/{contact_id}"),
    "contacts.update": _ep("PATCH", "/api/contacts/{contact_id}"),
    "contacts.delete": _ep("DELETE", "/api/contacts/{contact_id}"),
    "companies.list": _ep("GET", "/api/companies"),
    "companies.create": _ep("POST", "/api/companies"),
    "companies.get": _ep("GET", "/api/companies/{company_id}"),
    "companies.update": _ep("PATCH", "/api/companies/{company_id}"),
    "companies.delete": _ep("DELETE", "/api/companies/{company_id}"),
    "companies.contacts": _ep("GET", "/api/companies/{company_id}/contacts"),
    # ── leads + pipelines ──
    "pipelines.list": _ep("GET", "/api/pipelines"),
    "leads.list": _ep("GET", "/api/leads"),
    "leads.create": _ep("POST", "/api/leads"),
    "leads.get": _ep("GET", "/api/leads/{lead_id}"),
    "leads.update": _ep("PATCH", "/api/leads/{lead_id}"),
    "leads.delete": _ep("DELETE", "/api/leads/{lead_id}"),
    "leads.move_stage": _ep("POST", "/api/leads/{lead_id}/move-stage"),
    # ── tasks / time / projects ──
    "tasks.list": _ep("GET", "/api/tasks"),
    "tasks.create": _ep("POST", "/api/tasks"),
    "tasks.get": _ep("GET", "/api/tasks/{task_id}"),
    "tasks.update": _ep("PATCH", "/api/tasks/{task_id}"),
    "tasks.delete": _ep("DELETE", "/api/tasks/{task_id}"),
    "time_logs.create": _ep("POST", "/api/task-time-logs"),
    "projects.list": _ep("GET", "/api/projects"),
    "projects.create": _ep("POST", "/api/projects"),
    "projects.get": _ep("GET", "/api/projects/{project_id}"),
    "projects.update": _ep("PATCH", "/api/projects/{project_id}"),
    "projects.delete": _ep("DELETE", "/api/projects/{project_id}"),
    "projects.add_member": _ep(
        "POST",
        "/api/projects/{project_id}/members",
        pending="§102.6 project members: the server has the project_members table but no "
        "route yet (server packet)",
    ),
    # ── tags (entity tags; used by tasks/contacts tags=) ──
    "tags.list": _ep("GET", "/api/tags"),
    "tags.entity_list": _ep("GET", "/api/entities/{entity_type}/{entity_id}/tags"),
    "tags.attach": _ep("POST", "/api/entities/{entity_type}/{entity_id}/tags"),
    "tags.detach": _ep("DELETE", "/api/entities/{entity_type}/{entity_id}/tags/{tag_id}"),
    # ── chat (entity threads, reactions, attachments, search) ──
    "chat.list": _ep("GET", "/api/entities/{entity_type}/{entity_id}/chat"),
    "chat.post": _ep("POST", "/api/entities/{entity_type}/{entity_id}/chat"),
    "chat.message": _ep("GET", "/api/chat-messages/{message_id}"),
    "chat.attach": _ep("POST", "/api/chat-messages/{message_id}/attachments/json"),
    "chat.download": _ep("GET", "/api/chat-attachments/{attachment_id}/download"),
    "chat.react": _ep("POST", "/api/chat-messages/{message_id}/reactions"),
    "chat.unreact": _ep("DELETE", "/api/chat-messages/{message_id}/reactions/{emoji}"),
    "chat.search": _ep("GET", "/api/chat/search"),
    # ── channels + DMs ──
    "channels.list": _ep("GET", "/api/meshes/{mesh_id}/channels", mesh="path"),
    "channels.create": _ep("POST", "/api/meshes/{mesh_id}/channels", mesh="path"),
    "channels.get": _ep("GET", "/api/channels/{channel_id}"),
    "channels.messages": _ep("GET", "/api/channels/{channel_id}/messages"),
    "channels.post": _ep("POST", "/api/channels/{channel_id}/messages"),
    "channels.members": _ep("GET", "/api/channels/{channel_id}/members"),
    "channels.add_member": _ep("POST", "/api/channels/{channel_id}/members"),
    "channels.remove_member": _ep("DELETE", "/api/channels/{channel_id}/members/{user_id}"),
    "dms.list": _ep("GET", "/api/meshes/{mesh_id}/dms", mesh="path"),
    "dms.open": _ep("POST", "/api/meshes/{mesh_id}/dms/with/{user_id}", mesh="path"),
    # ── notifications ──
    "notifications.list": _ep("GET", "/api/notifications"),
    # ── entity attachments (§78) ──
    "files.attach": _ep("POST", "/api/entities/{entity_type}/{entity_id}/attachments/json"),
    "files.link": _ep("POST", "/api/entities/{entity_type}/{entity_id}/attachments/links"),
    "files.list": _ep("GET", "/api/entities/{entity_type}/{entity_id}/attachments"),
    "files.download": _ep("GET", "/api/entity-attachments/{attachment_id}/download"),
    "files.delete": _ep("DELETE", "/api/entity-attachments/{attachment_id}"),
    # ── custom fields / saved views ──
    "custom_fields.list": _ep("GET", "/api/custom-fields"),
    "saved_views.list": _ep("GET", "/api/saved-views"),
    "saved_views.create": _ep("POST", "/api/saved-views"),
    # ── calendar events (CALENDAR-CONTRACT.md, 2026-10-19) ──
    "events.list": _ep("GET", _CAL),
    "events.create": _ep("POST", _CAL),
    "events.get": _ep("GET", _CAL + "/{event_id}"),
    "events.update": _ep("PATCH", _CAL + "/{event_id}", pending=_CAL_PENDING),
    "events.delete": _ep("DELETE", _CAL + "/{event_id}"),
    "events.set_recurrence": _ep("PUT", _CAL + "/{event_id}/recurrence", pending=_CAL_PENDING),
    "events.clear_recurrence": _ep(
        "DELETE", _CAL + "/{event_id}/recurrence", pending=_CAL_PENDING
    ),
    "events.attendees": _ep("GET", _CAL + "/{event_id}/attendees", pending=_CAL_PENDING),
    "events.add_attendee": _ep("POST", _CAL + "/{event_id}/attendees", pending=_CAL_PENDING),
    "events.update_attendee": _ep(
        "PATCH", _CAL + "/{event_id}/attendees/{attendee_id}", pending=_CAL_PENDING
    ),
    "events.remove_attendee": _ep(
        "DELETE", _CAL + "/{event_id}/attendees/{attendee_id}", pending=_CAL_PENDING
    ),
}


def calls() -> list[tuple[str, str]]:
    """Every distinct (METHOD, path-template) pair, sorted — the contract check's input."""
    return sorted({(e.method, e.path) for e in ENDPOINTS.values()})


def pending() -> dict[tuple[str, str], str]:
    """(METHOD, path-template) → reason, for routes not yet in the published spec."""
    return {(e.method, e.path): e.pending for e in ENDPOINTS.values() if e.pending}


__all__ = ["ENDPOINTS", "Endpoint", "calls", "pending"]
