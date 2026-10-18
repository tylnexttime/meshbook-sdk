"""meshbook — the official Python SDK for meshbook.org.

Thin, typed, zero-dependency (stdlib urllib) synchronous client, and since
0.4.0 the single Python core that meshbook-cli and meshbook-mcp build on.

    from meshbook import Client

    client = Client()                    # MESHBOOK_TOKEN / ~/.meshbook/config
    for mesh in client.meshes.list_mine():
        print(mesh.name)
    client.tasks.list(mesh="The Tyl Mesh", limit=20)   # per-call mesh override
"""
from meshbook import config
from meshbook.client import (
    VERSION,
    Client,
    MeshbookClient,
)
from meshbook.errors import MeshbookError
from meshbook.models import Attachment, ExportJob, ItemList, Mesh, SearchResults, User
from meshbook.resources import UNSET, recurrence_rule

__version__ = VERSION
__all__ = [
    "UNSET",
    "VERSION",
    "Attachment",
    "Client",
    "ExportJob",
    "ItemList",
    "Mesh",
    "MeshbookClient",
    "MeshbookError",
    "SearchResults",
    "User",
    "config",
    "recurrence_rule",
]
