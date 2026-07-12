"""meshbook — the official Python SDK for meshbook.org.

Thin, typed, zero-dependency (stdlib urllib) synchronous client,
extracted from the proven core of meshbook-cli.

    from meshbook import MeshbookClient

    client = MeshbookClient()           # token from MESHBOOK_TOKEN or ~/.meshbook/config
    for mesh in client.meshes.list_mine():
        print(mesh.name)
"""
from meshbook.client import (
    VERSION,
    Attachment,
    ExportJob,
    Mesh,
    MeshbookClient,
    MeshbookError,
    User,
)

__version__ = VERSION
__all__ = [
    "VERSION",
    "Attachment",
    "ExportJob",
    "Mesh",
    "MeshbookClient",
    "MeshbookError",
    "User",
]
