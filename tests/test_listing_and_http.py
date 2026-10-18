"""ItemList, pagination on every list method, and the HTTP behaviour rules
(contract: "Listings", "HTTP behaviour").

The retry tests pin the asymmetry that matters: GET/HEAD/PUT/DELETE may be
retried on 429/503, POST/PATCH never — a retried write the server had in
fact applied would apply twice.
"""
from __future__ import annotations

import email.utils
import inspect
import time

import pytest

import meshbook.client as mc
from meshbook import ItemList, MeshbookClient, MeshbookError
from meshbook import resources as R
from tests._fakes import MESH, Err, install, ok, ok_list


@pytest.fixture
def client(tmp_path):
    return MeshbookClient(token="mb_token_test", mesh=MESH, config_path=tmp_path / "none")


# ─── ItemList ──────────────────────────────────────────────────────────


def test_itemlist_keeps_total_limit_offset_and_meta(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/contacts", ok_list([{"id": 1}, {"id": 2}], total=57, page=2, pageSize=2))
    rows = client.contacts.list(limit=2, offset=2)
    assert isinstance(rows, ItemList) and isinstance(rows, list)
    assert rows == [{"id": 1}, {"id": 2}]
    assert (rows.total, rows.limit, rows.offset) == (57, 2, 2)
    assert rows.meta["page"] == 2
    assert rows.has_more is True
    assert r.query() == {"limit": "2", "offset": "2"}


def test_bare_list_endpoints_are_itemlists_with_total_none(client, monkeypatch):
    """§102.8: one envelope everywhere, even where the server sends a bare list."""
    r = install(monkeypatch)
    r.add("GET", f"/api/entities/task/{MESH}/tags", ok([{"id": "t", "name": "x"}]))
    tags = client.tags.of("task", MESH)
    assert isinstance(tags, ItemList)
    assert tags.total is None and tags == [{"id": "t", "name": "x"}]


def test_server_ignoring_limit_is_paged_client_side(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", ok_list([{"id": i} for i in range(10)]))
    rows = client.users.list(limit=3, offset=4)
    assert [u["id"] for u in rows] == [4, 5, 6]
    assert rows.total == 10 and rows.meta["client_paginated"] is True


def test_server_envelope_limit_offset_win(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/calendar-events",
          ok_list([{"id": "e"}], total=1, limit=50, offset=0, truncated=True))
    rows = client.events.list()
    assert rows.limit == 50 and rows.offset == 0
    assert rows.truncated is True and rows.has_more is True


def test_bad_pagination_values_are_refused_locally(client, monkeypatch):
    r = install(monkeypatch)
    for kwargs in ({"limit": 0}, {"limit": -1}, {"offset": -1}, {"limit": True}):
        with pytest.raises(MeshbookError) as ei:
            client.contacts.list(**kwargs)
        assert ei.value.code == "bad_argument"
    assert r.requests == []


def test_old_items_helper_still_works():
    assert mc._items({"ok": True, "data": {"items": [1], "total": 9}}).total == 9


# Methods that return an ItemList but are not listings of a collection.
_NOT_LISTINGS = {"Channels.add_member", "Tags.add", "Tags.set", "Events.find_duplicates"}


def _list_methods():
    for name in R.__all__:
        cls = getattr(R, name)
        if not inspect.isclass(cls) or not issubclass(cls, R.Namespace) or cls is R.Namespace:
            continue
        for meth_name, fn in inspect.getmembers(cls, inspect.isfunction):
            if meth_name.startswith("_"):
                continue
            sig = inspect.signature(fn)
            if "ItemList" in str(sig.return_annotation) or "SearchResults" in str(
                sig.return_annotation
            ):
                yield f"{cls.__name__}.{meth_name}", sig


def test_every_list_method_takes_limit_and_offset():
    found = dict(_list_methods())
    assert len(found) >= 25, sorted(found)  # the reflection is actually finding them
    missing = [
        name for name, sig in found.items()
        if name not in _NOT_LISTINGS
        and not {"limit", "offset"} <= set(sig.parameters)
    ]
    assert missing == []


def test_every_mesh_scoped_method_takes_mesh():
    """Contract: a per-call mesh= on EVERY mesh-scoped method."""
    account_level = {"Meshes", "Exports"}  # mesh lists / explicit mesh args
    lacking = []
    for name in R.__all__:
        cls = getattr(R, name)
        if not inspect.isclass(cls) or not issubclass(cls, R.Namespace) or cls is R.Namespace:
            continue
        if cls.__name__ in account_level:
            continue
        for meth_name, fn in inspect.getmembers(cls, inspect.isfunction):
            if meth_name.startswith("_"):
                continue
            if "mesh" not in inspect.signature(fn).parameters:
                lacking.append(f"{cls.__name__}.{meth_name}")
    assert lacking == []


# ─── retries ───────────────────────────────────────────────────────────


def test_429_with_retry_after_seconds_is_retried(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/contacts",
          Err(429, {"error": {"code": "rate_limited", "message": "slow"}}, {"Retry-After": "2"}),
          ok_list([{"id": 1}]))
    assert client.contacts.list() == [{"id": 1}]
    assert len(r.requests) == 2
    assert r.sleeps == [2.0]


def test_retry_after_http_date_and_the_30s_cap(client, monkeypatch):
    soon = email.utils.formatdate(time.time() + 5, usegmt=True)
    later = email.utils.formatdate(time.time() + 3600, usegmt=True)
    r = install(monkeypatch)
    r.add("GET", "/api/leads", Err(503, {}, {"Retry-After": soon}),
          Err(503, {}, {"Retry-After": later}), ok_list([]))
    client.leads.list()
    assert 3.0 <= r.sleeps[0] <= 5.5
    assert r.sleeps[1] == 30.0, "Retry-After is capped at 30 s"


def test_503_without_retry_after_backs_off(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/tasks", Err(503), Err(503), ok_list([]))
    client.tasks.list()
    assert r.sleeps == [0.5, 1.0]


def test_at_most_three_attempts_then_the_error(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/contacts", Err(429, {"error": {"code": "rate_limited", "message": "no"}}))
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.status == 429 and ei.value.code == "rate_limited"
    assert len(r.requests) == 3


@pytest.mark.parametrize("method_call,method,path", [
    (lambda c: c.contacts.create("Ada"), "POST", "/api/contacts"),
    (lambda c: c.tasks.done("t1"), "PATCH", "/api/tasks/t1"),
])
def test_post_and_patch_are_never_retried(client, monkeypatch, method_call, method, path):
    r = install(monkeypatch)
    r.add(method, path, Err(503, {}, {"Retry-After": "1"}), ok({"id": "x"}))
    with pytest.raises(MeshbookError) as ei:
        method_call(client)
    assert ei.value.status == 503
    assert len(r.requests) == 1 and r.sleeps == []


@pytest.mark.parametrize("method_call,method,path", [
    (lambda c: c.files.delete("a1"), "DELETE", "/api/entity-attachments/a1"),
    (lambda c: c.events.set_recurrence("e1", "weekly"), "PUT", "/api/calendar-events/e1/recurrence"),
])
def test_put_and_delete_are_retried(client, monkeypatch, method_call, method, path):
    r = install(monkeypatch)
    r.add(method, path, Err(429, {}, {"Retry-After": "0"}), ok({"ok": 1}))
    method_call(client)
    assert len(r.requests) == 2


def test_max_attempts_is_configurable(tmp_path, monkeypatch):
    c = MeshbookClient(token="t", mesh=MESH, config_path=tmp_path / "none", max_attempts=1)
    r = install(monkeypatch)
    r.add("GET", "/api/contacts", Err(429), ok_list([]))
    with pytest.raises(MeshbookError):
        c.contacts.list()
    assert len(r.requests) == 1


def test_downloads_are_retried_and_use_the_download_timeout(tmp_path, monkeypatch):
    c = MeshbookClient(token="t", mesh=MESH, config_path=tmp_path / "none", timeout=5,
                       download_timeout=77)
    seen = []
    r = install(monkeypatch)
    r.add("GET", "/api/entity-attachments/a1/download", Err(503), b"BYTES")
    real = mc._urlopen

    def spy(req, timeout):
        seen.append(timeout)
        return real(req, timeout)

    monkeypatch.setattr(mc, "_urlopen", spy)
    out = c.files.download("a1", tmp_path / "f.bin")
    assert out.read_bytes() == b"BYTES"
    assert seen == [77, 77]


def test_network_error_is_typed(client, monkeypatch):
    import urllib.error

    def down(req, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(mc, "_urlopen", down)
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.code == "network_error" and ei.value.status == 0


def test_fastapi_detail_list_is_mapped_to_validation_error(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/contacts",
          Err(422, {"detail": [{"loc": ["body", "firstName"], "msg": "field required"}]}))
    with pytest.raises(MeshbookError) as ei:
        client.contacts.create("")
    assert ei.value.code == "validation_error"
    assert "firstName" in ei.value.message


def test_non_json_200_is_a_typed_error(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/contacts", b"<html>cloudflare</html>")
    with pytest.raises(MeshbookError) as ei:
        client.contacts.list()
    assert ei.value.code == "bad_response"
