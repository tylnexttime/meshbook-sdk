"""Time handling for every datetime the SDK puts on the wire.

The contract (PLAN 2026-10-19, "Times"): every datetime sent is ISO 8601
WITH an offset, and every date is ``YYYY-MM-DD``. A naive datetime is
refused here with a clear error rather than guessed at: the SDK runs on
servers, Pis and laptops in every zone, and "the machine's local zone" is
a decision only the caller (or the CLI, which says out loud which zone it
used) can make.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from .errors import MeshbookError

_NAIVE_HINT = (
    "meshbook never guesses a time zone. Pass a timezone-aware datetime, e.g. "
    "datetime(2026, 10, 20, 9, 0, tzinfo=ZoneInfo('Australia/Sydney')), "
    "datetime.now().astimezone(), or an ISO string with an offset such as "
    "'2026-10-20T09:00:00+11:00'."
)


def _parse_iso(value: str, field: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"  # fromisoformat only learned 'Z' in 3.11
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        raise MeshbookError(
            "bad_datetime", f"{field}={value!r} is not an ISO 8601 date-time. {_NAIVE_HINT}"
        ) from None


def iso_datetime(value: Any, field: str) -> str:
    """A timezone-aware datetime (or an ISO string with an offset) → ISO 8601.

    Raises ``naive_datetime`` for a datetime without tzinfo (or a string
    without an offset), ``bad_datetime`` for anything else.
    """
    if isinstance(value, _dt.datetime):
        dt = value
    elif isinstance(value, _dt.date):
        raise MeshbookError(
            "bad_datetime",
            f"{field} needs a date-time, got the date {value.isoformat()}. {_NAIVE_HINT}",
        )
    elif isinstance(value, str):
        dt = _parse_iso(value, field)
    else:
        raise MeshbookError(
            "bad_datetime", f"{field} must be a datetime or ISO string, got {type(value).__name__}."
        )
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise MeshbookError("naive_datetime", f"{field} has no time zone. {_NAIVE_HINT}")
    return dt.isoformat()


def aware(value: Any, field: str) -> _dt.datetime:
    """Like :func:`iso_datetime` but returns the parsed aware datetime."""
    return _dt.datetime.fromisoformat(iso_datetime(value, field))


def iso_date(value: Any, field: str) -> str:
    """A ``date`` (or ``'YYYY-MM-DD'``) → ``'YYYY-MM-DD'``.

    A ``datetime`` is refused: which zone's calendar day it means is
    exactly the ambiguity this module exists to stop.
    """
    if isinstance(value, _dt.datetime):
        raise MeshbookError(
            "bad_date",
            f"{field} is a date (YYYY-MM-DD), not a date-time; pass value.date() "
            "in the zone you mean.",
        )
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            raise MeshbookError("bad_date", f"{field}={value!r} is not YYYY-MM-DD.") from None
    raise MeshbookError(
        "bad_date", f"{field} must be a date or 'YYYY-MM-DD', got {type(value).__name__}."
    )


def due_fields(value: Any) -> dict[str, Any]:
    """A task's ``due``: a date → ``dueDate``; an aware datetime → ``dueAt``.

    ``None`` clears both (only meaningful on update). A string of exactly
    ``YYYY-MM-DD`` is a date; any other string must be an ISO date-time
    with an offset.
    """
    if value is None:
        return {"dueDate": None, "dueAt": None}
    if isinstance(value, _dt.datetime):
        return {"dueAt": iso_datetime(value, "due")}
    if isinstance(value, _dt.date):
        return {"dueDate": value.isoformat()}
    if isinstance(value, str) and len(value.strip()) == 10:
        return {"dueDate": iso_date(value, "due")}
    return {"dueAt": iso_datetime(value, "due")}
