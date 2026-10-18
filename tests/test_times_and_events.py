"""Times (contract: "Times") and calendar events (CALENDAR-CONTRACT.md).

Naive datetimes must be refused BEFORE the wire, every time: the web page
sent naive strings and a 09:00 Sydney meeting landed at 20:00 (contract
D6). The events tests then pin the exact wire for every route.
"""
from __future__ import annotations

import datetime as dt

import pytest

from meshbook import MeshbookClient, MeshbookError, recurrence_rule
from meshbook._times import due_fields, iso_date, iso_datetime
from tests._fakes import C1, E1, MESH, U1, U2, install, ok, ok_list

SYD = dt.timezone(dt.timedelta(hours=11))
START = dt.datetime(2026, 10, 20, 9, 0, tzinfo=SYD)
END = dt.datetime(2026, 10, 20, 10, 0, tzinfo=SYD)
EV = f"/api/calendar-events/{E1}"


@pytest.fixture
def client(tmp_path):
    return MeshbookClient(token="mb_token_test", mesh=MESH, config_path=tmp_path / "none")


# ─── the time rules ────────────────────────────────────────────────────


def test_aware_datetime_and_offset_strings_pass():
    assert iso_datetime(START, "start") == "2026-10-20T09:00:00+11:00"
    assert iso_datetime("2026-10-20T09:00:00Z", "start") == "2026-10-20T09:00:00+00:00"
    assert iso_datetime("2026-10-20T09:00:00+11:00", "start") == "2026-10-20T09:00:00+11:00"


@pytest.mark.parametrize("value", [
    dt.datetime(2026, 10, 20, 9, 0),
    "2026-10-20T09:00:00",
])
def test_naive_is_refused_with_a_clear_error(value):
    with pytest.raises(MeshbookError) as ei:
        iso_datetime(value, "start")
    assert ei.value.code == "naive_datetime"
    assert "time zone" in ei.value.message and "offset" in ei.value.message


@pytest.mark.parametrize("value,code", [
    ("tomorrow", "bad_datetime"),
    (dt.date(2026, 10, 20), "bad_datetime"),
    (12345, "bad_datetime"),
])
def test_bad_datetimes(value, code):
    with pytest.raises(MeshbookError) as ei:
        iso_datetime(value, "start")
    assert ei.value.code == code


def test_dates():
    assert iso_date(dt.date(2026, 12, 31), "until") == "2026-12-31"
    assert iso_date("2026-12-31", "until") == "2026-12-31"
    for bad in ("31/12/2026", "2026-02-30", START):
        with pytest.raises(MeshbookError) as ei:
            iso_date(bad, "until")
        assert ei.value.code == "bad_date"


def test_due_fields():
    assert due_fields(dt.date(2026, 10, 31)) == {"dueDate": "2026-10-31"}
    assert due_fields("2026-10-31") == {"dueDate": "2026-10-31"}
    assert due_fields(START) == {"dueAt": "2026-10-20T09:00:00+11:00"}
    assert due_fields(None) == {"dueDate": None, "dueAt": None}
    with pytest.raises(MeshbookError) as ei:
        due_fields(dt.datetime(2026, 10, 31, 17, 0))
    assert ei.value.code == "naive_datetime"


@pytest.mark.parametrize("call", [
    lambda c: c.events.create("x", dt.datetime(2026, 1, 1, 9), END),
    lambda c: c.events.update(E1, end=dt.datetime(2026, 1, 1, 9)),
    lambda c: c.events.list(dt.datetime(2026, 1, 1), END),
    lambda c: c.tasks.create("44444444-4444-4444-4444-444444444444", "t",
                             due=dt.datetime(2026, 1, 1, 9)),
    lambda c: c.tasks.update("t1", due=dt.datetime(2026, 1, 1, 9)),
])
def test_naive_never_reaches_the_wire(client, monkeypatch, call):
    r = install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        call(client)
    assert ei.value.code == "naive_datetime"
    assert r.requests == []


# ─── events: list ──────────────────────────────────────────────────────


