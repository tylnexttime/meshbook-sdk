# TypeScript SDK build plan — `@meshbook/sdk`

*A half-day build plan for a future session. Written 2026-07-12 alongside
meshbook-sdk (Python) v0.1.0 — DEV-DEBT §34.*

## Goal

`@meshbook/sdk` on npm: a thin fetch-based client for meshbook.org that
mirrors the Python SDK's namespaces one-to-one, with request/response types
generated from the live OpenAPI schema instead of hand-written.

```ts
import { MeshbookClient } from "@meshbook/sdk";

const client = new MeshbookClient({ token: process.env.MESHBOOK_TOKEN });
const meshes = await client.meshes.listMine();
await client.chat.post("hello from TS");
```

## Recipe (in order)

### 1. Scaffold (~30 min)

- New repo `meshbook-sdk-ts` (or `packages/ts` if we ever monorepo — don't
  start there; a plain repo ships faster).
- `npm init` scoped `@meshbook/sdk`, `"type": "module"`, dual ESM/CJS via
  `tsup` (one dep, zero-config). Node >= 18 so global `fetch` is guaranteed —
  **no runtime dependencies**, matching the Python SDK's zero-dep discipline.
- `vitest` for tests, `typescript` strict.

### 2. Generate types from OpenAPI (~30 min)

```bash
npx openapi-typescript https://meshbook.org/api/openapi.json -o src/generated/api.d.ts
```

- Commit the generated file (reproducibility beats freshness; regenerate on
  each release with an npm script `npm run gen`).
- GOTCHA: FastAPI serves the schema at `/api/openapi.json` only if that's how
  main.py mounts it — verify with `curl -A "meshbook-sdk-ts/dev" first`; the
  fallback is `/openapi.json`. If the route turns out to be behind Cloudflare
  bot rules, generate from a local checkout of the meshbook repo instead
  (`python -c "import json; from app.main import app; print(json.dumps(app.openapi()))"`).
- GOTCHA: many meshbook endpoints return the envelope as a generic dict in
  the schema (`ok()` returns are not fully typed server-side). The generated
  types get you paths + params + request bodies for free; response payloads
  will often be `Record<string, unknown>`. That's fine — mirror the Python
  SDK: hand-write small interfaces ONLY for the four stable shapes
  (`User`, `Mesh`, `ExportJob`, `Attachment`) and leave the rest as
  `Record<string, unknown>` (documented), exactly like Python returns dicts.

### 3. Core client (~1 h) — port `meshbook/client.py` semantics

One file, `src/client.ts`:

