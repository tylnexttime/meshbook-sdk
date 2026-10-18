"""The resource namespaces hung off :class:`meshbook.MeshbookClient`.

Each class is a thin, typed view over a slice of the endpoint registry
(:mod:`meshbook._endpoints`). Conventions every namespace follows:

* Every mesh-scoped method takes a keyword ``mesh=`` (id or name) that
  overrides the client's mesh for that one call — and never persists it.
* Every list method takes ``limit=`` and ``offset=`` and returns an
  :class:`~meshbook.models.ItemList`.
* ``*_id`` parameters are ids, sent as-is (the 0.3 contract). Parameters
  without the suffix (``project=``, ``assignee=``, ``user=``, ``stage=``,
  ``contact=``, ``owner=``) are REFERENCES: an id, or a name the SDK
  resolves — and refuses to guess when a name matches more than one thing.
* ``update()`` methods send only the arguments you pass; ``None`` clears a
  field; ``fields=`` passes extra raw (camelCase) wire keys through.
"""
from __future__ import annotations

from ._base import UNSET, Namespace
from .chat import Channels, Chat, Dms
from .crm import Companies, Contacts, Leads, Pipelines
from .events import EVENT_TYPES, FREQUENCIES, Events, recurrence_rule
from .meshes import Meshes, Users
from .misc import CustomFields, Exports, Files, Notifications, SavedViews
from .work import PRIORITIES, PROJECT_ROLES, STATUSES, TASK_SORTS, Projects, Tags, Tasks

__all__ = [
    "EVENT_TYPES",
    "FREQUENCIES",
    "PRIORITIES",
    "PROJECT_ROLES",
    "STATUSES",
    "TASK_SORTS",
    "UNSET",
    "Channels",
    "Chat",
    "Companies",
    "Contacts",
    "CustomFields",
    "Dms",
    "Events",
    "Exports",
    "Files",
    "Leads",
    "Meshes",
    "Namespace",
    "Notifications",
    "Pipelines",
    "Projects",
    "SavedViews",
    "Tags",
    "Tasks",
    "Users",
    "recurrence_rule",
]
