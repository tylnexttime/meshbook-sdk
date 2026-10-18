"""meshbook.endpoints — the public, read-only view of the endpoint registry.

Every request this SDK sends goes through one table, keyed by a stable
name (``"files.download"``, ``"tasks.list"`` …). Wrappers that need a
route the SDK has no method for, or that run their own contract check,
import THIS module rather than the private ``meshbook._endpoints``:

    from meshbook import endpoints
    endpoints.ENDPOINTS["tasks.list"].method        # "GET"
    endpoints.path("files.download", attachment_id=aid)
    # -> "/api/entity-attachments/<aid>/download" (values percent-encoded)
    endpoints.calls()                               # [(METHOD, template), ...]

``ENDPOINTS`` is a read-only mapping; :class:`Endpoint` is frozen. Keys
are stable within a minor release (an entry may be added, never renamed
or removed). ``scripts/contract_check.py --registry meshbook.endpoints``
works the same as on the private module.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ._endpoints import ENDPOINTS as _ENDPOINTS
from ._endpoints import Endpoint, MeshMode, calls, pending

#: Registry key -> :class:`Endpoint`, read-only.
ENDPOINTS: Mapping[str, Endpoint] = MappingProxyType(_ENDPOINTS)


def path(key: str, **params: Any) -> str:
    """The rendered path for registry *key*, every value percent-encoded.

    ``KeyError`` for an unknown key or a missing path parameter.
    """
    return _ENDPOINTS[key].render(params or None)


__all__ = ["ENDPOINTS", "Endpoint", "MeshMode", "calls", "path", "pending"]
