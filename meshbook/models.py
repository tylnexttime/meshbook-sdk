"""meshbook.models — ItemList and the cheap frozen dataclasses.

Every typed view keeps the full server payload in ``.raw`` so nothing is
lost in translation; the dataclasses exist for the handful of shapes that
are stable enough to deserve attribute access.
"""
from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, NamedTuple, SupportsIndex, TypeVar, overload

T = TypeVar("T")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ItemList(list[T], Generic[T]):
    """A list result, with the server's pagination envelope kept.

    A real ``list`` subclass, so every caller written against the 0.3
    plain-list returns keeps working (iteration, ``len``, indexing,
    equality with a list).

    Attributes:
        total:  the server's ``total`` (``None`` when the endpoint returns a
                bare list). Many endpoints report the PAGE length here, not
                the full count; :attr:`total_exact` says which you have.
        total_exact: True when ``total`` is known to be the full count: the
                server sent its paging (``limit`` and ``offset``) alongside
                ``total`` (the 2026-10-19 ``ok_page`` shape), or the server
                sent every row and the client paged them itself. False
                otherwise: ``total`` may be the page length, and
                ``has_more`` is the honest signal.
        limit:  the ``limit`` this call asked for (``None`` if it sent none).
        offset: the ``offset`` this call asked for (``None`` if it sent none).
        meta:   any other envelope keys (``page``, ``pageSize``,
                ``truncated``, ``semantic``, …).
    """

    total: int | None
    limit: int | None
    offset: int | None
    meta: dict[str, Any]
    total_exact: bool

    def __init__(
        self,
        items: Iterable[T] = (),
        *,
        total: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
        meta: dict[str, Any] | None = None,
        total_exact: bool | None = None,
    ) -> None:
        super().__init__(items)
        self.total = total
        self.limit = limit
        self.offset = offset
        self.meta = dict(meta or {})
        if total_exact is None:
            total_exact = (
                total is not None
                and _is_int(self.meta.get("limit"))
                and _is_int(self.meta.get("offset"))
            )
        self.total_exact = bool(total_exact) and total is not None

    @property
    def truncated(self) -> bool:
        """True when the server said it capped the result (``truncated: true``)."""
        return bool(self.meta.get("truncated"))

    @property
    def has_more(self) -> bool:
        """Best available answer to "is there another page?".

        True when the server's ``total`` exceeds what has been read so far,
        or when a full page came back (``len == limit``) — the server
        reports the page length as ``total`` on many endpoints, so a full
        page is the only signal those give.
        """
        start = self.offset or 0
        consumed = self._server_rows
        if self.total is not None and self.total > start + consumed:
            return True
        if self.meta.get("server_total") is not None:
            return int(self.meta["server_total"]) > start + consumed
        return self.truncated or (self.limit is not None and consumed >= self.limit)

    @property
    def _server_rows(self) -> int:
        """Rows the server sent for this page (more than ``len`` when the
        SDK filtered the page client-side, e.g. ``tasks.list_mine(open_only=True)``)."""
        raw = self.meta.get("server_rows")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        return len(self)

    @property
    def next_offset(self) -> int:
        """The ``offset=`` for the next page: this page's offset plus the rows
        the SERVER sent (not ``len(self)``, which can be smaller after a
        client-side filter)."""
        return (self.offset or 0) + self._server_rows

    def __repr__(self) -> str:
        return (
            f"ItemList({list.__repr__(self)}, total={self.total!r}, "
            f"limit={self.limit!r}, offset={self.offset!r}, total_exact={self.total_exact!r})"
        )


class SearchResults(ItemList[dict[str, Any]]):
    """``chat.search()``'s result: an :class:`ItemList` that also answers the
    0.3 dict keys (``res["items"]``, ``res["semantic"]``, ``res["total"]``)
    with a DeprecationWarning, so existing callers keep working for one minor.
    """

    @property
    def semantic(self) -> bool:
        """False when the server's embedding arm was down (keyword-only recall)."""
        return bool(self.meta.get("semantic"))

    @overload
    def __getitem__(self, key: SupportsIndex) -> dict[str, Any]: ...
    @overload
    def __getitem__(self, key: slice) -> list[dict[str, Any]]: ...
    @overload
    def __getitem__(self, key: str) -> Any: ...

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            warnings.warn(
                "chat.search() returns an ItemList since meshbook-sdk 0.4.0; "
                f"use the list itself / .total / .semantic instead of [{key!r}]",
                DeprecationWarning,
                stacklevel=2,
            )
            if key == "items":
                return list(self)
            if key == "semantic":
                return self.semantic
            if key == "total":
                return self.total if self.total is not None else len(self)
            raise KeyError(key)
        return super().__getitem__(key)


class DownloadedFile(NamedTuple):
    """A download held in memory: ``download_bytes()``'s result.

    Unpacks as ``filename, content = ...``. ``filename`` is the server's
    Content-Disposition name already reduced by :func:`meshbook.safe_filename`
    (a single safe path component, never ``..`` or a device name), so a
    caller can join it onto its own directory; it is still the uploader's
    choice, so check containment if you write it somewhere that matters.
    """

    filename: str
    content: bytes


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    identity_type: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> User:
        return cls(
            id=d.get("id", ""),
            username=d.get("username", ""),
            display_name=d.get("displayName") or d.get("display_name") or "",
            identity_type=d.get("identityType") or d.get("identity_type") or "",
            raw=d,
        )


@dataclass(frozen=True)
class Mesh:
    id: str
    name: str
    mesh_type: str
    member_role: str
    raw: dict[str, Any] = field(repr=False)
    #: Set only on ``meshes.use(..., persist=True)``'s result: the value of
    #: ``MESHBOOK_MESH`` when it is set to a DIFFERENT mesh, which beats the
    #: config file for every new client in that environment, so the mesh
    #: just persisted will not be the one they use. None otherwise.
    env_override: str | None = field(default=None, compare=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Mesh:
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            mesh_type=d.get("meshType") or d.get("type") or "",
            member_role=d.get("memberRole") or "",
            raw=d,
        )


@dataclass(frozen=True)
class ExportJob:
    id: str
    mesh_id: str
    status: str  # pending | running | ready | failed
    byte_size: int | None
    download_url: str | None
    expires_at: str | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> ExportJob:
        return cls(
            id=d.get("id", ""),
            mesh_id=d.get("meshId", ""),
            status=d.get("status", ""),
            byte_size=d.get("byteSize"),
            download_url=d.get("downloadUrl"),
            expires_at=d.get("expiresAt"),
            raw=d,
        )


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    mime_type: str
    byte_size: int | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Attachment:
        return cls(
            id=d.get("id", ""),
            filename=d.get("filename", ""),
            mime_type=d.get("mimeType", ""),
            byte_size=d.get("byteSize"),
            raw=d,
        )


__all__ = ["Attachment", "DownloadedFile", "ExportJob", "ItemList", "Mesh", "SearchResults", "User"]