def test_events_list_wire_with_expand_tz_and_owner(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", ok_list([{"id": U1, "username": "wren", "displayName": "Wren"}]))
    r.add("GET", "/api/calendar-events", ok_list(
        [{"id": E1, "occurrenceStart": "x", "seriesId": E1}], total=1, truncated=False))
    rows = client.events.list(START, END, owner="@wren", expand=True, tz="Australia/Sydney",
                              linked_type="task", linked_id=U2, limit=20, offset=0)
    q = r.query()
    assert q == {
        "limit": "20", "offset": "0",
        "from": "2026-10-20T09:00:00+11:00", "to": "2026-10-20T10:00:00+11:00",
        "linkedType": "task", "linkedId": U2, "ownerId": U1,
        "expand": "true", "tz": "Australia/Sydney",
    }
    assert r.last.get_header("X-active-mesh-id") == MESH
    assert rows[0]["seriesId"] == E1 and rows.truncated is False


def test_events_list_without_expand_sends_no_expand(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/calendar-events", ok_list([]))
    client.events.list()
    assert r.query() == {}


def test_expand_needs_a_window(client, monkeypatch):
    r = install(monkeypatch)
    with pytest.raises(MeshbookError) as ei:
        client.events.list(START, expand=True)
    assert ei.value.code == "bad_argument"
    assert r.requests == []


# ─── events: create ────────────────────────────────────────────────────


def test_events_create_wire(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", "/api/users", ok_list([{"id": U2, "username": "alder", "displayName": "Alder"}]))
    r.add("GET", "/api/contacts", ok_list([{"id": C1, "displayName": "Ada Lovelace"}]))
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    out = client.events.create(
        "Board sync", START, END,
        description="Q4", location="Gosford", meeting_url="https://meet.example/x",
        attendees=["user:alder", "contact:Ada Lovelace", {"email": "g@example.org", "name": "Guest"}],
        recurrence={"frequency": "fortnightly", "until": dt.date(2026, 12, 31)},
        linked_type="task", linked_id=U1, color="#112233",
    )
    assert out["id"] == E1
    assert r.body() == {
        "title": "Board sync",
        "startAt": "2026-10-20T09:00:00+11:00",
        "endAt": "2026-10-20T10:00:00+11:00",
        "description": "Q4",
        "isAllDay": False,
        "eventType": "Meeting",  # the server requires one; parity with @meshbook/sdk
        "location": "Gosford",
        "meetingUrl": "https://meet.example/x",
        "linkedEntityType": "task",
        "linkedEntityId": U1,
        "colorHex": "#112233",
        "attendees": [
            {"userId": U2},
            {"contactId": C1},
            {"externalEmail": "g@example.org", "externalName": "Guest"},
        ],
        "recurrence": {"frequency": "Weekly", "intervalN": 2, "untilDate": "2026-12-31"},
    }


def test_events_create_minimal_and_validation(client, monkeypatch):
    r = install(monkeypatch)
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    client.events.create("Standup", START, END, event_type="call")
    assert r.body() == {"title": "Standup", "startAt": "2026-10-20T09:00:00+11:00",
                        "endAt": "2026-10-20T10:00:00+11:00", "isAllDay": False,
                        "eventType": "Call"}
    for kwargs in ({"event_type": "Party"}, {"linked_type": "task"},
                   {"attendees": ["user:"]}, {"attendees": [{"user": U1, "contact": C1}]}):
        with pytest.raises(MeshbookError) as ei:
            client.events.create("x", START, END, **kwargs)
        assert ei.value.code == "bad_argument", kwargs
    with pytest.raises(MeshbookError) as ei:
        client.events.create("x", END, START)
    assert ei.value.code == "bad_argument" and "before" in ei.value.message
    assert len(r.requests) == 1


def test_events_create_dedupe_guard(client, monkeypatch):
    r = install(monkeypatch)
    existing = {"id": "old", "title": "  board   SYNC ", "startAt": "2026-10-19T22:00:00+00:00"}
    other = {"id": "o2", "title": "Board sync", "startAt": "2026-10-19T21:30:00+00:00"}
    r.add("GET", "/api/calendar-events", ok_list([existing, other]))
    r.add("POST", "/api/calendar-events", ok({"id": E1}))
    with pytest.raises(MeshbookError) as ei:
        client.events.create("Board sync", START, END, dedupe=True)
    assert ei.value.code == "duplicate_event" and "old" in ei.value.message
    assert "o2" not in ei.value.message, "a different start instant is not a duplicate"
    assert r.calls("POST", "/api/calendar-events") == []
    client.events.create("Board sync", START, END)  # dedupe off: creates
    assert len(r.calls("POST", "/api/calendar-events")) == 1


# ─── events: get / update / delete ─────────────────────────────────────


def test_events_update_sends_only_what_was_passed_and_none_clears(client, monkeypatch):
    r = install(monkeypatch)
    r.add("PATCH", EV, ok({"id": E1}))
    client.events.update(E1, title="Renamed", location=None, recurrence=None,
                         linked_type=None, linked_id=None, attendees=[])
    assert r.last.get_method() == "PATCH"
    assert r.body() == {"title": "Renamed", "location": None, "linkedEntityType": None,
                        "linkedEntityId": None, "attendees": [], "recurrence": None}


def test_events_update_refuses_clearing_required_fields_and_empty_patches(client, monkeypatch):
    r = install(monkeypatch)
    for kwargs in ({"title": None}, {"start": None}, {}, {"linked_type": None}):
        with pytest.raises(MeshbookError) as ei:
            client.events.update(E1, **kwargs)
        assert ei.value.code == "bad_argument", kwargs
    assert r.requests == []


def test_events_get_and_delete(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", EV, ok({"id": E1, "attendees": [], "recurrence": None}))
    r.add("DELETE", EV, ok({"id": E1, "deleted": True}))
    assert client.events.get(E1)["recurrence"] is None
    assert client.events.delete(E1) == {"id": E1, "deleted": True}


# ─── recurrence ────────────────────────────────────────────────────────


def test_recurrence_rule_shapes():
    assert recurrence_rule("weekly") == {"frequency": "Weekly", "intervalN": 1, "untilDate": None}
    assert recurrence_rule("Monthly", 1, count=6, by_weekday=["3th", "-1FR"]) == {
        "frequency": "Monthly", "intervalN": 1, "untilDate": None, "countN": 6,
        "byWeekday": ["3TH", "-1FR"]}
    for bad in (lambda: recurrence_rule("hourly"), lambda: recurrence_rule("daily", 0),
                lambda: recurrence_rule("daily", until="2026-12-31", count=3),
                lambda: recurrence_rule("weekly", by_weekday=["XX"]),
                lambda: recurrence_rule("weekly", by_weekday=[]),
                # CALENDAR-CONTRACT 3: ordinals and byMonthDay are Monthly-only,
                # and byMonthDay never comes with byWeekday.
                lambda: recurrence_rule("weekly", by_weekday=["3TH"]),
                lambda: recurrence_rule("weekly", by_month_day=[1]),
                lambda: recurrence_rule("monthly", by_month_day=[1], by_weekday=["MO"]),
                lambda: recurrence_rule("monthly", by_month_day=[0]),
                lambda: recurrence_rule("monthly", by_weekday=["9TH"])):
        with pytest.raises(MeshbookError) as ei:
            bad()
        assert ei.value.code == "bad_argument"


def test_monthly_by_month_day_and_last_friday():
    assert recurrence_rule("monthly", by_month_day=[1, -1])["byMonthDay"] == [1, -1]
    assert recurrence_rule("monthly", by_weekday=["-1fr"])["byWeekday"] == ["-1FR"]


def test_not_a_member_detail_shape_maps_to_its_code(client, monkeypatch):
    """D7: the membership gate answers in the dependency `detail` shape."""
    from tests._fakes import Err

    r = install(monkeypatch)
    r.add("GET", "/api/calendar-events",
          Err(403, {"detail": {"code": "not_a_member", "message": "Not a member of this mesh."}}))
    with pytest.raises(MeshbookError) as ei:
        client.events.list()
    assert (ei.value.status, ei.value.code) == (403, "not_a_member")


def test_set_and_clear_recurrence_wire(client, monkeypatch):
    r = install(monkeypatch)
    r.add("PUT", f"{EV}/recurrence", ok({"frequency": "Daily"}))
    r.add("DELETE", f"{EV}/recurrence", ok({"id": "r", "eventId": E1, "deleted": True}))
    client.events.set_recurrence(E1, "daily", 2, until=dt.date(2026, 11, 30))
    assert r.body() == {"frequency": "Daily", "intervalN": 2, "untilDate": "2026-11-30"}
    assert client.events.clear_recurrence(E1)["deleted"] is True


# ─── attendees ─────────────────────────────────────────────────────────


def test_attendee_routes_wire(client, monkeypatch):
    r = install(monkeypatch)
    r.add("GET", f"{EV}/attendees", ok_list([{"id": "at1", "userId": U1}]))
    r.add("POST", f"{EV}/attendees", ok({"id": "at2"}))
    r.add("PATCH", f"{EV}/attendees/at2", ok({"id": "at2", "response": "Accepted"}))
    r.add("DELETE", f"{EV}/attendees/at2", ok({"id": "at2", "deleted": True}))
    assert client.events.attendees(E1)[0]["userId"] == U1
    client.events.add_attendee(E1, user=U2, organiser=True)
    assert r.body() == {"userId": U2, "isOrganiser": True}
    client.events.add_attendee(E1, contact=C1, response="tentative")
    assert r.body() == {"contactId": C1, "response": "Tentative"}
    client.events.update_attendee(E1, "at2", response="accepted", optional=True)
    assert r.body() == {"response": "Accepted", "isOptional": True}
    assert client.events.remove_attendee(E1, "at2")["deleted"] is True
    with pytest.raises(MeshbookError):
        client.events.add_attendee(E1)
    with pytest.raises(MeshbookError):
        client.events.update_attendee(E1, "at2")
