"""Notifications, entity attachments, exports, custom fields, saved views."""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any

from ..errors import MeshbookError
from ..models import Attachment, DownloadedFile, ExportJob, ItemList
from ._base import Namespace, as_dict
from .chat import _file_body


class Notifications(Namespace):
    """``client.notifications``."""

    def list(
        self,
        *,
        unread_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
        group: str | None = None,
        kind: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/notifications — notification dicts (kind, summary,
        readAt, createdAt, …). ``unread_only`` filters server-side; ``group``
        is a tab key (mentions | invites | assignments | mesh | system)."""
        params: dict[str, Any] = {
            "unreadOnly": True if unread_only else None,
            "group": group,
            "kind": kind,
        }
        return self._c._list(
            "notifications.list", params=params, limit=limit, offset=offset, mesh=mesh
        )


class Files(Namespace):
    """Entity attachments (§78) — files and links on any entity: company |
    contact | lead | project | task | portfolio | calendar_event | mesh."""

    def attach(
        self,
        entity_type: str,
        entity_id: str,
        file_path: str | os.PathLike[str],
        filename: str | None = None,
        mime: str | None = None,
        *,
        mesh: str | None = None,
    ) -> Attachment:
        """POST /api/entities/{type}/{id}/attachments/json (base64)."""
        data = as_dict(
            self._c._call(
                "files.attach",
                path={"entity_type": entity_type, "entity_id": entity_id},
                body=_file_body(file_path, filename, mime),
                mesh=mesh,
            )
        )
        return Attachment.from_api(data)

    def link(
        self,
        entity_type: str,
        entity_id: str,
        url: str,
        filename: str | None = None,
        *,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/entities/{type}/{id}/attachments/links — attach an
        external URL (``filename`` is its display name)."""
        return as_dict(
            self._c._call(
                "files.link",
                path={"entity_type": entity_type, "entity_id": entity_id},
                body={"url": url, "filename": filename},
                mesh=mesh,
            )
        )

    def list(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/entities/{type}/{id}/attachments — dicts with
        kind='file' (filename/byteSize/mimeType) or kind='link' (externalUrl)."""
        return self._c._list(
            "files.list",
            path={"entity_type": entity_type, "entity_id": entity_id},
            limit=limit,
            offset=offset,
            mesh=mesh,
        )

    def download(
        self,
        attachment_id: str,
        out_path: str | os.PathLike[str] | None = None,
        *,
        mesh: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Download an entity attachment; returns the written Path.

        out_path, ``overwrite`` and the safety rules as
        :meth:`Chat.download <meshbook.resources.chat.Chat.download>`.
        """
        return self._c._download(
            "files.download",
            path={"attachment_id": attachment_id},
            out_path=out_path,
            mesh=mesh,
            overwrite=overwrite,
        )

    def download_bytes(self, attachment_id: str, *, mesh: str | None = None) -> DownloadedFile:
        """An entity attachment in memory: ``(filename, content)``, the
        filename already sanitised. Nothing is written to disk."""
        return self._c._download_bytes(
            "files.download", path={"attachment_id": attachment_id}, mesh=mesh
        )

    def delete(self, attachment_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/entity-attachments/{id} (uploader or mesh admin)."""
        return as_dict(
            self._c._call("files.delete", path={"attachment_id": attachment_id}, mesh=mesh)
        )


class Exports(Namespace):
    """Mesh data exports (§58). Admin/account-manager only; exports expire
    after 24 h server-side."""

    def _target(self, mesh_id: str | None, mesh: str | None) -> str:
        if mesh_id and mesh:
            raise MeshbookError("bad_argument", "Pass mesh_id= or mesh=, not both.")
        if mesh_id:
            return str(mesh_id)
        return self._c._require_mesh(mesh)

    def start(self, mesh_id: str | None = None, *, mesh: str | None = None) -> ExportJob:
        """POST /api/meshes/{id}/export — start (or return the in-flight)
        export job.

        ``mesh_id`` is an id sent as-is (0.3); ``mesh`` takes an id or NAME;
        neither → the client's mesh. The server requires the exported mesh
        to be the active one, so the matching ``X-Active-Mesh-Id`` is sent
        automatically and the client's own active mesh is left alone.
        """
        target = self._target(mesh_id, mesh)
        data = as_dict(self._c._call("exports.start", path={"mesh_id": target}))
        return ExportJob.from_api(data)

    def list(
        self,
        mesh_id: str | None = None,
        *,
        mesh: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ItemList[ExportJob]:
        """GET /api/meshes/{id}/exports — recent jobs, newest first. Poll
        until status == 'ready', then download. Mesh arguments as :meth:`start`."""
        target = self._target(mesh_id, mesh)
        rows = self._c._list("exports.list", path={"mesh_id": target}, limit=limit, offset=offset)
        return ItemList(
            (ExportJob.from_api(d) for d in rows),
            total=rows.total,
            limit=rows.limit,
            offset=rows.offset,
            meta=rows.meta,
        )

    def download(
        self,
        export_id: str,
        out_path: str | os.PathLike[str] | None = None,
        *,
        mesh: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """GET /api/mesh-exports/{id}/download → zip written to out_path
        (a directory keeps the server filename, sanitised). 409 while not
        ready, 410 once expired — both raise MeshbookError with that status.
        An existing file raises ``file_exists`` unless ``overwrite=True``."""
        return self._c._download(
            "exports.download",
            path={"export_id": export_id},
            out_path=out_path,
            mesh=mesh,
            overwrite=overwrite,
        )

    def download_bytes(self, export_id: str, *, mesh: str | None = None) -> DownloadedFile:
        """An export zip in memory: ``(filename, content)``, the filename
        already sanitised. Nothing is written to disk."""
        return self._c._download_bytes("exports.download", path={"export_id": export_id}, mesh=mesh)


class CustomFields(Namespace):
    """``client.custom_fields`` — custom field definitions (read)."""

    def list(
        self,
        entity: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/custom-fields (``entity``: contact / company / lead / …)."""
        return self._c._list(
            "custom_fields.list", params={"entityType": entity}, limit=limit, offset=offset, mesh=mesh
        )


class SavedViews(Namespace):
    """``client.saved_views``."""

    def list(
        self,
        module: str | None = None,
        *,
        entity: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/saved-views, optionally filtered by ``module`` (the
        grouping :meth:`create` takes: leads / contacts / tasks …).

        0.4.0 sent this filter as ``entityType``, which the route does not
        read, so every view came back whatever was asked. ``entity=`` is the
        0.4.0 keyword, kept as a deprecated alias for ``module``.
        """
        if entity is not None:
            if module is not None and module != entity:
                raise MeshbookError("bad_argument", "Pass module= or entity=, not both.")
            warnings.warn(
                "saved_views.list(entity=) is deprecated; the server filters by module=",
                DeprecationWarning,
                stacklevel=2,
            )
            module = entity
        return self._c._list(
            "saved_views.list", params={"module": module}, limit=limit, offset=offset, mesh=mesh
        )

    def create(
        self,
        module: str,
        name: str,
        filter: dict[str, Any] | None = None,
        shared: bool = False,
        *,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/saved-views. NOTE the create field is ``module`` (the
        entity grouping: leads / contacts / tasks …), not ``entityType``."""
        body: dict[str, Any] = {"module": module, "name": name}
        if filter is not None:
            body["filterJson"] = filter
        if shared:
            body["isShared"] = True
        return as_dict(self._c._call("saved_views.create", body=body, mesh=mesh))
