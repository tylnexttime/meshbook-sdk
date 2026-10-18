"""Shared plumbing for the resource namespaces."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..client import _data, _is_uuid
from ..errors import MeshbookError

if TYPE_CHECKING:
    from ..client import MeshbookClient


class _Unset:
    """Sentinel for "argument not passed" on update methods, where ``None``
    is a real value (it clears the field server-side)."""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: Default for every update-method argument: "leave this field alone".
UNSET: Any = _Unset()


def put(body: dict[str, Any], key: str, value: Any) -> None:
    """Copy *value* into *body* under *key* unless it is :data:`UNSET`."""
    if value is not UNSET:
        body[key] = value


def put_if(body: dict[str, Any], key: str, value: Any) -> None:
    """Copy *value* into *body* when it is not None/empty (create methods)."""
    if value is not None and value is not UNSET and value != "":
        body[key] = value


def merge_fields(body: dict[str, Any], fields: Mapping[str, Any] | None) -> None:
    """The ``fields=`` escape hatch: raw wire keys (camelCase) passed through
    verbatim, for server fields this SDK has no named argument for yet."""
    if fields:
        for k, v in fields.items():
            body.setdefault(k, v)


def as_dict(payload: Any) -> dict[str, Any]:
    data = _data(payload)
    return data if isinstance(data, dict) else {}


def no_offset(offset: Any, what: str, cursor: str = "limit= and before=") -> None:
    """Refuse ``offset=`` on a route whose server ignores it.

    These routes page by *cursor* only; an offset sent there is dropped
    server-side and the caller would silently get the first page again.
    """
    if offset is not None:
        raise MeshbookError(
            "bad_argument",
            f"{what} does not support offset=: the server ignores it and pages by {cursor} only.",
            hint="use_before_cursor" if "before" in cursor else "fix_argument",
        )


class Namespace:
    def __init__(self, client: MeshbookClient) -> None:
        self._c = client

    # helpers every namespace can use -------------------------------

    def _user_id(self, user: str, mesh: str | None = None) -> str:
        """A user ref (UUID, @username, or display name) → UUID."""
        if _is_uuid(user.strip().lstrip("@")):
            return user.strip().lstrip("@")
        return str(self._c.users.resolve(user, mesh=mesh)["id"])

    def _require_nonempty(self, value: str | None, what: str) -> str:
        if value is None or not str(value).strip():
            raise MeshbookError("bad_argument", f"{what} is empty.")
        return str(value).strip()


__all__ = ["UNSET", "Namespace", "as_dict", "merge_fields", "no_offset", "put", "put_if"]
