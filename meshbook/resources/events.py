"""Calendar events — the app-native mesh calendar (CALENDAR-CONTRACT.md, 2026-10-19).

Mesh-scoped like everything else: per-call ``mesh=`` > ``Client(mesh=)`` >
``MESHBOOK_MESH`` > config. Every datetime is sent ISO 8601 WITH an offset
(naive datetimes are refused before the wire); dates are ``YYYY-MM-DD``.
No Microsoft Graph anywhere — these are meshbook's own events.

Routes marked "pending" in :mod:`meshbook._endpoints` (PATCH, recurrence,
attendees) are built to the server packet's final contract and land on
prod when the server is deployed.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping
from typing import Any

from .._times import aware, iso_date, iso_datetime
from ..client import _is_uuid
from ..errors import MeshbookError
from ..models import ItemList
from ._base import UNSET, Namespace, as_dict, merge_fields, put

EVENT_TYPES = ("Meeting", "Call", "Milestone", "Deadline", "Internal", "External", "Training")
FREQUENCIES = ("Daily", "Weekly", "Monthly", "Yearly")
RESPONSES = ("NoResponse", "Accepted", "Declined", "Tentative")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def recurrence_rule(
    frequency: str,
    interval: int = 1,
    until: Any = None,
    *,
    count: int | None = None,
    by_weekday: Iterable[str] | None = None,
    by_month_day: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Build a RecurrenceIn body.

    ``frequency``: Daily / Weekly / Monthly / Yearly (any case), or
    ``fortnightly`` (= Weekly every 2). ``until`` (a date, inclusive) and
    ``count`` (occurrences, the first included) are mutually exclusive.
    ``by_weekday``: ``MO``…``SU`` (Monthly may carry an ordinal: ``3TH``,
    ``-1FR``); ``by_month_day``: 1..31 or -31..-1, Monthly only.
    """
    freq = (frequency or "").strip().lower()
    if freq == "fortnightly":
        freq, interval = "weekly", interval * 2
    canon = {f.lower(): f for f in FREQUENCIES}.get(freq)
    if canon is None:
        raise MeshbookError(
            "bad_argument",
            f"frequency must be one of Daily, Weekly, Monthly, Yearly (or fortnightly), got {frequency!r}.",
        )
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 999:
        raise MeshbookError("bad_argument", f"interval must be an int in 1..999, got {interval!r}.")
    if until is not None and count is not None:
        raise MeshbookError("bad_argument", "Pass until= or count=, not both.")
    body: dict[str, Any] = {"frequency": canon, "intervalN": interval}
    body["untilDate"] = iso_date(until, "until") if until is not None else None
    if count is not None:
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1000:
            raise MeshbookError("bad_argument", f"count must be an int in 1..1000, got {count!r}.")
        body["countN"] = count
    if by_weekday is not None and by_month_day is not None:
        raise MeshbookError("bad_argument", "Pass by_weekday= or by_month_day=, not both.")
    if by_weekday is not None:
        days = [str(d).strip().upper() for d in by_weekday if str(d).strip()]
        if not days:
            raise MeshbookError("bad_argument", "by_weekday must not be empty.")
        for d in days:
            ordinal = d[:-2]
            if d[-2:] not in WEEKDAYS or (ordinal and not _is_ordinal(ordinal)):
                raise MeshbookError("bad_argument", f"Unknown weekday {d!r} (use MO..SU, or 3TH / -1FR).")
            if ordinal and canon != "Monthly":
                raise MeshbookError(
                    "bad_argument", f"An ordinal weekday ({d}) only makes sense with Monthly."
                )
        body["byWeekday"] = days
    if by_month_day is not None:
        if canon != "Monthly":
            raise MeshbookError("bad_argument", "by_month_day only makes sense with Monthly.")
        mdays = list(by_month_day)
        if not mdays:
            raise MeshbookError("bad_argument", "by_month_day must not be empty.")
        for md in mdays:
            if not isinstance(md, int) or isinstance(md, bool) or not (1 <= abs(md) <= 31):
                raise MeshbookError("bad_argument", f"by_month_day values are 1..31 or -31..-1, got {md!r}.")
        body["byMonthDay"] = mdays
    return body


