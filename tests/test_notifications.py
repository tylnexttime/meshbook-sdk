"""The notification write surface (0.4.3, DEV-DEBT §106) against a STATEFUL fake.

Every write test here runs against :class:`meshbook.testing.FakeNotificationServer`,
which applies the server's real visibility rule from the X-Active-Mesh-Id
header and answers like the server in production (``mode="old"``: PATCH says
``updated: true`` whether or not a row matched) or like the s106 server
branch (``mode="fixed"``). A test asserts what the client REPORTS and what
the fake's rows really ARE, so a client that trusts the wrong signal goes red.

The red cases are proven red: each ``sc_*`` scenario below is run normally
(must hold) and, in ``test_mutant_is_caught``, against a deliberately broken
client (must NOT hold). To run the whole module against a mutant::

    MESHBOOK_MUTANT=trust_updated pytest tests/test_notifications.py

(also ``ambient_mesh``, ``readat_state``, ``str_iter``, ``no_group_check``,
``row_mesh``, ``one_page_lookup``; comma-separated to combine). Expect failures: that is the point.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import meshbook.resources.notifications as nmod
from meshbook import MeshbookClient, MeshbookError
from meshbook.resources import NOTIFICATION_GROUP_KINDS, NOTIFICATION_STATUSES, Notifications
from meshbook.testing import (
    FOREIGN_MESH,
    MESH_A,
    MESH_B,
    MESH_C,
    OTHER_USER_ID,
    FakeNotificationServer,
)

UTC = dt.timezone.utc
LONG_AGO = dt.datetime(2026, 5, 1, tzinfo=UTC)


# ─── mutants: deliberately broken clients the tests must catch ───────────


def _m_trust_updated(mp: pytest.MonkeyPatch) -> None:
    """Believes the server's `updated: true` (the §106 trap)."""
    orig = nmod._judge

    def judge(data: Any, action: str) -> str:
        if isinstance(data, dict) and data.get("updated") is True:
            return "applied"
        return orig(data, action)

    mp.setattr(nmod, "_judge", judge)


def _m_ambient_mesh(mp: pytest.MonkeyPatch) -> None:
    """PATCHes (and re-reads) under the client's ambient mesh, not the row's."""
    mp.setattr(Notifications, "_patch_mesh", lambda self, f: self._c.active_mesh_id)


def _m_readat_state(mp: pytest.MonkeyPatch) -> None:
    """Keys read state on readAt (which survives a mark-unread)."""
    mp.setattr(nmod, "_is_read", lambda row: row.get("readAt") is not None)


def _m_str_iter(mp: pytest.MonkeyPatch) -> None:
    """Iterates a str id as characters."""
    orig = nmod._normalise_ids
    mp.setattr(
        nmod,
        "_normalise_ids",
        lambda ids, cap: orig(list(ids) if isinstance(ids, str) else ids, cap),
    )


def _m_no_group_check(mp: pytest.MonkeyPatch) -> None:
    """Sends any group string (the server ignores an unknown one)."""
    mp.setattr(nmod, "_check_group", lambda group: group)


def _m_row_mesh(mp: pytest.MonkeyPatch) -> None:
    """PATCHes under the row's own meshId (for an invitation: the mesh it is ABOUT)."""
    mp.setattr(Notifications, "_patch_mesh", lambda self, f: f.row.get("meshId"))


def _m_one_page_lookup(mp: pytest.MonkeyPatch) -> None:
    """Looks for the row on the first page only."""
    mp.setattr(Notifications, "MAX_PAGES_PER_MESH", 1)


MUTANTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "trust_updated": _m_trust_updated,
    "ambient_mesh": _m_ambient_mesh,
    "readat_state": _m_readat_state,
    "str_iter": _m_str_iter,
    "no_group_check": _m_no_group_check,
    "row_mesh": _m_row_mesh,
    "one_page_lookup": _m_one_page_lookup,
}
_ENV_MUTANTS = [m.strip() for m in os.environ.get("MESHBOOK_MUTANT", "").split(",") if m.strip()]


@pytest.fixture(autouse=True)
def _env_mutant(monkeypatch):
    for name in _ENV_MUTANTS:
        MUTANTS[name](monkeypatch)
    yield


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(params=["old", "fixed"])
def mode(request) -> str:
    return request.param


def _client(tmp_path: Path, mesh: str | None = MESH_B) -> MeshbookClient:
    return MeshbookClient(token="mb_token_test", mesh=mesh, config_path=tmp_path / "none")


@pytest.fixture
def fake(monkeypatch, mode) -> FakeNotificationServer:
    return FakeNotificationServer(mode=mode).install(monkeypatch)


@pytest.fixture
def client(tmp_path) -> MeshbookClient:
    return _client(tmp_path)  # active mesh B


def _one(out: dict[str, Any]) -> dict[str, Any]:
    assert len(out["results"]) == 1
    return out["results"][0]


# ─── scenarios: each returns True when the client behaved correctly ──────


