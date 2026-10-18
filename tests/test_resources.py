"""The resource surface the CLI and MCP port onto, and the §102 additions.

Every test pins the exact wire (method, path, query, body), because the
point of these methods is that meshbook-cli and meshbook-mcp can delete
their own HTTP code and call them instead.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from meshbook import UNSET, MeshbookClient, MeshbookError
from tests._fakes import A1, C1, MESH, MESH2, P1, T1, U1, U2, install, meshes_payload, ok, ok_list


@pytest.fixture
def client(tmp_path):
    return MeshbookClient(token="mb_token_test", mesh=MESH, config_path=tmp_path / "none")


USERS = ok_list([
    {"id": U1, "username": "wren", "displayName": "Wren"},
    {"id": U2, "username": "alder", "display_name": "Alder"},
])


# ─── users ─────────────────────────────────────────────────────────────


def test_users_resolve_username_then_display_name(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", USERS)
    assert client.users.resolve("@WREN")["id"] == U1
    assert r.query() == {"limit": "500", "q": "WREN", "lite": "true"}
    assert client.users.resolve("alder")["id"] == U2  # username
    assert client.users.resolve(U2) == {"id": U2}


def test_users_resolve_ambiguous_display_name(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", ok_list([
        {"id": U1, "username": "a1", "displayName": "Sam"},
        {"id": U2, "username": "a2", "displayName": "sam"}]))
    with pytest.raises(MeshbookError) as ei:
        client.users.resolve("Sam")
    assert ei.value.code == "user_ambiguous"
    with pytest.raises(MeshbookError) as ei:
        client.users.resolve("nobody")
    assert ei.value.code == "user_not_found"


# ─── tasks (§102.3-5) ──────────────────────────────────────────────────


def test_tasks_create_with_due_assignee_tags_parent(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", USERS)
    r.add("POST", "/api/tasks", ok({"id": T1, "title": "Ship"}))
    r.add("POST", f"/api/entities/task/{T1}/tags", ok({"attached": True}))
    r.add("GET", f"/api/entities/task/{T1}/tags", ok([{"id": "g1", "name": "release"}]))
    out = client.tasks.create(P1, "Ship", "High", "the thing", due=dt.date(2026, 10, 31),
                              assignee="wren", tags=["release", " Q4 board "], parent=A1)
    create = r.calls("POST", "/api/tasks")[0]
    assert r.body(create) == {"projectId": P1, "title": "Ship", "priority": "High",
                              "description": "the thing", "dueDate": "2026-10-31",
                              "assigneeUserId": U1, "parentTaskId": A1}
    tag_posts = r.calls("POST", f"/api/entities/task/{T1}/tags")
    assert [r.body(q) for q in tag_posts] == [{"name": "release"}, {"name": "Q4 board"}]
    assert out["tags"] == [{"id": "g1", "name": "release"}]


def test_tasks_create_by_project_name(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/projects", ok_list([{"id": P1, "name": "Books", "code": "BK"},
                                           {"id": A1, "name": "Books 2"}]))
    r.add("POST", "/api/tasks", ok({"id": T1}))
    client.tasks.create(None, "Draft", project="bk")
    assert r.body()["projectId"] == P1


def test_tasks_update_fields_due_switch_and_unassign(client, monkeypatch):
    r = install(monkeypatch)
    r.add("PATCH", f"/api/tasks/{T1}", ok({"id": T1}))
    aware = dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.timezone.utc)
    client.tasks.update(T1, title="New", priority="Urgent", status="InProgress",
                        due=aware, assignee=None, description=None)
    assert r.body() == {"title": "New", "priority": "Urgent", "description": None,
                        "status": "InProgress", "dueAt": "2026-11-01T17:00:00+00:00",
                        "dueDate": None, "assigneeUserId": None}
    client.tasks.update(T1, due=None)
    assert r.body() == {"dueDate": None, "dueAt": None}


def test_tasks_update_tags_only_adds_and_never_removes(client, monkeypatch):
    """Parity with @meshbook/sdk: tags= on update never removes a tag."""
    r = install(monkeypatch)
    r.add("GET", f"/api/tasks/{T1}", ok({"id": T1}))
    r.add("POST", f"/api/entities/task/{T1}/tags", ok({"attached": True}))
    r.add("GET", f"/api/entities/task/{T1}/tags", ok([{"id": "g0", "name": "old"},
                                                       {"id": "g1", "name": "new"}]))
    out = client.tasks.update(T1, tags=["new"])
    assert r.calls("PATCH") == [], "a tags-only update sends no empty PATCH"
    assert [q.get_method() for q in r.requests].count("DELETE") == 0
    assert {t["name"] for t in out["tags"]} == {"old", "new"}


def test_tags_set_is_the_explicit_replace(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/entities/contact/{C1}/tags",
          ok([{"id": "g0", "name": "old"}, {"id": "g1", "name": "q4-board"}]))
    r.add("POST", f"/api/entities/contact/{C1}/tags", ok({"attached": True}))
    r.add("DELETE", f"/api/entities/contact/{C1}/tags/g0", ok({"detached": True}))
    client.tags.set("contact", C1, ["Q4 board", "vip"])
    assert [r.body(q) for q in r.calls("POST")] == [{"name": "vip"}]
    assert len(r.calls("DELETE")) == 1


def test_tasks_update_nothing_is_refused(client, monkeypatch):
    install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        client.tasks.update(T1)
    assert ei.value.code == "bad_argument"


def test_tasks_list_sort_and_filters(client, monkeypatch):
    rows = [
        {"id": 1, "title": "b", "priority": "Low", "dueDate": "2026-11-02"},
        {"id": 2, "title": "a", "priority": "Urgent"},
        {"id": 3, "title": "c", "priority": "High", "dueAt": "2026-10-30T09:00:00+00:00"},
    ]
    r = install(monkeypatch)
    r.add("GET", "/api/users", USERS)
    r.add("GET", "/api/tasks", ok_list(rows, total=3))
    by_due = client.tasks.list(sort="due", assignee="wren", search="x")
    # Sent to the server (it sorts the whole set) AND mirrored on the page,
    # so an older server that ignores ?sort= still yields a sorted page.
    assert r.query() == {"limit": "50", "assigneeId": U1, "search": "x", "sort": "due"}
    assert [t["id"] for t in by_due] == [3, 1, 2]  # undated last
    assert by_due.total == 3
    assert [t["id"] for t in client.tasks.list(sort="priority")] == [2, 3, 1]  # Urgent first
    assert [t["id"] for t in client.tasks.list(sort="-priority")] == [1, 3, 2]
    assert [t["id"] for t in client.tasks.list(sort="-title")] == [3, 1, 2]
    assert [t["id"] for t in client.tasks.list(sort="-due")] == [1, 3, 2]  # still undated last
    for bad in ("vibes", "status"):
        with pytest.raises(MeshbookError):
            client.tasks.list(sort=bad)


def test_tasks_list_mine_open_only(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/me", ok({"user": {"id": U1}}))
    r.add("GET", "/api/tasks", ok_list([{"id": 1, "status": "Done"}, {"id": 2, "status": "Blocked"}]))
    mine = client.tasks.list_mine(open_only=True, limit=100)
    assert [t["id"] for t in mine] == [2]
    assert r.query() == {"limit": "100", "assigneeId": U1}


def test_tasks_delete_and_log_time(client, monkeypatch):
    r = install(monkeypatch)
    r.add("DELETE", f"/api/tasks/{T1}", ok({"id": T1, "deleted": True}))
    r.add("POST", "/api/task-time-logs", ok({"id": "l1"}))
    assert client.tasks.delete(T1)["deleted"] is True
    client.tasks.log_time(T1, 1.5, dt.date(2026, 10, 18), note="review")
    assert r.body() == {"taskId": T1, "hours": 1.5, "loggedForDate": "2026-10-18", "note": "review"}
    client.tasks.log_time(T1, 2)
    assert r.body()["loggedForDate"] == dt.datetime.now(dt.timezone.utc).date().isoformat()
    for bad in (0, 25, -1):
        with pytest.raises(MeshbookError):
            client.tasks.log_time(T1, bad)


# ─── projects (§102.6) ─────────────────────────────────────────────────


def test_projects_update_archive_add_member_delete(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", USERS)
    r.add("PATCH", f"/api/projects/{P1}", ok({"id": P1}))
    r.add("POST", f"/api/projects/{P1}/members", ok({"ok": True}))
    r.add("DELETE", f"/api/projects/{P1}", ok({"id": P1, "deleted": True}))
    client.projects.update(P1, name="Books", owner="alder", target_end_date=dt.date(2026, 12, 1))
    assert r.body() == {"name": "Books", "ownerUserId": U2, "targetEndDate": "2026-12-01"}
    client.projects.archive(P1)
    assert r.body() == {"status": "Complete"}
    client.projects.archive(P1, "Cancelled")
    assert r.body() == {"status": "Cancelled"}
    with pytest.raises(MeshbookError):
        client.projects.archive(P1, "Archived")
    client.projects.add_member(P1, "@wren", role="Contributor")
    assert r.body() == {"userId": U1, "roleOnProject": "Contributor"}  # parity with @meshbook/sdk
    with pytest.raises(MeshbookError):
        client.projects.add_member(P1, U1, role="Boss")
    assert client.projects.delete(P1)["deleted"] is True


def test_project_members_list_and_remove(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/projects/{P1}/members", ok_list([{"userId": U1, "roleOnProject": "Owner"}]))
    r.add("GET", "/api/users", USERS)
    r.add("DELETE", f"/api/projects/{P1}/members/{U2}", ok({"projectId": P1, "userId": U2,
                                                            "removed": True}))
    assert client.projects.members(P1)[0]["roleOnProject"] == "Owner"
    assert client.projects.remove_member(P1, "alder")["removed"] is True


def test_projects_list_and_create(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/projects", ok_list([]))
    r.add("POST", "/api/projects", ok({"id": P1}))
    client.projects.list(status="active", portfolio_id=A1, offset=50)
    assert r.query() == {"limit": "50", "offset": "50", "portfolioId": A1, "statusFilter": "active"}
    client.projects.create("Books", "BK", "desc", status="Active")
    assert r.body() == {"name": "Books", "code": "BK", "description": "desc", "status": "Active"}


# ─── contacts / companies (§102.7) ─────────────────────────────────────


def test_contacts_create_full_fields(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/contacts", ok({"id": C1}))
    r.add("POST", f"/api/entities/contact/{C1}/tags", ok({"attached": True}))
    r.add("GET", f"/api/entities/contact/{C1}/tags", ok([{"id": "g", "name": "vip"}]))
    out = client.contacts.create("Ada", "Lovelace", "ada@example.org", "Analytical",
                                 phone="+61 2 0000", title="Dr", role="Engineer",
                                 notes="met at the bench", tags=["vip"])
    assert r.body(r.calls("POST", "/api/contacts")[0]) == {
        "firstName": "Ada", "lastName": "Lovelace", "primaryEmail": "ada@example.org",
        "company": "Analytical", "primaryPhone": "+61 2 0000", "title": "Dr",
        "jobTitle": "Engineer", "description": "met at the bench"}
    assert out["tags"] == [{"id": "g", "name": "vip"}]


def test_contacts_update_get_delete_resolve(client, monkeypatch):
    r = install(monkeypatch)
    r.add("PATCH", f"/api/contacts/{C1}", ok({"id": C1}))
    r.add("GET", f"/api/contacts/{C1}", ok({"id": C1}))
    r.add("DELETE", f"/api/contacts/{C1}", ok({"id": C1, "deleted": True}))
    r.add("GET", "/api/contacts", ok_list([
        {"id": C1, "displayName": "Ada Lovelace", "primaryEmail": "ada@example.org"},
        {"id": A1, "displayName": "Ada Lovelace-Byron"}]))
    client.contacts.update(C1, phone="+1", job_title="CTO", notes=None, email="a@b.c")
    assert r.body() == {"primaryEmail": "a@b.c", "primaryPhone": "+1", "jobTitle": "CTO",
                        "description": None}
    with pytest.raises(MeshbookError):
        client.contacts.update(C1, job_title="A", role="B")
    assert client.contacts.get(C1)["id"] == C1
    assert client.contacts.delete(C1)["deleted"] is True
    assert client.contacts.resolve("ada lovelace")["id"] == C1  # exact beats prefix
    assert client.contacts.resolve("ADA@example.org")["id"] == C1
    with pytest.raises(MeshbookError) as ei:
        client.contacts.resolve("Ada")
    assert ei.value.code == "contact_ambiguous"


def test_companies_get_update_delete_contacts(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/companies/{A1}", ok({"id": A1}))
    r.add("PATCH", f"/api/companies/{A1}", ok({"id": A1}))
    r.add("DELETE", f"/api/companies/{A1}", ok({"id": A1, "deleted": True}))
    r.add("GET", f"/api/companies/{A1}/contacts", ok_list([]))
    r.add("POST", "/api/companies", ok({"id": A1}))
    r.add("GET", "/api/companies", ok_list([]))
    client.companies.get(A1)
    client.companies.update(A1, industry="Robots", website=None, fields={"suburb": "Gosford"})
    assert r.body() == {"website": None, "industry": "Robots", "suburb": "Gosford"}
    client.companies.delete(A1)
    client.companies.contacts(A1, limit=5)
    client.companies.create("Acme", website="https://acme.example", phone="1")
    assert r.body() == {"name": "Acme", "website": "https://acme.example", "phone": "1"}
    client.companies.list("acme")
    assert r.query() == {"limit": "50", "search": "acme"}


# ─── leads + pipelines ─────────────────────────────────────────────────

PIPES = ok_list([
    {"id": "p-sales", "name": "Sales", "isDefault": True,
     "stages": [{"id": "s-new", "name": "New"}, {"id": "s-won", "name": "Won"}]},
    {"id": "p-grants", "name": "Grants", "isDefault": False,
     "stages": [{"id": "g-new", "name": "New"}, {"id": "g-sub", "name": "Submitted"}]},
])


def test_leads_create_defaults_to_the_default_pipeline_first_stage(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/pipelines", PIPES)
    r.add("GET", "/api/contacts", ok_list([{"id": C1, "displayName": "Ada"}]))
    r.add("POST", "/api/leads", ok({"id": "L1"}))
    client.leads.create("Deal", value=10.0, contact="Ada")
    assert r.body() == {"title": "Deal", "pipelineId": "p-sales", "stageId": "s-new",
                        "valueAmount": 10.0, "contactId": C1}


def test_stage_resolution_prefers_default_and_supports_pipeline_slash_stage(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/pipelines", PIPES)
    assert client.pipelines.resolve_stage("new") == "s-new"
    assert client.pipelines.resolve_stage("Grants/New") == "g-new"
    assert client.pipelines.resolve_stage("submitted") == "g-sub"
    with pytest.raises(MeshbookError) as ei:
        client.pipelines.resolve_stage("Lost")
    assert ei.value.code == "stage_not_found" and "Sales/Won" in ei.value.message


def test_leads_move_by_stage_name_update_get_delete(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/pipelines", PIPES)
    r.add("POST", "/api/leads/L1/move-stage", ok({"id": "L1"}))
    r.add("PATCH", "/api/leads/L1", ok({"id": "L1"}))
    r.add("GET", "/api/leads/L1", ok({"id": "L1"}))
    r.add("DELETE", "/api/leads/L1", ok({"id": "L1", "deleted": True}))
    r.add("GET", "/api/leads", ok_list([]))
    client.leads.move_stage("L1", stage="won", note="signed")
    assert r.body() == {"stageId": "s-won", "note": "signed"}
    client.leads.update("L1", value=99, expected_close_date="2026-12-01", contact=None)
    assert r.body() == {"valueAmount": 99, "contactId": None, "expectedCloseDate": "2026-12-01"}
    client.leads.get("L1")
    client.leads.delete("L1")
    client.leads.list(stage="Submitted")
    assert r.query() == {"limit": "50", "stageId": "g-sub"}


# ─── chat / channels / DMs ─────────────────────────────────────────────


def test_chat_unreact_percent_encodes_the_emoji(client, monkeypatch):
    r = install(monkeypatch)
    r.add("DELETE", "/api/chat-messages/m1/reactions/%E2%9C%85", ok({}))
    client.chat.unreact("m1", "✅")
    assert r.last.get_full_url().endswith("/api/chat-messages/m1/reactions/%E2%9C%85")


def test_chat_entity_threads_and_get_message(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", f"/api/entities/task/{T1}/chat", ok({"id": "m"}))
    r.add("GET", "/api/chat-messages/m", ok({"id": "m", "channelId": None}))
    client.chat.post("note", entity_type="task", entity_id=T1)
    assert client.chat.get_message("m")["id"] == "m"
    with pytest.raises(MeshbookError):
        client.chat.list(entity_type="task")


def test_channels_create_reply_and_members_by_name(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", f"/api/meshes/{MESH}/channels", ok({"id": "c1", "name": "news"}))
    r.add("GET", "/api/chat-messages/m1", ok({"id": "m1", "channelId": "c9"}))
    r.add("POST", "/api/channels/c9/messages", ok({"id": "m2"}))
    r.add("GET", f"/api/meshes/{MESH}/channels", ok_list([{"id": "c1", "name": "news"}]))
    r.add("GET", "/api/users", USERS)
    r.add("POST", "/api/channels/c1/members", ok({"items": [{"userId": U1}]}))
    r.add("DELETE", f"/api/channels/c1/members/{U2}", ok({}))
    client.channels.create("#news", channel_type="broadcast", topic="t", private=True)
    assert r.body() == {"name": "news", "topic": "t", "isPrivate": True,
                        "channelType": "broadcast", "broadcastSeverity": "fyi"}
    client.channels.reply("m1", "ok")
    assert r.body() == {"bodyMd": "ok", "parentMessageId": "m1"}
    members = client.channels.add_member("news", user="@wren")
    assert members == [{"userId": U1}]
    client.channels.remove_member("#NEWS", user="Alder")
    with pytest.raises(MeshbookError):
        client.channels.add_member("news")


def test_channels_reply_refuses_entity_messages(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/chat-messages/m1", ok({"id": "m1", "channelId": None}))
    with pytest.raises(MeshbookError) as ei:
        client.channels.reply("m1", "x")
    assert ei.value.code == "not_channel_message"


def test_channel_not_found_names_the_real_ones(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/meshes/{MESH}/channels", ok_list([{"id": "c1", "name": "general"}]))
    with pytest.raises(MeshbookError) as ei:
        client.channels.read("#nope")
    assert ei.value.code == "channel_not_found" and "#general" in ei.value.message


def test_dms_list_open_read_send(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"/api/meshes/{MESH}/dms", ok_list([{"id": "d1"}]))
    r.add("GET", "/api/users", USERS)
    r.add("POST", f"/api/meshes/{MESH}/dms/with/{U1}", ok({"id": "d1"}))
    r.add("GET", "/api/channels/d1/messages", ok_list([{"id": "m"}]))
    r.add("POST", "/api/channels/d1/messages", ok({"id": "m2"}))
    assert client.dms.list() == [{"id": "d1"}]
    assert client.dms.read("wren", limit=5) == [{"id": "m"}]
    assert r.query() == {"limit": "5"}
    client.dms.send("@wren", "hi")
    assert r.body() == {"bodyMd": "hi"}


# ─── membership ────────────────────────────────────────────────────────


def test_membership_verbs(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", f"/api/meshes/{MESH}/invite", ok({}))
    r.add("GET", "/api/meshes/my-pending", ok_list([{"id": MESH2, "name": "Invited Mesh"}]))
    r.add("POST", f"/api/meshes/{MESH2}/respond", ok({}))
    r.add("GET", "/api/users", USERS)
    r.add("POST", f"/api/meshes/{MESH}/set-role", ok({}))
    r.add("POST", f"/api/meshes/{MESH}/remove", ok({}))
    r.add("GET", "/api/me", ok({"user": {"id": U2}}))
    client.meshes.invite("@newbie", role="reader")
    assert r.body() == {"username": "newbie", "role": "reader"}
    client.meshes.respond("invited mesh", accept=False)
    assert r.body() == {"action": "decline"}
    assert r.last.get_header("X-active-mesh-id") is None
    client.meshes.set_role("wren", "reader")
    assert r.body() == {"userId": U1, "role": "reader"}
    client.meshes.remove_member("@wren")
    assert r.body() == {"userId": U1}
    client.meshes.leave()
    assert r.body() == {"userId": U2}


# ─── the rest ──────────────────────────────────────────────────────────


def test_notifications_files_views_fields_health(client, monkeypatch, tmp_path):
    r = install(monkeypatch)
    r.add("GET", "/api/notifications", ok_list([]))
    r.add("POST", f"/api/entities/lead/{A1}/attachments/links", ok({"id": "a"}))
    r.add("GET", "/api/saved-views", ok_list([]))
    r.add("POST", "/api/saved-views", ok({"id": "v"}))
    r.add("GET", "/api/custom-fields", ok_list([]))
    r.add("GET", "/api/health", ok({"status": "ok"}))
    client.notifications.list(unread_only=True, limit=10)
    assert r.query() == {"limit": "10", "unreadOnly": "true"}
    client.files.link("lead", A1, "https://example.org/x", "spec")
    assert r.body() == {"url": "https://example.org/x", "filename": "spec"}
    client.saved_views.list("lead")
    assert r.query() == {"entityType": "lead"}
    client.saved_views.create("leads", "Hot", {"stage": "New"}, shared=True)
    assert r.body() == {"module": "leads", "name": "Hot", "filterJson": {"stage": "New"},
                        "isShared": True}
    client.custom_fields.list("contact")
    assert client.health() == {"status": "ok"}
    health = r.calls("GET", "/api/health")[0]
    assert health.get_header("Authorization") is None
    assert health.get_header("X-active-mesh-id") is None


def test_files_attach_missing_and_empty(client, monkeypatch, tmp_path):
    install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        client.files.attach("lead", A1, tmp_path / "nope.txt")
    assert ei.value.code == "file_not_found"
    (tmp_path / "e.txt").write_bytes(b"")
    with pytest.raises(MeshbookError) as ei:
        client.files.attach("lead", A1, tmp_path / "e.txt")
    assert ei.value.code == "empty_file"


def test_exports_by_mesh_name(client, monkeypatch, tmp_path):
    r = install(monkeypatch)
    r.add("GET", "/api/meshes", meshes_payload((MESH, "Home"), (MESH2, "Work")))
    r.add("POST", f"/api/meshes/{MESH2}/export", ok({"id": "x", "status": "pending"}))
    r.add("GET", f"/api/meshes/{MESH2}/exports", ok_list([{"id": "x", "status": "ready"}]))
    job = client.exports.start(mesh="work")
    assert job.status == "pending"
    assert r.last.get_header("X-active-mesh-id") == MESH2
    assert client.exports.list(mesh="Work")[0].status == "ready"
    assert client.active_mesh_id == MESH


def test_unset_sentinel_is_falsy_and_singleton():
    from meshbook.resources._base import _Unset

    assert not UNSET and _Unset() is UNSET and repr(UNSET) == "UNSET"


def test_fields_escape_hatch_never_overrides_named_args(client, monkeypatch):
    r = install(monkeypatch)
    r.add("PATCH", f"/api/contacts/{C1}", ok({"id": C1}))
    client.contacts.update(C1, phone="+1", fields={"primaryPhone": "ignored", "pets": "cat"})
    assert json.loads(r.last.data) == {"primaryPhone": "+1", "pets": "cat"}
