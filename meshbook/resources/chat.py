"""Entity chat (the mesh feed and entity threads), channels and DMs."""
from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from ..client import _is_uuid
from ..errors import MeshbookError
from ..models import Attachment, DownloadedFile, ItemList, SearchResults
from ._base import Namespace, as_dict, put_if


def _file_body(
    file_path: str | os.PathLike[str], filename: str | None, mime: str | None
) -> dict[str, Any]:
    path = Path(file_path).expanduser()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise MeshbookError("file_not_found", f"No such file: {path}") from None
    if not raw:
        raise MeshbookError("empty_file", f"File is empty: {path}")
    return {
        "filename": filename or path.name,
        "mimeType": mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "base64Bytes": base64.b64encode(raw).decode("ascii"),
    }


class Chat(Namespace):
    """``client.chat`` — entity chat. By default the mesh-wide feed of the
    active mesh; pass ``entity_type``/``entity_id`` for any entity's thread.
    For named channels and DMs use ``client.channels`` / ``client.dms``."""

    def _thread(self, entity_type: str, entity_id: str | None, mesh: str | None) -> dict[str, str]:
        if entity_type == "mesh" and entity_id is None:
            entity_id = self._c._require_mesh(mesh)
        if not entity_id:
            raise MeshbookError("bad_argument", f"entity_id is required for entity_type={entity_type!r}.")
        return {"entity_type": entity_type, "entity_id": entity_id}

    def post(
        self,
        message: str,
        reply_to: str | None = None,
        *,
        entity_type: str = "mesh",
        entity_id: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/entities/{type}/{id}/chat. ``reply_to`` threads the
        message under an existing message's UUID (parentMessageId)."""
        body: dict[str, Any] = {"bodyMd": message}
        put_if(body, "parentMessageId", reply_to)
        return as_dict(
            self._c._call(
                "chat.post", path=self._thread(entity_type, entity_id, mesh), body=body, mesh=mesh
            )
        )

    def list(
        self,
        limit: int | None = 20,
        *,
        offset: int | None = None,
        entity_type: str = "mesh",
        entity_id: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/entities/{type}/{id}/chat — newest first."""
        return self._c._list(
            "chat.list",
            path=self._thread(entity_type, entity_id, mesh),
            limit=limit,
            offset=offset,
            mesh=mesh,
        )

    def get_message(self, message_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/chat-messages/{id} — any message (entity, channel or DM);
        ``channelId`` is set for channel/DM messages."""
        return as_dict(self._c._call("chat.message", path={"message_id": message_id}, mesh=mesh))

    def search(
        self,
        q: str,
        limit: int | None = 20,
        *,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> SearchResults:
        """§84 hybrid keyword+semantic search over everything readable in the
        mesh (feed, entity threads, channels, your own DMs).

        Returns :class:`SearchResults`: an ItemList of hits with ``.total``
        and ``.semantic`` (False when the server's embedding arm was down
        and recall was keyword-only — still valid results, just narrower).
        The 0.3 dict keys (``res["items"]`` …) still work, with a
        DeprecationWarning.
        """
        self._c._require_mesh(mesh)
        out = self._c._list(
            "chat.search", lead={"q": q}, limit=limit, offset=offset, mesh=mesh, cls=SearchResults
        )
        assert isinstance(out, SearchResults)
        return out

    def attach(
        self,
        message_id: str,
        file_path: str | os.PathLike[str],
        filename: str | None = None,
        mime: str | None = None,
        *,
        mesh: str | None = None,
    ) -> Attachment:
        """Attach a local file to an existing chat message (§26d-json:
        base64 in, no multipart)."""
        data = as_dict(
            self._c._call(
                "chat.attach",
                path={"message_id": message_id},
                body=_file_body(file_path, filename, mime),
                mesh=mesh,
            )
        )
        return Attachment.from_api(data)

    def download(
        self,
        attachment_id: str,
        out_path: str | os.PathLike[str] | None = None,
        *,
        mesh: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Download a chat attachment. Returns the written Path.

        No out_path → the server's filename in the current directory; a
        directory → the server's filename inside it (sanitised, and refused
        with ``unsafe_path`` if it would land anywhere else); a file path →
        that file. An existing file raises ``file_exists`` unless
        ``overwrite=True``. Written atomically.
        """
        return self._c._download(
            "chat.download",
            path={"attachment_id": attachment_id},
            out_path=out_path,
            mesh=mesh,
            overwrite=overwrite,
        )

    def download_bytes(self, attachment_id: str, *, mesh: str | None = None) -> DownloadedFile:
        """A chat attachment in memory: ``(filename, content)``, the filename
        already sanitised. Nothing is written to disk."""
        return self._c._download_bytes(
            "chat.download", path={"attachment_id": attachment_id}, mesh=mesh
        )

    def react(self, message_id: str, emoji: str, *, mesh: str | None = None) -> dict[str, Any]:
        """POST /api/chat-messages/{id}/reactions — entity, channel and DM
        messages alike."""
        return as_dict(
            self._c._call(
                "chat.react", path={"message_id": message_id}, body={"emoji": emoji}, mesh=mesh
            )
        )

    def unreact(self, message_id: str, emoji: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/chat-messages/{id}/reactions/{emoji} (emoji percent-encoded)."""
        return as_dict(
            self._c._call(
                "chat.unreact", path={"message_id": message_id, "emoji": emoji}, mesh=mesh
            )
        )


class Channels(Namespace):
    """``client.channels`` — named channels (groups and broadcasts)."""

    def list(
        self, *, limit: int | None = None, offset: int | None = None, mesh: str | None = None
    ) -> ItemList[dict[str, Any]]:
        """GET /api/meshes/{mesh}/channels — channel dicts (name,
        channelType, isPrivate, unreadCount, …). A 403 is raised, never
        rendered as an empty list (an authorisation failure is not an empty
        room — Wren, report A4)."""
        mesh_id = self._c._require_mesh(mesh)
        return self._c._list(
            "channels.list", path={"mesh_id": mesh_id}, limit=limit, offset=offset
        )

    def resolve(self, channel: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Channel ref → row. A UUID is fetched directly; a name (leading
        '#' stripped, case-insensitive) is matched in the mesh's channel
        list. ``channel_not_found`` lists the channels that do exist."""
        target = channel.lstrip("#").strip()
        if not target:
            raise MeshbookError("bad_channel", "Empty channel reference.")
        if _is_uuid(target):
            detail = as_dict(self._c._call("channels.get", path={"channel_id": target}, mesh=mesh))
            if detail:
                return detail
            raise MeshbookError("channel_not_found", f"No channel {channel!r}.", 404)
        rows = self.list(mesh=mesh)
        folded = target.casefold()
        for ch in rows:
            if (ch.get("name") or "").casefold() == folded:
                return dict(ch)
        names = ", ".join("#" + str(c.get("name") or "?") for c in rows) or "(none)"
        raise MeshbookError(
            "channel_not_found", f"No channel matching {channel!r} in this mesh. Channels: {names}", 404
        )

    #: 0.3 name, kept.
    _resolve = resolve

    def create(
        self,
        name: str,
        *,
        topic: str | None = None,
        private: bool = False,
        channel_type: str = "group",
        severity: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/meshes/{mesh}/channels (mesh admin by default).

        ``channel_type="broadcast"`` makes an admin-post-only channel;
        ``severity`` is ``announcement`` or ``fyi`` (default ``fyi``).
        """
        clean = name.lstrip("#").strip()
        if not clean:
            raise MeshbookError("bad_argument", "Channel name cannot be empty.")
        body: dict[str, Any] = {"name": clean}
        put_if(body, "topic", topic)
        if private:
            body["isPrivate"] = True
        if channel_type and channel_type != "group":
            body["channelType"] = channel_type
            if channel_type == "broadcast":
                body["broadcastSeverity"] = severity or "fyi"
        mesh_id = self._c._require_mesh(mesh)
        return as_dict(self._c._call("channels.create", path={"mesh_id": mesh_id}, body=body))

    def read(
        self,
        channel: str,
        limit: int | None = 20,
        *,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/channels/{id}/messages — newest first."""
        ch = self.resolve(channel, mesh=mesh)
        return self._c._list(
            "channels.messages", path={"channel_id": ch["id"]}, limit=limit, offset=offset, mesh=mesh
        )

    def post(
        self,
        channel: str,
        message: str,
        reply_to: str | None = None,
        *,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/channels/{id}/messages. Broadcast channels are
        admin-post-only server-side (403 → MeshbookError)."""
        ch = self.resolve(channel, mesh=mesh)
        body: dict[str, Any] = {"bodyMd": message}
        put_if(body, "parentMessageId", reply_to)
        return as_dict(
            self._c._call("channels.post", path={"channel_id": ch["id"]}, body=body, mesh=mesh)
        )

    def reply(self, message_id: str, message: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Threaded reply to a channel (or DM) message, finding its channel
        from the message itself. ``not_channel_message`` for entity-chat
        messages (use ``chat.post(..., reply_to=)`` for those)."""
        parent = self._c.chat.get_message(message_id, mesh=mesh)
        channel_id = parent.get("channelId")
        if not channel_id:
            raise MeshbookError(
                "not_channel_message",
                f"Message {message_id} is not in a channel; reply with "
                "chat.post(..., reply_to=...) instead.",
            )
        body = {"bodyMd": message, "parentMessageId": message_id}
        return as_dict(
            self._c._call("channels.post", path={"channel_id": channel_id}, body=body, mesh=mesh)
        )

    def members(
        self,
        channel: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """§88a — a channel's members (for private channels, the access list;
        the server refuses for channels you can't see)."""
        ch = self.resolve(channel, mesh=mesh)
        return self._c._list(
            "channels.members", path={"channel_id": ch["id"]}, limit=limit, offset=offset, mesh=mesh
        )

    def _member_id(self, user_id: str | None, user: str | None, mesh: str | None) -> str:
        if (user_id is None) == (user is None):
            raise MeshbookError("bad_argument", "Pass exactly one of user_id= or user=.")
        if user_id is not None:
            return user_id
        assert user is not None
        return self._user_id(user, mesh=mesh)

    def add_member(
        self,
        channel: str,
        user_id: str | None = None,
        *,
        user: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """§88a — add a mesh member (channel creator / mesh admin only).

        ``user_id`` is an id sent as-is (0.3); ``user`` takes a UUID,
        @username or display name. Returns the member list.
        """
        ch = self.resolve(channel, mesh=mesh)
        uid = self._member_id(user_id, user, mesh)
        payload = self._c._call(
            "channels.add_member", path={"channel_id": ch["id"]}, body={"userId": uid}, mesh=mesh
        )
        from ..client import _item_list

        return _item_list(payload)

    def remove_member(
        self,
        channel: str,
        user_id: str | None = None,
        *,
        user: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """§88a — remove a member (creator/admin, or yourself to leave).
        User arguments as :meth:`add_member`."""
        ch = self.resolve(channel, mesh=mesh)
        uid = self._member_id(user_id, user, mesh)
        return as_dict(
            self._c._call(
                "channels.remove_member",
                path={"channel_id": ch["id"], "user_id": uid},
                mesh=mesh,
            )
        )


class Dms(Namespace):
    """``client.dms`` — direct messages."""

    def list(
        self, *, limit: int | None = None, offset: int | None = None, mesh: str | None = None
    ) -> ItemList[dict[str, Any]]:
        """GET /api/meshes/{mesh}/dms — your DM threads."""
        mesh_id = self._c._require_mesh(mesh)
        return self._c._list("dms.list", path={"mesh_id": mesh_id}, limit=limit, offset=offset)

    def open(self, user: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Open (or return the existing) DM channel with *user* — idempotent."""
        mesh_id = self._c._require_mesh(mesh)
        uid = self._user_id(user, mesh=mesh_id)
        return as_dict(self._c._call("dms.open", path={"mesh_id": mesh_id, "user_id": uid}))

    def read(
        self,
        user: str,
        limit: int | None = 20,
        *,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """Messages in your DM thread with *user* — newest first."""
        dm = self.open(user, mesh=mesh)
        return self._c._list(
            "channels.messages", path={"channel_id": dm["id"]}, limit=limit, offset=offset, mesh=mesh
        )

    def send(self, user: str, message: str, *, mesh: str | None = None) -> dict[str, Any]:
        """Send a DM (opens the thread if needed)."""
        dm = self.open(user, mesh=mesh)
        return as_dict(
            self._c._call(
                "channels.post", path={"channel_id": dm["id"]}, body={"bodyMd": message}, mesh=mesh
            )
        )
