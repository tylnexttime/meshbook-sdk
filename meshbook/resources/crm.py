"""Contacts, companies, pipelines and leads."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .._times import iso_date
from ..client import _is_uuid
from ..errors import MeshbookError
from ..models import ItemList
from ._base import UNSET, Namespace, as_dict, merge_fields, put, put_if


def _job_title(job_title: Any, role: Any) -> Any:
    """``role`` is the §102 spelling of the contact's job title."""
    if job_title is not UNSET and role is not UNSET and job_title != role:
        raise MeshbookError("bad_argument", "Pass job_title= or role=, not both.")
    return job_title if job_title is not UNSET else role


class Contacts(Namespace):
    """``client.contacts`` — CRM contacts in the active mesh."""

    def list(
        self,
        q: str | None = None,
        limit: int | None = 50,
        *,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/contacts — contact dicts (displayName, primaryEmail, …)."""
        return self._c._list(
            "contacts.list", params={"search": q}, limit=limit, offset=offset, mesh=mesh
        )

    def get(self, contact_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/contacts/{id}."""
        return as_dict(self._c._call("contacts.get", path={"contact_id": contact_id}, mesh=mesh))

    def create(
        self,
        first_name: str,
        last_name: str | None = None,
        email: str | None = None,
        company: str | None = None,
        *,
        phone: str | None = None,
        title: str | None = None,
        job_title: str | None = None,
        role: str | None = None,
        notes: str | None = None,
        tags: Iterable[str] | None = None,
        custom_fields: Mapping[str, Any] | None = None,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/contacts.

        * ``company`` is free text, resolved server-side (§22c) — check
          ``primaryCompanyId`` on the result to see whether it matched.
        * ``title`` is the honorific (Mr, Ms, Dr …); ``job_title`` (alias
          ``role``) is what they do; ``notes`` is the contact's description;
          ``phone`` is the primary phone.
        * ``tags`` are attached by name after the contact exists (one call
          each; unknown names are created in the mesh's tag dictionary).
          The result then carries ``tags``.
        """
        body: dict[str, Any] = {"firstName": first_name, "lastName": last_name}
        put_if(body, "primaryEmail", email)
        put_if(body, "company", company)
        put_if(body, "primaryPhone", phone)
        put_if(body, "title", title)
        jt = _job_title(job_title if job_title is not None else UNSET, role if role is not None else UNSET)
        put_if(body, "jobTitle", jt)
        put_if(body, "description", notes)
        if custom_fields is not None:
            body["customFields"] = dict(custom_fields)
        merge_fields(body, fields)
        created = as_dict(self._c._call("contacts.create", body=body, mesh=mesh))
        if tags is not None and created.get("id"):
            created["tags"] = list(
                self._c.tags.add("contact", str(created["id"]), tags, mesh=mesh)
            )
        return created

    def update(
        self,
        contact_id: str,
        *,
        first_name: Any = UNSET,
        last_name: Any = UNSET,
        email: Any = UNSET,
        phone: Any = UNSET,
        title: Any = UNSET,
        job_title: Any = UNSET,
        role: Any = UNSET,
        notes: Any = UNSET,
        company_id: Any = UNSET,
        custom_fields: Any = UNSET,
        tags: Iterable[str] | None = None,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/contacts/{id} — only the arguments you pass are sent;
        ``None`` clears a field. ``tags`` are ADDED (never removed; use
        ``client.tags.set`` / ``client.tags.detach`` to take tags off)."""
        body: dict[str, Any] = {}
        put(body, "firstName", first_name)
        put(body, "lastName", last_name)
        put(body, "primaryEmail", email)
        put(body, "primaryPhone", phone)
        put(body, "title", title)
        put(body, "jobTitle", _job_title(job_title, role))
        put(body, "description", notes)
        put(body, "primaryCompanyId", company_id)
        put(body, "customFields", custom_fields)
        merge_fields(body, fields)
        if not body and tags is None:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        out: dict[str, Any]
        if body:
            out = as_dict(
                self._c._call("contacts.update", path={"contact_id": contact_id}, body=body, mesh=mesh)
            )
        else:
            out = self.get(contact_id, mesh=mesh)
        if tags is not None:
            out["tags"] = list(self._c.tags.add("contact", contact_id, tags, mesh=mesh))
        return out

    def delete(self, contact_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/contacts/{id} (soft delete; restorable from the recycle bin)."""
        return as_dict(self._c._call("contacts.delete", path={"contact_id": contact_id}, mesh=mesh))

    def resolve(self, contact: str, *, mesh: str | None = None) -> dict[str, Any]:
        """A contact ref (UUID, exact name, or email) → a contact row.

        A UUID comes back as ``{"id": uuid}`` without a lookup. Otherwise
        the server's search runs and an exact display-name or email match
        wins; a single search hit is accepted; anything else raises
        ``contact_not_found`` or ``contact_ambiguous`` (with candidates).
        """
        target = self._require_nonempty(contact, "contact")
        if _is_uuid(target):
            return {"id": target}
        rows = self.list(q=target, limit=20, mesh=mesh)
        folded = target.casefold()
        exact = [
            c
            for c in rows
            if (c.get("displayName") or "").casefold() == folded
            or (c.get("primaryEmail") or "").casefold() == folded
        ]
        if len(exact) == 1:
            return dict(exact[0])
        if not exact and len(rows) == 1:
            return dict(rows[0])
        if not rows:
            raise MeshbookError("contact_not_found", f"No contact matching {target!r}.", 404)
        pool = exact or list(rows)
        listing = "; ".join(f"{c.get('displayName')} ({c.get('id')})" for c in pool[:10])
        raise MeshbookError(
            "contact_ambiguous", f"Several contacts match {target!r}: {listing}. Pass the id."
        )


class Companies(Namespace):
    """``client.companies`` — CRM companies."""

    def list(
        self,
        q: str | None = None,
        limit: int | None = 50,
        *,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/companies."""
        return self._c._list(
            "companies.list", params={"search": q}, limit=limit, offset=offset, mesh=mesh
        )

    def get(self, company_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/companies/{id}."""
        return as_dict(self._c._call("companies.get", path={"company_id": company_id}, mesh=mesh))

    def create(
        self,
        name: str,
        *,
        website: str | None = None,
        industry: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        description: str | None = None,
        custom_fields: Mapping[str, Any] | None = None,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/companies."""
        body: dict[str, Any] = {"name": name}
        put_if(body, "website", website)
        put_if(body, "industry", industry)
        put_if(body, "email", email)
        put_if(body, "phone", phone)
        put_if(body, "description", description)
        if custom_fields is not None:
            body["customFields"] = dict(custom_fields)
        merge_fields(body, fields)
        return as_dict(self._c._call("companies.create", body=body, mesh=mesh))

    def update(
        self,
        company_id: str,
        *,
        name: Any = UNSET,
        website: Any = UNSET,
        industry: Any = UNSET,
        email: Any = UNSET,
        phone: Any = UNSET,
        description: Any = UNSET,
        legal_name: Any = UNSET,
        abn: Any = UNSET,
        company_size: Any = UNSET,
        custom_fields: Any = UNSET,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/companies/{id} — only the arguments you pass are sent."""
        body: dict[str, Any] = {}
        put(body, "name", name)
        put(body, "website", website)
        put(body, "industry", industry)
        put(body, "email", email)
        put(body, "phone", phone)
        put(body, "description", description)
        put(body, "legalName", legal_name)
        put(body, "abn", abn)
        put(body, "companySize", company_size)
        put(body, "customFields", custom_fields)
        merge_fields(body, fields)
        if not body:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        return as_dict(
            self._c._call("companies.update", path={"company_id": company_id}, body=body, mesh=mesh)
        )

    def delete(self, company_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/companies/{id} (soft delete)."""
        return as_dict(self._c._call("companies.delete", path={"company_id": company_id}, mesh=mesh))

    def contacts(
        self,
        company_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/companies/{id}/contacts."""
        return self._c._list(
            "companies.contacts",
            path={"company_id": company_id},
            limit=limit,
            offset=offset,
            mesh=mesh,
        )


class Pipelines(Namespace):
    """``client.pipelines`` — lead pipelines and their stages."""

    def list(
        self, *, limit: int | None = None, offset: int | None = None, mesh: str | None = None
    ) -> ItemList[dict[str, Any]]:
        """GET /api/pipelines — each with its ``stages`` (default pipeline first)."""
        return self._c._list("pipelines.list", limit=limit, offset=offset, mesh=mesh)

    def default(self, *, mesh: str | None = None) -> dict[str, Any]:
        """The mesh's default pipeline (``isDefault``), else the first one."""
        rows = self.list(mesh=mesh)
        if not rows:
            raise MeshbookError(
                "no_pipeline", "This mesh has no pipelines — create one in the web app first.", 404
            )
        return dict(next((p for p in rows if p.get("isDefault")), rows[0]))

    def resolve_stage(self, stage: str, *, mesh: str | None = None) -> str:
        """A stage ref → stage UUID.

        Accepts a UUID (returned as-is), a stage name (case-insensitive,
        across every pipeline in the mesh), or ``"Pipeline/Stage"`` to
        disambiguate. A name present in several pipelines resolves to the
        default pipeline's stage if it has one, else ``stage_ambiguous``.
        """
        target = self._require_nonempty(stage, "stage")
        if _is_uuid(target):
            return target
        pipe_name: str | None = None
        stage_name = target
        if "/" in target:
            pipe_name, stage_name = (s.strip() for s in target.split("/", 1))
        folded = stage_name.casefold()
        hits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        names: list[str] = []
        for p in self.list(mesh=mesh):
            if pipe_name and (p.get("name") or "").casefold() != pipe_name.casefold():
                continue
            for s in p.get("stages") or []:
                names.append(f"{p.get('name')}/{s.get('name')}")
                if (s.get("name") or "").casefold() == folded:
                    hits.append((p, s))
        if len(hits) == 1:
            return str(hits[0][1]["id"])
        if not hits:
            raise MeshbookError(
                "stage_not_found",
                f"No pipeline stage matching {target!r}. Stages: {', '.join(names) or '(none)'}",
                404,
            )
        defaults = [s for p, s in hits if p.get("isDefault")]
        if len(defaults) == 1:
            return str(defaults[0]["id"])
        raise MeshbookError(
            "stage_ambiguous",
            f"Stage {target!r} exists in several pipelines; pass 'Pipeline/Stage' or the id.",
        )


class Leads(Namespace):
    """``client.leads`` — CRM leads."""

    def list(
        self,
        limit: int | None = 50,
        pipeline_id: str | None = None,
        stage_id: str | None = None,
        company_id: str | None = None,
        *,
        offset: int | None = None,
        stage: str | None = None,
        mesh: str | None = None,
    ) -> ItemList[dict[str, Any]]:
        """GET /api/leads — lead dicts (title, valueAmount, stageName, …).

        ``stage`` takes a stage NAME or id (resolved via pipelines); the
        0.3 ``stage_id`` still takes the id only.
        """
        if stage is not None:
            stage_id = self._c.pipelines.resolve_stage(stage, mesh=mesh)
        params = {"pipelineId": pipeline_id, "stageId": stage_id, "companyId": company_id}
        return self._c._list("leads.list", params=params, limit=limit, offset=offset, mesh=mesh)

    def get(self, lead_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """GET /api/leads/{id}."""
        return as_dict(self._c._call("leads.get", path={"lead_id": lead_id}, mesh=mesh))

    def create(
        self,
        title: str,
        pipeline_id: str | None = None,
        stage_id: str | None = None,
        value: float | None = None,
        description: str | None = None,
        *,
        stage: str | None = None,
        contact: str | None = None,
        company_id: str | None = None,
        currency: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/leads.

        Omit ``pipeline_id`` to use the mesh's default pipeline, and the
        stage for that pipeline's first stage (what the MCP's
        ``create_lead`` does). ``stage_id`` is an id, sent as-is; ``stage``
        takes a stage NAME or id and is resolved. ``contact`` takes a
        contact id, exact name or email.
        """
        if stage is not None:
            stage_id = self._c.pipelines.resolve_stage(stage, mesh=mesh)
        if pipeline_id is None:
            pipe = self._c.pipelines.default(mesh=mesh)
            pipeline_id = str(pipe["id"])
            if stage_id is None:
                stages = pipe.get("stages") or []
                if not stages:
                    raise MeshbookError(
                        "no_stage", f"Pipeline {pipe.get('name')!r} has no stages.", 404
                    )
                stage_id = str(stages[0]["id"])
        if stage_id is None:
            raise MeshbookError("bad_argument", "stage_id is required when pipeline_id is given.")
        body: dict[str, Any] = {"title": title, "pipelineId": pipeline_id, "stageId": stage_id}
        if value is not None:
            body["valueAmount"] = value
        put_if(body, "description", description)
        put_if(body, "currency", currency)
        put_if(body, "companyId", company_id)
        if contact:
            body["contactId"] = str(self._c.contacts.resolve(contact, mesh=mesh)["id"])
        return as_dict(self._c._call("leads.create", body=body, mesh=mesh))

    def update(
        self,
        lead_id: str,
        *,
        title: Any = UNSET,
        description: Any = UNSET,
        value: Any = UNSET,
        currency: Any = UNSET,
        contact: Any = UNSET,
        company_id: Any = UNSET,
        owner: Any = UNSET,
        expected_close_date: Any = UNSET,
        custom_fields: Any = UNSET,
        fields: Mapping[str, Any] | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/leads/{id} — only the arguments you pass are sent.
        Move stages with :meth:`move_stage` (it records the move)."""
        body: dict[str, Any] = {}
        put(body, "title", title)
        put(body, "description", description)
        put(body, "valueAmount", value)
        put(body, "currency", currency)
        if contact is not UNSET:
            body["contactId"] = (
                None if contact is None else str(self._c.contacts.resolve(contact, mesh=mesh)["id"])
            )
        put(body, "companyId", company_id)
        if owner is not UNSET:
            body["ownerUserId"] = None if owner is None else self._user_id(owner, mesh=mesh)
        if expected_close_date is not UNSET:
            body["expectedCloseDate"] = (
                None
                if expected_close_date is None
                else iso_date(expected_close_date, "expected_close_date")
            )
        put(body, "customFields", custom_fields)
        merge_fields(body, fields)
        if not body:
            raise MeshbookError("bad_argument", "Nothing to update: pass at least one field.")
        return as_dict(self._c._call("leads.update", path={"lead_id": lead_id}, body=body, mesh=mesh))

    def delete(self, lead_id: str, *, mesh: str | None = None) -> dict[str, Any]:
        """DELETE /api/leads/{id} (soft delete)."""
        return as_dict(self._c._call("leads.delete", path={"lead_id": lead_id}, mesh=mesh))

    def move_stage(
        self,
        lead_id: str,
        stage_id: str | None = None,
        note: str | None = None,
        *,
        stage: str | None = None,
        mesh: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/leads/{id}/move-stage. ``stage_id`` is an id (sent
        as-is); ``stage`` takes a stage NAME or id and is resolved."""
        if stage is not None:
            stage_id = self._c.pipelines.resolve_stage(stage, mesh=mesh)
        if not stage_id:
            raise MeshbookError("bad_argument", "Pass stage_id= or stage=.")
        body: dict[str, Any] = {"stageId": stage_id}
        put_if(body, "note", note)
        return as_dict(
            self._c._call("leads.move_stage", path={"lead_id": lead_id}, body=body, mesh=mesh)
        )
