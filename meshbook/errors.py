"""meshbook.errors — the one exception type the SDK raises."""
from __future__ import annotations


class MeshbookError(Exception):
    """Any failure talking to meshbook.org, or refused locally before the wire.

    Attributes:
        status:  HTTP status code (0 for network / local errors).
        code:    machine code from the API error envelope
                 (e.g. ``forbidden``, ``not_found``, ``network_error``) or a
                 local code (``naive_datetime``, ``mesh_ambiguous``, …).
        message: human-readable message, safe to show to a person or a model.
    """

    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{status}] {code}: {message}")