- `MeshbookClient({ token?, base?, activeMeshId?, timeoutMs? })`
  - token resolution: explicit → `MESHBOOK_TOKEN` env (guard `typeof process
    !== "undefined"` so the browser build doesn't crash) → error on first
    authed call. **No config-file reading in TS** — `~/.meshbook/config` is a
    CLI/Python affordance; Node users pass the token explicitly. (If demand
    appears, add an optional `fromCliConfig()` helper behind a dynamic
    `node:fs` import so the browser bundle stays clean.)
  - base default `https://meshbook.org` — **apex only**: www 301s and the
    redirect downgrades POST to GET. Never default to www.
- Headers on every request — copy these exactly, they are load-bearing:
  - `User-Agent: meshbook-sdk-ts/<version>` — **Cloudflare blocks default
    UAs**; in browsers UA is not settable, so ALSO send
    `X-Meshbook-Client: meshbook-sdk-ts/<version>` and don't fail if UA
    couldn't be set.
  - `Authorization: Bearer <token>`
  - `X-Active-Mesh-Id: <uuid>` whenever set — most CRM/chat surfaces are
    mesh-scoped and 4xx without it.
  - `Content-Type: application/json` on bodies; `Accept: application/json`.
- **Envelope unwrap** (the single most important port):
  - success: `{ok: true, data}` → return `data`
  - lists: `{ok: true, data: {items, total}}` → return `items` (this is the
    `ok_list` shape; also tolerate bare arrays and `{data: [...]}`)
  - failure: non-2xx OR `{ok: false, error: {code, message}}` in a 200 →
    `throw new MeshbookError(code, message, status)`; parse the JSON error
    body of non-2xx responses for `error.code`/`error.message` before falling
    back to `http_error` + first 200 chars.
- `MeshbookError extends Error { code: string; status: number }`.
- Downloads (`chat.download`, `files.download`, `exports.download`): NOT
  JSON — `res.arrayBuffer()`, filename from `Content-Disposition`
  (`filename*=UTF-8''…` wins over `filename="…"`). In Node write with
  `node:fs`; in browser return a `Blob` + suggested filename instead.

### 4. Namespaces (~1 h) — mirror Python exactly

Same names, camelCased methods. Endpoint map (verified against
meshbook-cli v0.6.0 and the Python SDK — do not re-derive):

| Namespace  | Method                    | Wire call |
|------------|---------------------------|-----------|
| meshes     | `listMine()`              | `GET /api/meshes` |
| meshes     | `use(nameOrId)`           | resolve via listMine, set `activeMeshId` (in-memory) |
| contacts   | `list({q?, limit?})`      | `GET /api/contacts?search=&limit=` |
| contacts   | `create({...})`           | `POST /api/contacts` `{firstName, lastName, primaryEmail, company}` |
| leads      | `list({...})`             | `GET /api/leads?pipelineId=&stageId=&companyId=&limit=` |
| leads      | `create({...})`           | `POST /api/leads` `{title, pipelineId, stageId, valueAmount?, description?}` |
| leads      | `moveStage(id, stageId)`  | `POST /api/leads/{id}/move-stage` `{stageId}` |
| tasks      | `list({...})` / `listMine()` | `GET /api/tasks?assigneeId=…` (self id via `GET /api/me`, cache it) |
| tasks      | `done(id, status="Done")` | `PATCH /api/tasks/{id}` `{status}` |
| chat       | `post(msg, {replyTo?})`   | `POST /api/entities/mesh/{activeMeshId}/chat` `{bodyMd, parentMessageId?}` |
| chat       | `list({limit?})`          | `GET /api/entities/mesh/{activeMeshId}/chat` |
| chat       | `attach(messageId, file)` | `POST /api/chat-messages/{id}/attachments/json` `{filename, mimeType, base64Bytes}` |
| chat       | `download(attachmentId)`  | `GET /api/chat-attachments/{id}/download` |
| channels   | `list()`                  | `GET /api/meshes/{activeMeshId}/channels` |
| channels   | `read(ch, {limit?})`      | `GET /api/channels/{id}/messages` (resolve `#name` case-insensitively via list) |
| channels   | `post(ch, msg)`           | `POST /api/channels/{id}/messages` `{bodyMd}` |
| notifications | `list()`               | `GET /api/notifications` |
| files      | `attach(type, id, file)`  | `POST /api/entities/{type}/{id}/attachments/json` (base64, no multipart) |
| files      | `list(type, id)`          | `GET /api/entities/{type}/{id}/attachments` |
| files      | `download(attachmentId)`  | `GET /api/entity-attachments/{id}/download` |
| files      | `delete(attachmentId)`    | `DELETE /api/entity-attachments/{id}` |
| exports    | `start(meshId)`           | `POST /api/meshes/{id}/export` — MUST send `X-Active-Mesh-Id: <same meshId>` (server rejects a mismatch with `mesh_mismatch`) |
| exports    | `list(meshId)`            | `GET /api/meshes/{id}/exports` |
| exports    | `download(exportId)`      | `GET /api/mesh-exports/{id}/download` (409 not ready, 410 expired) |

### 5. Tests (~1 h)

- Vitest + a fetch stub (`vi.stubGlobal("fetch", …)`) — the transport seam,
  same philosophy as the Python suite: no live HTTP, assert exact method /
  path / headers / JSON body for one representative method per namespace,
  plus the three envelope shapes and the error mapping.
- Port the Python test table directly from
  `tests/test_client.py` — it IS the wire-contract spec.

### 6. Publish (~30 min)

- `npm publish --access public` under the `@meshbook` org (create the org
  on npmjs.com first; add `tylnexttime` as owner).
- Prefer npm **trusted publishing / provenance** via GitHub Actions
  (`npm publish --provenance`), mirroring the PyPI OIDC setup: CI workflow
  with `permissions: id-token: write`, publish job gated on `v*` tags.
- README: same five examples as the Python SDK, translated.

## Definition of done

- `npm i @meshbook/sdk` + 5 README examples run against production with a
  real token.
- One vitest per namespace green in CI on Node 18/20/22.
- Bundle has zero runtime deps; `tsup` output < 20 kB.

## Known deferrals (fine for 0.1)

- No retry/backoff (nor in Python — add to both together or neither).
- No async iterators/pagination helpers (`ok_list` `total` is returned but
  unused; the server caps list sizes anyway).
- No websocket/live-updates surface — that's a different §.
- Browser story is "works if CORS allows it" — meshbook currently serves
  same-origin SPA; cross-origin browser use needs a server CORS decision
  first. Don't block the Node release on it.