def _is_ordinal(text: str) -> bool:
    """``3`` or ``-1`` (the nth / nth-from-last weekday of the month)."""
    t = text[1:] if text.startswith(("-", "+")) else text
    return t.isdigit() and 1 <= int(t) <= 5


def _recurrence_body(value: Any) -> Any:
    """``recurrence=`` on create/update: None (clear), a dict, or a frequency string."""
    if value is None:
        return None
    if isinstance(value, str):
        return recurrence_rule(value)
    if isinstance(value, Mapping):
        v = dict(value)
        if "intervalN" in v or "untilDate" in v or "countN" in v:
            return v  # already wire-shaped
        return recurrence_rule(
            str(v.get("frequency", "")),
            int(v.get("interval", 1)),
            v.get("until"),
            count=v.get("count"),
            by_weekday=v.get("by_weekday"),
            by_month_day=v.get("by_month_day"),
        )
    raise MeshbookError("bad_argument", "recurrence must be a dict, a frequency string, or None.")


class Events(Namespace):
    """``client.events`` — calendar events in a mesh."""

    # ── attendee helpers ──────────────────────────────────────────────

    def _attendee(self, item: Any, mesh: str | None) -> dict[str, Any]:
        """One attendee spec → AttendeeIn.

        Accepts ``"user:REF"`` / ``"contact:REF"`` / ``"email:ADDRESS"``
        strings (the CLI's ``--attendee`` syntax; REF = UUID or name), or a
        dict with ``user`` / ``contact`` / ``email`` (or the wire keys
        ``userId`` / ``contactId`` / ``externalEmail``) plus optional
        ``organiser``/``isOrganiser``, ``optional``/``isOptional``,
        ``response``, ``name``/``externalName``.
        """
        if isinstance(item, str):
            kind, _, ref = item.partition(":")
            if not ref:
                raise MeshbookError(
                    "bad_argument", f"Attendee {item!r} must look like user:NAME, contact:NAME or email:ADDR."
                )
            item = {kind.strip().lower(): ref.strip()}
        if not isinstance(item, Mapping):
            raise MeshbookError("bad_argument", f"Unsupported attendee spec: {item!r}")
        out: dict[str, Any] = {}
        user = item.get("user") or item.get("userId")
        contact = item.get("contact") or item.get("contactId")
        email = item.get("email") or item.get("externalEmail")
        refs = [r for r in (user, contact, email) if r]
        if len(refs) != 1:
            raise MeshbookError(
                "bad_argument", "An attendee needs exactly one of user, contact or email."
            )
        if user:
            out["userId"] = self._user_id(str(user), mesh=mesh)
        elif contact:
            c = str(contact)
            out["contactId"] = c if _is_uuid(c) else str(self._c.contacts.resolve(c, mesh=mesh)["id"])
        else:
            out["externalEmail"] = str(email)
            name = item.get("name") or item.get("externalName")
            if name:
                out["externalName"] = str(name)
        organiser = item.get("organiser", item.get("isOrganiser"))
        if organiser is not None:
            out["isOrganiser"] = bool(organiser)
        optional = item.get("optional", item.get("isOptional"))
        if optional is not None:
            out["isOptional"] = bool(optional)
        if item.get("response"):
            out["response"] = _response(str(item["response"]))
        return out

    def _event_body(
        self,
        mesh: str | None,
        *,
        title: Any,
        start: Any,
        end: Any,
        description: Any,
        all_day: Any,
        event_type: Any,
        location: Any,
        meeting_url: Any,
        owner: Any,
        linked_type: Any,
        linked_id: Any,
        color: Any,
        custom_fields: Any,
        attendees: Any,
        recurrence: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        put(body, "title", title)
        if start is not UNSET:
            body["startAt"] = iso_datetime(start, "start")
        if end is not UNSET:
            body["endAt"] = iso_datetime(end, "end")
        if start is not UNSET and end is not UNSET and aware(end, "end") < aware(start, "start"):
            raise MeshbookError("bad_argument", "end is before start.")
        put(body, "description", description)
        if all_day is not UNSET:
            body["isAllDay"] = bool(all_day)
        if event_type is not UNSET:
            body["eventType"] = _event_type(event_type)
        put(body, "location", location)
        put(body, "meetingUrl", meeting_url)
        if owner is not UNSET:
            body["ownerUserId"] = None if owner is None else self._user_id(str(owner), mesh=mesh)
        if (linked_type is UNSET) != (linked_id is UNSET) or (
            linked_type is not UNSET and (linked_type is None) != (linked_id is None)
        ):
            raise MeshbookError(
                "bad_argument", "Pass linked_type and linked_id together (or both None to clear)."
            )
        put(body, "linkedEntityType", linked_type)
        put(body, "linkedEntityId", linked_id)
        put(body, "colorHex", color)
        put(body, "customFields", custom_fields)
        if attendees is not UNSET:
            body["attendees"] = [self._attendee(a, mesh) for a in (attendees or [])]
        if recurrence is not UNSET:
            body["recurrence"] = _recurrence_body(recurrence)
        return body

    # ── the routes ────────────────────────────────────────────────────

    def list(
        self,
        start: Any = None,
        end: Any = None,
        *,
        linked_type: str | None = None,
        linked_id: str | None = None,
        owner: str | None = None,
        expand: bool = False,
        tz: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/calendar-events — events overlapping [start, end).

        ``expand=True`` (needs both start and end) turns each recurring
        event into one item per occurrence in the window, each with
        ``occurrenceStart`` / ``occurrenceEnd`` / ``seriesId``; at most 500
        come back and ``.truncated`` says if the server capped it. ``tz``
        (IANA) is the wall clock recurrences repeat in (default UTC
        server-side). ``owner``: UUID, @username or display name. Items
        carry ``attendees`` and ``recurrence``.
        """
        if expand and (start is None or end is None):
            raise MeshbookError("bad_argument", "expand=True needs both start and end.")
        if (linked_type is None) != (linked_id is None):
            raise MeshbookError("bad_argument", "Pass linked_type and linked_id together.")
        params: dict[str, Any] = {
            "from": iso_datetime(start, "start") if start is not None else None,
            "to": iso_datetime(end, "end") if end is not None else None,
            "linkedType": linked_type,
            "linkedId": linked_id,
            "ownerId": self._user_id(owner, mesh=mesh) if owner else None,
            "expand": True if expand else None,
            "tz": tz,
        }
        return self._c._list("events.list", params=params, limit=limit, offset=offset, mesh=mesh)

    def get(self, event_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/calendar-events/{id} — with ``attendees`` and ``recurrence``."""
        return as_dict(self._c._call("events.get", path={"event_id": event_id}, mesh=mesh))

    def create(
        self,
        title: str,
        start: Any,
        end: Any,
        *,
        description: str | None = None,
        all_day: bool = False,
        event_type: str = "Meeting",
        location: str | None = None,
        meeting_url: str | None = None,
        owner: str | None = None,
        linked_type: str | None = None,
        linked_id: str | None = None,
        color: str | None = None,
        custom_fields: Mapping[str, Any] | None = None,
        attendees: Iterable[Any] | None = None,
        recurrence: Any = None,
        dedupe: bool = False,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/calendar-events.

        * ``start`` / ``end``: timezone-aware datetimes or ISO strings with
          an offset (naive → ``naive_datetime``, refused locally).
        * ``event_type``: Meeting / Call / Milestone / Deadline / Internal /
          External / Training.
        * ``owner`` (default: you), attendees' ``user:`` refs: UUID,
          @username or display name; ``contact:`` refs: id, name or email.
        * ``recurrence``: ``recurrence_rule(...)``'s dict, a dict with
          ``frequency``/``interval``/``until``/``count``…, or just
          ``"weekly"``.
        * ``dedupe=True`` refuses (``duplicate_event``) when an event with
          the same title and the same start instant already exists in the
          mesh — the guard ``mesh events create`` applies unless ``--force``.
        """
        body = self._event_body(
            mesh,
            title=title,
            start=start,
            end=end,
            description=description if description is not None else UNSET,
            all_day=all_day,
            event_type=event_type,
            location=location if location is not None else UNSET,
            meeting_url=meeting_url if meeting_url is not None else UNSET,
            owner=owner if owner is not None else UNSET,
            linked_type=linked_type if linked_type is not None else UNSET,
            linked_id=linked_id if linked_id is not None else UNSET,
            color=color if color is not None else UNSET,
            custom_fields=dict(custom_fields) if custom_fields is not None else UNSET,
            attendees=list(attendees) if attendees is not None else UNSET,
            recurrence=recurrence if recurrence is not None else UNSET,
        )
        merge_fields(body, fields)
        if dedupe:
            dupes = self.find_duplicates(title, start, mesh=mesh)
            if dupes:
                ids = ", ".join(str(d.get("id")) for d in dupes)
                raise MeshbookError(
                    "duplicate_event",
                    f"An event titled {title!r} already starts at {body['startAt']} in this mesh "
                    f"({ids}). Pass dedupe=False (the CLI's --force) to create it anyway.",
                    409,
                )
        return as_dict(self._c._call("events.create", body=body, mesh=mesh))

    def update(
        self,
        event_id: str,
        *,
        title: Any = UNSET,
        start: Any = UNSET,
        end: Any = UNSET,
        description: Any = UNSET,
        all_day: Any = UNSET,
        event_type: Any = UNSET,
        location: Any = UNSET,
        meeting_url: Any = UNSET,
        owner: Any = UNSET,
        linked_type: Any = UNSET,
        linked_id: Any = UNSET,
        color: Any = UNSET,
        custom_fields: Any = UNSET,
        attendees: Any = UNSET,
        recurrence: Any = UNSET,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/calendar-events/{id} — only the arguments you pass are sent.

        ``None`` clears a nullable field (description, location, meeting_url,
        owner, linked_type+linked_id together, color, custom_fields).
        ``attendees`` (when passed) REPLACES the attendee set — kept rows keep
        their response. ``recurrence``: a rule = upsert, ``None`` = clear.
        """
        for name, value in (("title", title), ("start", start), ("end", end),
                            ("event_type", event_type), ("all_day", all_day)):
            if value is None:
                raise MeshbookError("bad_argument", f"{name} cannot be cleared (None).")
        body = self._event_body(
            mesh,
            title=title,
            start=start,
            end=end,
            description=description,
            all_day=all_day,
            event_type=event_type,
            location=location,
            meeting_url=meeting_url,
            owner=owner,
            linked_type=linked_type,
            linked_id=linked_id,
            color=color,
            custom_fields=custom_fields,
            attendees=attendees,
            recurrence=recurrence,
        )
        merge_fields(body, fields)
        if not body:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        return as_dict(
            self._c._call("events.update", path={"event_id": event_id}, body=body, mesh=mesh)
        )

    def delete(self, event_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/calendar-events/{id} → ``{id, deleted: true}`` (soft)."""
        return as_dict(self._c._call("events.delete", path={"event_id": event_id}, mesh=mesh))

    def set_recurrence(
        self,
        event_id: str,
        frequency: str,
        interval: int = 1,
        until: Any = None,
        *,
        count: int | None = None,
        by_weekday: Iterable[str] | None = None,
        by_month_day: Iterable[int] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PUT /api/calendar-events/{id}/recurrence — upsert the series rule
        (see :func:`recurrence_rule` for the arguments)."""
        body = recurrence_rule(
            frequency, interval, until, count=count, by_weekday=by_weekday, by_month_day=by_month_day
        )
        return as_dict(
            self._c._call("events.set_recurrence", path={"event_id": event_id}, body=body, mesh=mesh)
        )

    def clear_recurrence(self, event_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/calendar-events/{id}/recurrence → ``{id, eventId,
        deleted: true}``; 404 ``not_found`` if the event had none."""
        return as_dict(
            self._c._call("events.clear_recurrence", path={"event_id": event_id}, mesh=mesh)
        )

    def attendees(
        self,
        event_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/calendar-events/{id}/attendees (not paginated server-side,
        so ``limit``/``offset`` page the full list client-side)."""
        return self._c._list(
            "events.attendees", path={"event_id": event_id}, limit=limit, offset=offset, mesh=mesh
        )

    def add_attendee(
        self,
        event_id: str,
        user: str | None = None,
        contact: str | None = None,
        *,
        email: str | None = None,
        name: str | None = None,
        organiser: bool | None = None,
        optional: bool | None = None,
        response: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/calendar-events/{id}/attendees — exactly one of ``user``
        (UUID/@username/name), ``contact`` (id/name/email) or ``email`` (an
        external guest, with optional ``name``). 409 ``attendee_exists``
        if already on the event."""
        spec: dict[str, Any] = {"user": user, "contact": contact, "email": email, "name": name}
        if organiser is not None:
            spec["organiser"] = organiser
        if optional is not None:
            spec["optional"] = optional
        if response is not None:
            spec["response"] = response
        body = self._attendee(spec, mesh)
        return as_dict(
            self._c._call("events.add_attendee", path={"event_id": event_id}, body=body, mesh=mesh)
        )

    def update_attendee(
        self,
        event_id: str,
        attendee_id: str,
        *,
        response: str | None = None,
        optional: bool | None = None,
        organiser: bool | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/calendar-events/{id}/attendees/{attendeeId} — RSVP
        (``response``: Accepted / Declined / Tentative / NoResponse) or flip
        ``optional``/``organiser``. A user may set their OWN response with
        read access; anything else needs writer role."""
        body: dict[str, Any] = {}
        if response is not None:
            body["response"] = _response(response)
        if optional is not None:
            body["isOptional"] = bool(optional)
        if organiser is not None:
            body["isOrganiser"] = bool(organiser)
        if not body:
            raise MeshbookError("bad_argument", "Nothing to update: pass response, optional or organiser.")
        return as_dict(
            self._c._call(
                "events.update_attendee",
                path={"event_id": event_id, "attendee_id": attendee_id},
                body=body,
                mesh=mesh,
            )
        )

    def remove_attendee(
        self, event_id: str, attendee_id: str, *, mesh: str | None = None
    ) -> dict[str, Any]:
        """DELETE /api/calendar-events/{id}/attendees/{attendeeId} → ``{id, deleted: true}``."""
        return as_dict(
            self._c._call(
                "events.remove_attendee",
                path={"event_id": event_id, "attendee_id": attendee_id},
                mesh=mesh,
            )
        )

    def find_duplicates(
        self, title: str, start: Any, *, mesh: str | None = None
    ) -> ItemList[dict[str, Any]]:
        """Events in the mesh with the same title (case- and
        whitespace-insensitive) that start at the same instant as *start*."""
        when = aware(start, "start")
        window = self.list(when, when + _dt.timedelta(seconds=1), limit=500, mesh=mesh)
        want = " ".join(title.split()).casefold()
        out: list[dict[str, Any]] = []
        for ev in window:
            if " ".join(str(ev.get("title") or "").split()).casefold() != want:
                continue
            try:
                ev_start = aware(str(ev.get("startAt") or ""), "startAt")
            except MeshbookError:
                continue
            if ev_start == when:
                out.append(dict(ev))
        return ItemList(out, total=len(out))


def _event_type(value: Any) -> str:
    canon = {t.lower(): t for t in EVENT_TYPES}.get(str(value).strip().lower())
    if canon is None:
        raise MeshbookError(
            "bad_argument", f"event_type must be one of {', '.join(EVENT_TYPES)}, got {value!r}."
        )
    return canon


def _response(value: str) -> str:
    canon = {r.lower(): r for r in RESPONSES}.get(value.strip().lower().replace(" ", ""))
    if canon is None:
        raise MeshbookError(
            "bad_argument", f"response must be one of {', '.join(RESPONSES)}, got {value!r}."
        )
    return canon