def sc_silent_miss(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """The server answers the PATCH like a hit but changes nothing."""
    nid = fake.add(mesh=MESH_B)
    fake.drop(nid)
    out = c.notifications.mark_read(nid)
    r = _one(out)
    return (
        r["status"] == "unconfirmed"
        and r["sent"] is True
        and out["ok"] is False
        and not fake.row(nid).is_read
    )


def sc_wrong_ambient(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """The row is in mesh A while the client's mesh is B (C-01, C-09)."""
    nid = fake.add(mesh=MESH_A)
    out = c.notifications.mark_read(nid)
    r = _one(out)
    return (
        r["status"] == "applied"
        and r["mesh"] == MESH_A
        and fake.row(nid).is_read
        and [p.mesh for p in fake.patches] == [MESH_A]
    )


def sc_wrong_ambient_dismiss(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    nid = fake.add(mesh=MESH_A)
    out = c.notifications.dismiss(nid, confirm=True)
    r = _one(out)
    return r["status"] == "applied" and fake.row(nid).dismissed_at is not None


def sc_stale_readat(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """Unread (isRead false) but readAt still set, as after a mark-unread (C-02)."""
    nid = fake.add(mesh=MESH_B, is_read=False, read_at=LONG_AGO)
    out = c.notifications.mark_read(nid)
    return _one(out)["status"] == "applied" and len(fake.patches) == 1 and fake.row(nid).is_read


def sc_single_str(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """A str is ONE id (C-05)."""
    nid = fake.add(mesh=MESH_B)
    try:
        out = c.notifications.mark_read(nid)
    except MeshbookError:
        return False
    return out["counts"] == {"applied": 1} and len(fake.patches) == 1


def sc_unknown_group(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """A guessed group is refused before the wire (C-03)."""
    fake.add(mesh=MESH_B, kind="Mention")
    fake.add(mesh=MESH_B, kind="TaskAssigned")
    try:
        c.notifications.read_all(group="invitations", apply=True)
    except MeshbookError as e:
        return e.code == "bad_argument" and fake.log == []
    except Exception:
        return False
    return False


def sc_cross_mesh_invitation(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """An invitation to a mesh the scoped token does not cover (C-22)."""
    fake.scope_meshes = {MESH_B}
    nid = fake.add(cross_mesh=True, mesh=FOREIGN_MESH, kind="MeshInvitationReceived")
    out = c.notifications.mark_read(nid)
    return _one(out)["status"] == "applied" and fake.row(nid).is_read


def sc_deep_row(fake: FakeNotificationServer, c: MeshbookClient) -> bool:
    """The six-month-old row sits far below the first page (C-01)."""
    ids = fake.add_many(450, mesh=MESH_B)
    out = c.notifications.mark_read(ids[10])
    return _one(out)["status"] == "applied" and fake.row(ids[10]).is_read


def test_silent_miss_is_unconfirmed(fake, client):
    assert sc_silent_miss(fake, client)


def test_wrong_ambient_mesh_hunts_and_patches_under_the_rows_mesh(fake, client):
    assert sc_wrong_ambient(fake, client)


def test_dismiss_under_the_wrong_ambient_mesh(fake, client):
    assert sc_wrong_ambient_dismiss(fake, client)


def test_stale_read_at_is_not_read(fake, client):
    assert sc_stale_readat(fake, client)


def test_a_str_is_one_id(fake, client):
    assert sc_single_str(fake, client)


def test_an_invitation_to_an_unscoped_mesh(fake, client):
    assert sc_cross_mesh_invitation(fake, client)


def test_a_row_far_below_the_first_page(fake, client):
    assert sc_deep_row(fake, client)


def test_unknown_group_is_refused_before_the_wire(fake, client):
    assert sc_unknown_group(fake, client)


CAUGHT = [
    ("trust_updated", sc_silent_miss, "old"),
    ("trust_updated", sc_silent_miss, "fixed"),
    ("ambient_mesh", sc_wrong_ambient, "old"),
    ("ambient_mesh", sc_wrong_ambient, "fixed"),
    ("ambient_mesh", sc_wrong_ambient_dismiss, "old"),
    ("trust_updated+ambient_mesh", sc_wrong_ambient, "old"),
    ("readat_state", sc_stale_readat, "old"),
    ("readat_state", sc_stale_readat, "fixed"),
    ("str_iter", sc_single_str, "old"),
    ("no_group_check", sc_unknown_group, "old"),
    ("row_mesh", sc_cross_mesh_invitation, "old"),
    ("row_mesh", sc_cross_mesh_invitation, "fixed"),
    ("one_page_lookup", sc_deep_row, "old"),
]


@pytest.mark.skipif(bool(_ENV_MUTANTS), reason="already running against a mutant")
@pytest.mark.parametrize(
    "mutant,scenario,server", CAUGHT, ids=[f"{m}-{s.__name__}-{v}" for m, s, v in CAUGHT]
)
def test_mutant_is_caught(monkeypatch, tmp_path, mutant, scenario, server):
    """A red case that is not red proves nothing: each scenario must FAIL
    against the broken client it exists to catch."""
    for name in mutant.split("+"):
        MUTANTS[name](monkeypatch)
    fake = FakeNotificationServer(mode=server).install(monkeypatch)
    assert scenario(fake, _client(tmp_path)) is False


# ─── validation: refused before anything is sent ─────────────────────────


@pytest.mark.parametrize(
    "ids",
    [
        "not-a-uuid",
        "",
        [],
        ["3fa2", "4e1c"],
        [str(uuid.uuid4()), "3fa2"],
        [123],
        b"bytes",
        None,
        {str(uuid.uuid4()): True},
        42,
    ],
    ids=[
        "word",
        "empty-str",
        "empty-list",
        "truncated",
        "mixed",
        "int-item",
        "bytes",
        "none",
        "dict",
        "int",
    ],
)
def test_bad_ids_are_refused_before_the_wire(fake, client, ids):
    with pytest.raises(MeshbookError) as ei:
        client.notifications.mark_read(ids)
    assert ei.value.code == "bad_argument" and ei.value.hint == "fix_argument"
    assert "Nothing was sent" in ei.value.message
    assert fake.log == []


def test_too_many_ids_in_one_call(fake, client):
    with pytest.raises(MeshbookError) as ei:
        client.notifications.mark_unread(
            [str(uuid.uuid4()) for _ in range(Notifications.MAX_IDS + 1)]
        )
    assert ei.value.code == "bad_argument" and fake.log == []


def test_ids_are_normalised_and_deduplicated(fake, client):
    a = fake.add(mesh=MESH_B)
    b = fake.add(mesh=MESH_B)
    out = client.notifications.mark_read(
        [a.upper(), " " + a + " ", uuid.UUID(b), "{" + b + "}", a.replace("-", "")]
    )
    assert [r["id"] for r in out["results"]] == [a, b]
    assert len(fake.patches) == 2 and out["ok"]


@pytest.mark.parametrize("confirm", [False, "yes", 1, None])
def test_dismiss_needs_an_explicit_confirm(fake, client, confirm):
    nid = fake.add(mesh=MESH_B)
    with pytest.raises(MeshbookError) as ei:
        client.notifications.dismiss(nid, confirm=confirm)
    assert (ei.value.code, ei.value.hint) == ("bad_argument", "confirm_irreversible")
    assert fake.log == [] and fake.row(nid).dismissed_at is None


@pytest.mark.parametrize("group", ["invitations", "mention", "all", "mentions,invites", "", 5])
def test_list_refuses_an_unknown_group(fake, client, group):
    with pytest.raises(MeshbookError) as ei:
        client.notifications.list(group=group)
    assert ei.value.code == "bad_argument" and fake.log == []


def test_list_canonicalises_a_known_group_and_keeps_its_old_wire(fake, client):
    client.notifications.list(group=" Mentions ")
    assert fake.log[-1].query == {"group": "mentions"}
    client.notifications.list(unread_only=True, limit=10)
    assert fake.log[-1].query == {"limit": "10", "unreadOnly": "true"}
    assert fake.log[-1].mesh == MESH_B


@pytest.mark.parametrize(
    "kwargs",
    [
        {"group": "mention"},
        {"cross_mesh": "all"},
        {"apply": "yes"},
        {"apply": 1},
        {"older_than": dt.timedelta(days=-1)},
        {"older_than": dt.datetime(2026, 1, 1)},  # naive
        {"older_than": "last week"},
        {"kinds": []},
        {"kinds": [""]},
    ],
)
def test_read_all_refuses_bad_arguments(fake, client, kwargs):
    with pytest.raises(MeshbookError):
        client.notifications.read_all(**kwargs)
    assert fake.log == []


def test_read_all_needs_a_mesh_unless_cross_mesh_only(fake, tmp_path):
    c = _client(tmp_path, mesh=None)
    fake.add(mesh=MESH_A)
    x = fake.add(cross_mesh=True, kind="MeshInvitationReceived")
    with pytest.raises(MeshbookError) as ei:
        c.notifications.read_all(apply=True)
    assert ei.value.code == "no_active_mesh" and fake.log == []
    out = c.notifications.read_all(cross_mesh="only", apply=True)
    assert out["ids"] == [x] and out["mesh"] is None and out["outcome"]["ok"]
    assert all(r.mesh is None for r in fake.log)  # never a header
    assert fake.row(x).is_read


# ─── exact wire ──────────────────────────────────────────────────────────


def test_exact_bodies_carry_only_the_key_being_set(fake, client):
    """{"isRead": None} would mark UNREAD and {"dismiss": "false"} would
    dismiss on the old server (C-21): only literal JSON booleans, one key."""
    r1, r2, r3 = fake.add(mesh=MESH_B), fake.add(mesh=MESH_B, is_read=True), fake.add(mesh=MESH_B)
    client.notifications.mark_read(r1)
    client.notifications.mark_unread(r2)
    client.notifications.dismiss(r3, confirm=True)
    assert [p.body for p in fake.patches] == [
        {"isRead": True},
        {"isRead": False},
        {"dismiss": True},
    ]
    raw = [json.dumps(p.body) for p in fake.patches]
    assert raw == ['{"isRead": true}', '{"isRead": false}', '{"dismiss": true}']
    assert all(type(v) is bool for p in fake.patches for v in p.body.values())


def test_a_patch_is_never_retried(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.fail("PATCH", status=503, times=5)
    client.notifications.mark_read(nid)
    assert len(fake.patches) == 1


# ─── finding the row ─────────────────────────────────────────────────────


def test_a_row_deep_in_the_listing_is_found(fake, client):
    ids = fake.add_many(450, mesh=MESH_B)
    target = ids[10]  # old: near the bottom (position 439)
    out = client.notifications.mark_read(target)
    assert _one(out)["status"] == "applied" and fake.row(target).is_read
    lists = fake.requests("GET", "notifications.list")
    assert [(q.query["limit"], q.query["offset"]) for q in lists[:3]] == [
        ("200", "0"),
        ("200", "200"),
        ("200", "400"),
    ]
    assert all("unreadOnly" not in q.query for q in lists)  # read rows count for "already"


def test_an_explicit_mesh_is_strict(fake, client):
    nid = fake.add(mesh=MESH_A)
    out = client.notifications.mark_read(nid, mesh=MESH_B)
    r = _one(out)
    assert (r["status"], r["sent"], r["mesh"]) == ("not_in_mesh", False, MESH_B)
    assert fake.patches == [] and fake.requests(route="meshes.list") == []
    assert not fake.row(nid).is_read and out["ok"] is False


def test_an_explicit_mesh_by_name(fake, client):
    nid = fake.add(mesh=MESH_A)
    out = client.notifications.mark_read(nid, mesh="alpha")
    assert _one(out)["status"] == "applied" and fake.patches[0].mesh == MESH_A


def test_a_cross_mesh_row_is_patched_under_the_listed_mesh(monkeypatch, tmp_path, mode):
    """An invitation's meshId is the mesh it invites you TO; a scoped token
    may not cover it (C-22)."""
    fake = FakeNotificationServer(mode=mode, scope_meshes={MESH_B}).install(monkeypatch)
    nid = fake.add(cross_mesh=True, mesh=FOREIGN_MESH, kind="MeshInvitationReceived")
    out = _client(tmp_path).notifications.mark_read(nid)
    assert _one(out)["status"] == "applied" and fake.row(nid).is_read
    assert [p.mesh for p in fake.patches] == [MESH_B]


def test_a_cross_mesh_row_with_no_mesh_at_all(fake, tmp_path):
    c = _client(tmp_path, mesh=None)
    nid = fake.add(cross_mesh=True, kind="StorageThresholdWarning")
    out = c.notifications.mark_read(nid)
    assert _one(out)["status"] == "applied" and _one(out)["mesh"] is None
    assert [p.mesh for p in fake.patches] == [None]
    assert fake.requests(route="meshes.list") == []  # found on the first listing


def test_a_dismissed_row_is_not_found_and_not_sent(fake, client):
    nid = fake.add(mesh=MESH_B, dismissed=True)
    out = client.notifications.mark_read(nid)
    r = _one(out)
    assert (r["status"], r["sent"]) == ("not_found", False) and fake.patches == []
    assert {s["mesh"] for s in out["searched"]} == {MESH_A, MESH_B, MESH_C}
    assert all(s["exhausted"] for s in out["searched"])


def test_someone_elses_row_is_not_found(fake, client):
    nid = fake.add(mesh=MESH_B, user=OTHER_USER_ID)
    assert _one(client.notifications.mark_read(nid))["status"] == "not_found"
    assert fake.patches == [] and not fake.row(nid).is_read


def test_a_search_cut_short_is_unconfirmed_never_not_found(fake, client, monkeypatch):
    monkeypatch.setattr(Notifications, "MAX_PAGES_PER_MESH", 1)
    ids = fake.add_many(250, mesh=MESH_B)
    r = _one(client.notifications.mark_read(ids[0]))  # position 249: page 2
    assert (r["status"], r["sent"]) == ("unconfirmed", False) and fake.patches == []


def test_a_scoped_token_skips_meshes_it_cannot_reach(monkeypatch, tmp_path, mode):
    fake = FakeNotificationServer(mode=mode, scope_meshes={MESH_B}).install(monkeypatch)
    nid = fake.add(mesh=MESH_A)
    out = _client(tmp_path).notifications.mark_read(nid)
    r = _one(out)
    assert (r["status"], r["sent"]) == ("unconfirmed", False) and fake.patches == []
    skipped = {s["mesh"]: s["skipped"] for s in out["searched"]}
    assert skipped[MESH_A] == "token_out_of_scope" and skipped[MESH_B] is None


def test_already_in_state_sends_nothing(fake, client):
    read = fake.add(mesh=MESH_B, is_read=True)
    unread = fake.add(mesh=MESH_B)
    assert _one(client.notifications.mark_read(read))["status"] == "already"
    assert _one(client.notifications.mark_unread(unread))["status"] == "already"
    stale = fake.add(mesh=MESH_B, is_read=False, read_at=LONG_AGO)
    assert _one(client.notifications.mark_unread(stale))["status"] == "already"
    assert fake.patches == []


def test_mark_unread_is_judged_by_is_read(fake, client):
    nid = fake.add(mesh=MESH_B, is_read=True)
    out = client.notifications.mark_unread(nid)
    assert _one(out)["status"] == "applied" and not fake.row(nid).is_read
    # The production server keeps readAt on unread; the fixed one clears it.
    assert (fake.row(nid).read_at is None) is (fake.mode == "fixed")


def test_the_lookup_failing_raises_with_nothing_sent(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.fail("GET", route="notifications.list", status=500)
    with pytest.raises(MeshbookError):
        client.notifications.mark_read(nid)
    assert fake.patches == []


# ─── what an answer proves ───────────────────────────────────────────────


def test_the_fixed_server_is_believed_without_a_re_read(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="fixed").install(monkeypatch)
    nid = fake.add(mesh=MESH_B)
    out = _client(tmp_path).notifications.mark_read(nid)
    assert out["server"] == "fixed" and _one(out)["status"] == "applied"
    after_patch = fake.log[fake.log.index(fake.patches[0]) + 1 :]
    assert after_patch == []


def test_the_old_server_is_checked_by_re_reading(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    nid = fake.add(mesh=MESH_B)
    out = _client(tmp_path).notifications.mark_read(nid)
    assert out["server"] == "old" and _one(out)["status"] == "applied"
    after_patch = fake.log[fake.log.index(fake.patches[0]) + 1 :]
    assert [(r.route, r.mesh) for r in after_patch] == [("notifications.list", MESH_B)]


def test_a_row_gone_before_the_patch(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.before_patch = lambda server, i: server.remove(i)
    r = _one(client.notifications.mark_read(nid))
    if fake.mode == "fixed":
        assert (r["status"], r["sent"], r["code"]) == ("not_found", True, "notification_not_found")
    else:
        assert (r["status"], r["sent"]) == ("unconfirmed", True)


@pytest.mark.parametrize("envelope", ["error", "detail"])
def test_a_coded_404_in_either_envelope_is_not_found(monkeypatch, tmp_path, envelope):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    nid = fake.add(mesh=MESH_B)
    body = {"code": "notification_not_found", "message": "no such row here"}
    fake.fail("PATCH", status=404, payload={envelope: body})
    r = _one(_client(tmp_path).notifications.mark_read(nid))
    assert (r["status"], r["code"], r["sent"]) == ("not_found", "notification_not_found", True)


def test_a_bare_404_means_server_too_old_and_stops_the_batch(fake, client):
    ids = [fake.add(mesh=MESH_B) for _ in range(3)]
    fake.fail("PATCH", status=404, payload={"detail": "Not Found"})
    out = client.notifications.mark_read(ids)
    assert [r["status"] for r in out["results"]] == ["error", "not_attempted", "not_attempted"]
    assert out["results"][0]["hint"] == "server_too_old"
    assert len(fake.patches) == 1 and not out["ok"]


def test_a_read_only_token_stops_after_the_first_refusal(monkeypatch, tmp_path, mode):
    fake = FakeNotificationServer(mode=mode, readonly=True).install(monkeypatch)
    ids = [fake.add(mesh=MESH_B) for _ in range(4)]
    out = _client(tmp_path).notifications.mark_read(ids)
    assert out["counts"] == {"error": 1, "not_attempted": 3}
    assert out["results"][0]["code"] == "token_readonly" and len(fake.patches) == 1
    assert [r["sent"] for r in out["results"]] == [True, False, False, False]


def test_a_401_stops_the_batch(fake, client):
    ids = [fake.add(mesh=MESH_B) for _ in range(2)]
    fake.fail(
        "PATCH",
        status=401,
        payload={"detail": {"code": "not_authenticated", "message": "Login required."}},
    )
    out = client.notifications.mark_read(ids)
    assert [r["status"] for r in out["results"]] == ["error", "not_attempted"]
    assert out["results"][0]["hint"] == "reauthenticate"


def test_a_503_mid_batch_is_recorded_and_the_batch_continues(fake, client):
    a, b, c = (fake.add(mesh=MESH_B) for _ in range(3))
    fake.fail("PATCH", id=b, status=503)
    out = client.notifications.mark_read([a, b, c])
    assert [r["status"] for r in out["results"]] == ["applied", "error", "applied"]
    assert out["results"][1]["code"] == "service_unavailable"
    assert fake.row(a).is_read and not fake.row(b).is_read and fake.row(c).is_read


def test_a_lost_answer_after_the_write_landed_is_applied(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.fail("PATCH", network=True, after=True)
    r = _one(client.notifications.mark_read(nid))
    assert r["status"] == "applied" and fake.row(nid).is_read


def test_a_lost_answer_before_the_write_is_an_error(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.fail("PATCH", network=True)
    r = _one(client.notifications.mark_read(nid))
    assert (r["status"], r["code"]) == ("error", "network_error") and not fake.row(nid).is_read


def test_a_lost_answer_that_cannot_be_re_read_is_maybe_applied(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.fail("PATCH", network=True, after=True)
    fake.after_patch = lambda server, i: server.fail("GET", network=True, times=5)
    r = _one(client.notifications.mark_read(nid))
    assert (r["status"], r["hint"], r["sent"]) == ("maybe_applied", "check_before_retry", True)


def test_a_failed_re_read_keeps_what_it_settled(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    ids = fake.add_many(300, mesh=MESH_B)
    newest, oldest = ids[-1], ids[0]  # page 0 and page 200

    def break_page_two(server: FakeNotificationServer, nid: str) -> None:
        if nid == oldest:
            server.fail("GET", route="notifications.list", query={"offset": "200"}, network=True)

    fake.after_patch = break_page_two
    out = _client(tmp_path).notifications.mark_read([newest, oldest])
    assert [r["status"] for r in out["results"]] == ["applied", "unconfirmed"]
    assert fake.row(oldest).is_read  # it did land; the client just cannot show it


def test_repeated_transport_failures_end_the_batch(fake, client):
    ids = [fake.add(mesh=MESH_B) for _ in range(5)]
    fake.fail("PATCH", status=503, times=10)
    out = client.notifications.mark_read(ids)
    assert [r["status"] for r in out["results"]] == ["error"] * 3 + ["not_attempted"] * 2
    assert len(fake.patches) == Notifications.MAX_TRANSIENT


def test_rows_arriving_meanwhile_do_not_fool_the_re_read(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    ids = fake.add_many(300, mesh=MESH_B)
    target = ids[100]  # position 199: the last row of page 1
    fake.after_patch = lambda server, i: server.add_many(3, mesh=MESH_B)  # pushes it to page 2
    r = _one(_client(tmp_path).notifications.mark_read(target))
    assert r["status"] == "applied" and fake.row(target).is_read


def test_dismiss_is_confirmed_by_the_row_leaving_its_place(fake, client):
    keep = fake.add_many(5, mesh=MESH_B)
    nid = keep[2]
    r = _one(client.notifications.dismiss(nid, confirm=True))
    assert (r["status"], r["mesh"]) == ("applied", MESH_B) and fake.row(nid).dismissed_at
    again = _one(client.notifications.dismiss(nid, confirm=True))
    assert again["status"] == "not_found" and len(fake.patches) == 1


def test_a_dismiss_that_did_not_land(fake, client):
    nid = fake.add(mesh=MESH_B)
    fake.drop(nid)
    r = _one(client.notifications.dismiss(nid, confirm=True))
    assert r["status"] == "unconfirmed" and fake.row(nid).dismissed_at is None


def test_results_keep_the_callers_order_and_shape(fake, client):
    a = fake.add(mesh=MESH_A)
    b = fake.add(mesh=MESH_B, is_read=True)
    missing = str(uuid.uuid4())
    out = client.notifications.mark_read([missing, b, a])
    assert [(r["id"], r["status"]) for r in out["results"]] == [
        (missing, "not_found"),
        (b, "already"),
        (a, "applied"),
    ]
    keys = {"id", "status", "sent", "mesh", "kind", "title", "message", "code", "hint"}
    assert all(set(r) == keys for r in out["results"])
    assert set(out) == {"action", "ok", "counts", "results", "server", "searched"}
    assert all(r["status"] in NOTIFICATION_STATUSES for r in out["results"])
    json.dumps(out)  # JSON-able as returned


# ─── read_all ────────────────────────────────────────────────────────────


def test_read_all_is_a_dry_run_by_default(fake, client):
    ids = [fake.add(mesh=MESH_B) for _ in range(3)]
    fake.add(mesh=MESH_B, is_read=True)
    out = client.notifications.read_all()
    assert out["dry_run"] is True and out["outcome"] is None
    assert out["ids"] == ids[::-1] and out["count"] == 3 and out["by_kind"] == {"Mention": 3}
    assert fake.patches == [] and fake.requests("POST") == []
    assert not any(fake.row(i).is_read for i in ids)


def test_read_all_apply_marks_exactly_the_plan_and_never_posts(fake, client):
    ids = [fake.add(mesh=MESH_B) for _ in range(3)]
    other_mesh = fake.add(mesh=MESH_A)
    out = client.notifications.read_all(apply=True)
    assert out["dry_run"] is False and out["outcome"]["ok"]
    assert out["outcome"]["counts"] == {"applied": 3}
    assert all(fake.row(i).is_read for i in ids) and not fake.row(other_mesh).is_read
    assert fake.requests("POST") == [] and {p.mesh for p in fake.patches} == {MESH_B}


def test_read_all_filters(fake, client):
    old_mention = fake.add(mesh=MESH_B, created_at=LONG_AGO)
    old_task = fake.add(mesh=MESH_B, kind="TaskAssigned", created_at=LONG_AGO)
    new_mention = fake.add(mesh=MESH_B, created_at=dt.datetime.now(UTC) - dt.timedelta(days=1))
    invite = fake.add(cross_mesh=True, kind="MeshInvitationReceived", created_at=LONG_AGO)
    nf = client.notifications
    assert nf.read_all(older_than="2026-06-01T00:00:00+00:00")["ids"] == [
        invite,
        old_task,
        old_mention,
    ]
    assert set(nf.read_all(older_than=dt.timedelta(days=30))["ids"]) == {
        invite,
        old_task,
        old_mention,
    }
    assert nf.read_all(kinds="Mention")["ids"] == [new_mention, old_mention]
    assert nf.read_all(group="assignments")["ids"] == [old_task]
    assert nf.read_all(group="assignments")["filters"]["group"] == "assignments"
    assert invite not in nf.read_all(cross_mesh="exclude")["ids"]
    only = nf.read_all(cross_mesh="only")
    assert only["ids"] == [invite] and only["cross_mesh_rows"] == 1
    assert fake.patches == []


def test_read_all_caps_one_call_and_says_what_remains(fake, client, monkeypatch):
    monkeypatch.setattr(Notifications, "MAX_IDS", 5)
    ids = [fake.add(mesh=MESH_B) for _ in range(12)]
    out = client.notifications.read_all(apply=True)
    assert (out["count"], out["matched"], out["remaining"]) == (5, 12, 7)
    assert sum(fake.row(i).is_read for i in ids) == 5
    again = client.notifications.read_all()
    assert again["matched"] == 7


def test_read_all_says_when_its_scan_was_cut_short(fake, client, monkeypatch):
    monkeypatch.setattr(Notifications, "READ_ALL_MAX_PAGES", 1)
    fake.add_many(250, mesh=MESH_B)
    out = client.notifications.read_all()
    assert out["truncated"] is True and out["scanned"] == 200


def test_read_all_never_marks_kinds_outside_the_group(monkeypatch, tmp_path):
    """If a server ever answered a group with extra kinds, they stay unread."""
    kinds = {**NOTIFICATION_GROUP_KINDS, "mentions": ("Mention", "Weird")}
    fake = FakeNotificationServer(mode="old", group_kinds=kinds).install(monkeypatch)
    weird = fake.add(mesh=MESH_B, kind="Weird")
    mention = fake.add(mesh=MESH_B)
    out = _client(tmp_path).notifications.read_all(group="mentions", apply=True)
    assert out["ids"] == [mention] and not fake.row(weird).is_read


def test_read_all_apply_reports_a_silent_miss(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    a, b = fake.add(mesh=MESH_B), fake.add(mesh=MESH_B)
    fake.drop(a)
    out = _client(tmp_path).notifications.read_all(apply=True)
    statuses = {r["id"]: r["status"] for r in out["outcome"]["results"]}
    assert statuses == {a: "unconfirmed", b: "applied"} and out["outcome"]["ok"] is False


# ─── summary ─────────────────────────────────────────────────────────────


def test_summary_names_its_mesh_and_splits_out_cross_mesh(fake, client):
    fake.add(mesh=MESH_B)
    fake.add(cross_mesh=True, kind="MeshInvitationReceived")
    s = client.notifications.summary()
    assert (s["unread"], s["unreadCrossMesh"], s["unreadInMesh"]) == (2, 1, 1)
    assert s["meshId"] == MESH_B and s["allMeshes"] is False and s["invites"] == 1


def test_summary_all_meshes_counts_cross_mesh_rows_once(fake, client):
    fake.add_many(3, mesh=MESH_A)
    fake.add(mesh=MESH_B)
    fake.add(mesh=MESH_B, is_read=True)
    fake.add(cross_mesh=True, kind="MeshInvitationReceived")
    fake.add(cross_mesh=True, kind="StorageThresholdWarning")
    naive = sum(client.notifications.summary(mesh=m)["unread"] for m in (MESH_A, MESH_B, MESH_C))
    assert naive == 10  # what summing per-mesh answers would claim
    s = client.notifications.summary(all_meshes=True)
    assert (s["unread"], s["unreadCrossMesh"], s["unreadInMesh"], s["total"]) == (6, 2, 4, 7)
    assert (s["mentions"], s["invites"], s["system"]) == (4, 1, 1)
    assert s["complete"] is True and s["skipped"] == []
    assert [(m["name"], m["unreadInMesh"]) for m in s["meshes"]] == [
        ("Alpha", 3),
        ("Beta", 1),
        ("Gamma", 0),
    ]
    no_header = [r for r in fake.requests(route="notifications.summary") if r.mesh is None]
    assert len(no_header) == 1


def test_summary_all_meshes_says_what_it_could_not_count(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old", scope_meshes={MESH_A, MESH_B}).install(monkeypatch)
    fake.add(mesh=MESH_C)
    s = _client(tmp_path).notifications.summary(all_meshes=True)
    assert s["complete"] is False
    assert s["skipped"] == [{"meshId": MESH_C, "name": "Gamma", "code": "token_out_of_scope"}]


@pytest.mark.parametrize("kwargs", [{"mesh": MESH_A, "all_meshes": True}, {"all_meshes": "yes"}])
def test_summary_refuses_bad_arguments(fake, client, kwargs):
    with pytest.raises(MeshbookError) as ei:
        client.notifications.summary(**kwargs)
    assert ei.value.code == "bad_argument" and fake.log == []


# ─── the fake keeps faith with the server (REPORT-SERVER-106) ────────────


def test_fake_visibility_paging_and_envelope(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    fake.add_many(60, mesh=MESH_A)
    fake.add(mesh=MESH_B)
    fake.add(cross_mesh=True)
    fake.add(mesh=MESH_A, dismissed=True)
    fake.add(mesh=MESH_A, user=OTHER_USER_ID)
    c = _client(tmp_path, mesh=None)
    assert len(c.notifications.list()) == 1  # no mesh: cross-mesh only
    page = c.notifications.list(mesh=MESH_A)
    assert len(page) == 50 and page.total == 50  # default 50; total is the PAGE length
    assert len(c.notifications.list(mesh=MESH_A, limit=200)) == 61
    with pytest.raises(MeshbookError) as ei:
        c.notifications.list(mesh=MESH_A, limit=201)
    assert ei.value.code == "validation_error"


def test_fake_old_server_coerces_and_lies(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="old").install(monkeypatch)
    nid = fake.add(mesh=MESH_A, is_read=True)
    c = _client(tmp_path)  # mesh B: the PATCH misses
    assert c.request("PATCH", f"/api/notifications/{nid}", body={"isRead": False})["data"] == {
        "id": nid,
        "updated": True,
    }
    assert fake.row(nid).is_read  # nothing changed
    c.request("PATCH", f"/api/notifications/{nid}", body={"isRead": None}, mesh=MESH_A)
    assert fake.row(nid).is_read is False and fake.row(nid).read_at is not None  # bool(None)
    assert c.request("PATCH", f"/api/notifications/{nid}", body={})["data"] == {"noop": True}


def test_fake_fixed_server_contract(monkeypatch, tmp_path):
    fake = FakeNotificationServer(mode="fixed").install(monkeypatch)
    nid = fake.add(mesh=MESH_A)
    c = _client(tmp_path, mesh=MESH_A)
    path = f"/api/notifications/{nid}"
    for bad in (
        {"isRead": None},
        {"isRead": "false"},
        {"isRead": 1},
        {"dismiss": False},
        {"isRead": True, "is_read": False},
    ):
        with pytest.raises(MeshbookError) as ei:
            c.request("PATCH", path, body=bad)
        assert (ei.value.status, ei.value.code) == (422, "validation_error")
    assert not fake.row(nid).is_read
    first = c.request("PATCH", path, body={"isRead": True})["data"]
    assert first["isRead"] is True and first["updated"] is True and first["dismissedAt"] is None
    again = c.request("PATCH", path, body={"isRead": True})["data"]
    assert again["readAt"] == first["readAt"]  # set only on a change
    assert c.request("PATCH", path, body={"isRead": False})["data"]["readAt"] is None
    d1 = c.request("PATCH", path, body={"dismiss": True})["data"]["dismissedAt"]
    d2 = c.request("PATCH", path, body={"dismiss": True})["data"]["dismissedAt"]
    assert d1 and d1 == d2  # a dismissed row is still reachable; first stamp kept
    with pytest.raises(MeshbookError) as ei:
        c.request("PATCH", path, body={"isRead": True}, mesh=MESH_B)
    assert (ei.value.status, ei.value.code, ei.value.hint) == (404, "notification_not_found", None)
    with pytest.raises(MeshbookError) as ei:
        c.request("GET", "/api/notifications", params={"group": "invitations"})
    assert ei.value.code == "unknown_notification_group"


@pytest.mark.raw_request
def test_fake_read_all_in_both_modes(monkeypatch, tmp_path):
    for mode_ in ("old", "fixed"):
        fake = FakeNotificationServer(mode=mode_).install(monkeypatch)
        fake.add(mesh=MESH_A)
        task = fake.add(mesh=MESH_A, kind="TaskAssigned")
        c = _client(tmp_path, mesh=MESH_A)
        if mode_ == "old":
            # The trap C-03 names: an unknown group filters NOTHING.
            assert c.request(
                "POST", "/api/notifications/read-all", params={"group": "invitations"}
            )["data"] == {"marked": True}
            assert fake.row(task).is_read
        else:
            with pytest.raises(MeshbookError) as ei:
                c.request("POST", "/api/notifications/read-all", params={"group": "invitations"})
            assert ei.value.code == "unknown_notification_group" and not fake.row(task).is_read
            assert c.request("POST", "/api/notifications/read-all")["data"] == {
                "marked": True,
                "count": 2,
            }


# ─── docs, constants and drift ───────────────────────────────────────────


def test_statuses_and_groups_are_documented():
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    for status in NOTIFICATION_STATUSES:
        assert f"| `{status}` |" in readme, f"status {status} missing from the README"
    for group in NOTIFICATION_GROUP_KINDS:
        assert f"`{group}`" in readme
    assert Notifications.STATUSES is NOTIFICATION_STATUSES
    assert set(nmod.SETTLED_STATUSES) <= set(NOTIFICATION_STATUSES)


def test_group_kinds_match_a_server_checkout_beside_this_repo():
    server = (
        Path(__file__).resolve().parents[2] / "meshbook" / "app" / "routers" / "notifications.py"
    )
    if not server.is_file():
        pytest.skip("no meshbook server checkout beside this repo")
    tree = ast.parse(server.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "GROUP_KINDS" for t in node.targets
        ):
            theirs = ast.literal_eval(node.value)
            break
    else:
        pytest.fail("GROUP_KINDS not found in the server router")
    assert {k: tuple(v) for k, v in theirs.items()} == NOTIFICATION_GROUP_KINDS


# ─── the two judgements everything rests on ─────────────────────────────


@pytest.mark.parametrize("action", ["mark_read", "mark_unread", "dismiss"])
def test_a_bare_updated_proves_nothing(action):
    nid = str(uuid.uuid4())
    assert nmod._judge({"id": nid, "updated": True}, action) == "unknown"
    assert nmod._judge({"noop": True}, action) == "unknown"
    assert nmod._judge(None, action) == "unknown"


def test_an_echoed_row_is_judged_by_its_state():
    echo = {"id": "x", "updated": True, "isRead": True, "readAt": "t", "dismissedAt": None}
    assert nmod._judge(echo, "mark_read") == "applied"
    assert nmod._judge(echo, "mark_unread") == "mismatch"
    assert nmod._judge(echo, "dismiss") == "mismatch"
    assert nmod._judge({**echo, "dismissedAt": "t"}, "dismiss") == "applied"
    assert nmod._judge({**echo, "isRead": "true"}, "mark_read") == "unknown"  # not a bool


def test_is_read_prefers_is_read_over_read_at():
    assert nmod._is_read({"isRead": False, "readAt": "2026-05-01T00:00:00+00:00"}) is False
    assert nmod._is_read({"isRead": True, "readAt": None}) is True
    assert nmod._is_read({"readAt": "2026-05-01T00:00:00+00:00"}) is True  # older shape only
    assert nmod._is_read({"readAt": None}) is False
    assert nmod._is_read({}) is None


def test_the_public_read_state_helper_follows_the_same_rule():
    from meshbook.resources import notification_is_read

    stale = {"isRead": False, "readAt": "2026-05-01T00:00:00+00:00"}
    assert notification_is_read(stale) is False and notification_is_read({"readAt": None}) is False
