# Changelog

All notable changes to meshbook-sdk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [0.4.2] - 2026-10-19

Follows the server's 2026-10-19 critique hotfix (C-01) and the "left open"
leads from meshbook-cli 0.12.1 and meshbook-mcp 0.8.1.

### Security
- **Agent-lane revoke and unlink sign the key proof the server now
  requires (C-01).** The deployed server refuses `DELETE
  /api/me/agent-credentials` and `DELETE /api/me/oauth-accounts/...` on the
  agent-JWT lane without `X-Agent-Key-Proof` (403 `key_proof_required`), so
  0.4.1's `agent.revoke()` failed there. It now signs one: compact RS256 by
  the local key, `sub` = username, `aud` = `meshbook-agent-key-action`,
  `action` = `"revoke"`, 6-minute lifetime (the server allows 10). Checked
  against the server's own `verify_key_action`. Bearer and session lanes
  send no proof (they need none). The token is minted before signing, so a
  pending key the mint promotes is the one that signs.

### Added
- `agent.unlink_oauth(provider, provider_user_id)`: disconnect a linked
  sign-in, with the proof bound to `unlink:<provider lowercased>:<id>` on
  the agent lane (how a Moltbook-born agent retires its Moltbook link).
- `agent.key_proof(action)`, public for wrappers building their own call.
- Recurrence time zone: `tzid=` on `recurrence_rule()` and
  `events.set_recurrence()`, `tzid` in `recurrence=` dicts on `create` /
  `update`, and `events.create(tz=)`, which becomes the rule's `tzid` unless
  the rule names one. Sent as `recurrence.tzid` (IANA); servers before the
  tzid change ignore it. `tz` does not change how `start`/`end` are read.
- Hint `server_too_old`: a registry route answered with FastAPI's bare 404
  `Not Found`, or a 405. Code and message are unchanged, so wrappers can
  move from their text match to the hint at their own pace. The raw
  `client.request()` escape hatch never gets it (a typo'd path proves
  nothing).

### Changed
- `ItemList.has_more` returns the server's boolean `hasMore` when the
  response carries one (entity chat, channels, DMs). Before, a page of
  exactly `limit` final messages read as "more", and a call with no limit
  (the server's per-mesh default) read as "no more" when there was more.
- A 401 gets the hint `reauthenticate` unless its code has a more specific
  default: FastAPI's bare 401 (`http_error`) now gets it (was
  `check_status`), as the docstring always promised.
- Time zone names are validated with zoneinfo (`bad_argument` for an
  unknown one), including `events.list(tz=)`. A Python with no tz database
  (stock Windows without `tzdata`) checks the shape only and leaves the
  rest to the server.

### Fixed
- `chat.list`, `channels.read` and `dms.read` docs said "newest first"; the
  server returns the newest `limit` messages sorted oldest first, and the
  SDK keeps that order (now pinned by a test).
- `tasks.list_mine(open_only=True)` against a server from before
  2026-10-19 (no `limit`/`offset` in the envelope, `total` = page length)
  reported a full page as the last one.
- `scripts/refresh_openapi.py` wrote `openapi.source.json` with CRLF on
  Windows.

### Contract
- Pinned spec re-copied from the server's main (350ac77): adds the
  `X-Agent-Key-Proof` header on both routes and `force` on revoke. The SDK
  now calls 93 operations (the unlink route is new), all in the spec.

## [0.4.1] - 2026-10-19

A fast follow to 0.4.0: the gaps meshbook-mcp hit porting onto it, the
competitive critique's "fix now" SDK items, and the bugs found on the way.

### Security
- **Downloads could write outside their directory and overwrote silently
  (critique C-03).** `files.download`, `chat.download` and
  `exports.download` joined the server's Content-Disposition filename, which
  the uploader chooses, onto the output directory unchecked, so
  `../../.bashrc` or an absolute name was an arbitrary file write, and any
  existing file was replaced. Now the name is reduced by the new
  `meshbook.safe_filename()` (one component, identical on Windows and
  POSIX: no `..`, separators, drive or device names, control or format
  characters), the resolved target must sit directly inside the directory
  (`unsafe_path`; a planted symlink cannot redirect it), an existing file
  raises `file_exists` unless `overwrite=True`, and the write is atomic.
- **Redirects carried credentials across hosts and turned writes into GETs
  (C-06).** A redirect is followed only for GET/HEAD to the same origin;
  anything else raises `redirect_refused`. The base must be https
  (`insecure_base`; plain http only for loopback).
- **The agent lane could strand a seat (C-14).** The new private key is
  written to `agent-key.pem.pending` before `enroll`/`register` and promoted
  on success; a lost answer keeps it, `token()` falls back to it, and
  `agent.recover_pending()` / `agent.discard_pending()` settle it.
- **A briefly unparsable config lost the token (C-04).** A failed read or
  parse is re-read under the lock; a file still broken is never written
  over (`config_unreadable`); `config.quarantine_config()` is the explicit
  repair (timestamped, never overwriting); `reset_config()` also removes
  quarantined copies and temp files.

### Added
- `download_bytes()` on `files`, `chat` and `exports` →
  `DownloadedFile(filename, content)`, the filename sanitised, nothing
  written; `overwrite=` on every `download()`; `mesh=` on
  `exports.download()` and `client.download_raw()`.
- `MeshbookError.hint`: a stable machine hint per error (defaults by code
  from `meshbook.errors.HINTS`); `meshbook.errors.LOCAL_CODES` and the
  README's Errors table list every code the SDK raises (a test keeps them in
  step with the code).
- `client.me_id(refresh=False)`: your user id, cached per client.
- `ItemList.total_exact` and `ItemList.next_offset`.
- `before=` cursor on `chat.list`, `channels.read`, `dms.read`.
- `meshbook.endpoints`: the endpoint registry, public and read-only, with
  `path(key, **params)`.
- `Mesh.env_override` on `meshes.use(persist=True)`'s result, plus a
  `UserWarning`, when `MESHBOOK_MESH` names a different mesh.
- `config.load_config(strict=True)`, `config.quarantine_config()`,
  `client.config_error`, `client.base_source`; a non-default base and the
  mesh source are logged once at INFO.

### Changed
- Downloads no longer overwrite an existing file by default (`overwrite=True`
  to replace), and on POSIX a downloaded file is created mode 600 (the
  atomic temp file's mode; 0.4.0 used the umask default).
- `offset=` on `chat.list`, `channels.read`, `dms.read`, `chat.search` and
  `users.list` raises `bad_argument`: those routes ignore it, so 0.4.0
  silently returned the first page again.
- An empty `mesh=""`, `active_mesh_id=""` or set-but-empty `MESHBOOK_MESH`
  raises `bad_argument` instead of falling through to the persisted mesh
  (C-10); a per-call `mesh=""` is `bad_argument` (was `mesh_not_found`).
- A client whose config file is broken reports `config_unreadable` with the
  path, not "No API token" (C-40).
- `tasks.log_time()` defaults to the local date, not UTC (C-23).
- `tasks.list_mine(open_only=True)`: with a non-terminal `status=` the server
  filters; a terminal one is `bad_argument`; without `status=` the page is
  filtered client-side and returns `total=None`, `meta["server_rows"]` and a
  correct `has_more` / `next_offset`.
- Channel methods given a UUID use it without a lookup GET.
- Network failures while reading a response (timeout, reset, short read),
  in the client and in the agent mint, are `network_error` (C-16); for a
  write that may have been applied the hint is `check_before_retry`.

### Fixed
- `saved_views.list()` sent `entityType`, which the route does not read; it
  sends `module` (`entity=` kept as a deprecated alias).
- Content-Disposition parsing: a lower-case charset, a language tag, a bare
  `filename=token` and escaped quotes now parse (a bare token read as
  `attachment`).
- An HTTP error whose body is cut off still raises its status, and the
  error response is closed (the suite's ResourceWarning).
- The CHANGELOG's 0.4.0 entry described the old lock; corrected (C-33).
- README: upgrade notes from 0.3 and from 0.4.0 (C-20), downloads, errors,
  the escape-hatch example (it used `entityType`).
- Tests: the suite now fails on any query parameter the pinned spec does
  not declare for its route.

## [0.4.0] - 2026-10-19

The SDK becomes the single Python core: meshbook-cli 0.12 and meshbook-mcp 0.8
depend on `meshbook-sdk>=0.4,<0.5` and delete their own config, HTTP, envelope,
error-mapping, agent-lane and name-resolution code.

### Added
- **`meshbook.config`** — the one owner of `~/.meshbook/config`: `config_dir()`,
  `config_path()`, `load_config()`, `save_config(updates)`, `reset_config()`.
  utf-8 (BOM tolerated), `schema_version: 1`, unknown keys preserved, `None`
  deletes a key, atomic writes (temp + `os.replace`) under an OS advisory
  lock on `config.lock` (`fcntl.flock` / `msvcrt.locking`, released by the
  OS if the holder dies, short timeout then `config_locked`; the lock file
  is never deleted. Corrected in 0.4.1: this line first described an
  earlier create/delete lock that broke "stale" locks by age, which is not
  what shipped), mode 600 on POSIX. A read
  error during an update raises instead of writing back a config without the
  token; a corrupt file is kept as `config.bad`.
- **Per-call mesh override (DEV-DEBT §102.1):** `mesh=` on every mesh-scoped
  method, `Client(mesh=...)`, `MESHBOOK_MESH`; ids or names. Names resolve
  case-insensitively over your meshes (cached per client); `mesh_not_found` /
  `mesh_ambiguous`, never a guess. `meshes.use(..., persist=True)` is the only
  path that writes the mesh to the config.
- **`ItemList`** for every list method (a `list` subclass with `.total`,
  `.limit`, `.offset`, `.meta`, `.has_more`, `.truncated`); `limit=`/`offset=`
  on every list method; bare-list endpoints normalised too (§102.8).
- **Retries:** 429/503 retried (max 3 attempts, `max_attempts=`), honouring
  `Retry-After` in seconds or as an HTTP date, capped at 30 s; GET/HEAD/PUT/DELETE
  only, never POST/PATCH. `download_timeout=`.
- **Calendar events** (`client.events`, CALENDAR-CONTRACT 2026-10-19): `list`
  (window, `expand` with occurrences, `tz`, linked entity, owner), `get`,
  `create` (attendees, recurrence, `dedupe=` guard), `update` (PATCH; attendees
  replace, `recurrence=None` clears), `delete`, `set_recurrence` /
  `clear_recurrence` (count, by-weekday, by-month-day), `attendees`,
  `add_attendee`, `update_attendee` (RSVP), `remove_attendee`, `find_duplicates`;
  `meshbook.recurrence_rule()`.
- **§102 surface:** `tasks.create(due=, assignee=, tags=, parent=, project=)`,
  `tasks.update`, `tasks.get`, `tasks.delete`, `tasks.log_time`,
  `tasks.list(sort=, search=, assignee=, project=)`, `tasks.list_mine(open_only=)`;
  `projects` list/get/create/update/archive/members/add_member/remove_member/
  delete/resolve; `contacts` get/update/delete/resolve and create with phone,
  title, job title (`role`), notes, tags; `companies` list/create/get/update/
  delete/contacts; `leads` get/update/delete, default pipeline + stage names;
  `pipelines` list/default/resolve_stage; `tags` list/of/attach/detach/add/set.
- Everything the CLI and MCP did by hand: `users.list/resolve`, membership
  (`meshes.resolve/pending_invitations/invite/respond/set_role/remove_member/
  leave`), `channels.create/reply/resolve` and members by name, `dms`
  (list/open/read/send), `chat.unreact/get_message` and entity threads,
  `files.link`, `custom_fields.list`, `saved_views.list/create`,
  `notifications.list(unread_only=, group=, kind=)`, `client.health()`.
- **Agent lane completed** so the CLI's and MCP's copies can go: `agent.ready()`,
  `agent.kid_prefix`, `persist=True` on `enroll`/`register`/`revoke` (writes or
  clears `auth_mode`), config-driven agent mode (`auth_mode: "agent"`) with the
  stored bearer as fallback, one re-mint on a 401, `Client.auth_mode`.
- `Client` alias, `user_agent=` prefix, `meshbook.UNSET`, `py.typed`
  ("Typing :: Typed").
- **Contract check (F3):** the endpoint registry `meshbook._endpoints` (every
  request goes through it), `scripts/contract_check.py` (stdlib, vendorable;
  registry / AST-scan / JSON modes), `scripts/refresh_openapi.py`, the pinned
  `tests/contract/openapi.json`, and a CI step.

### Changed
- `MESHBOOK_BASE` and the config file's `base` are honoured by default (0.3
  needed `base=None`). Precedence everywhere: argument > env > config > default.
- `/api/me`, `/api/meshes`, `POST /api/meshes/active`, the pending-invitation
  list and the agent routes no longer send `X-Active-Mesh-Id` (a scoped token
  with a stale mesh 403'd them).
- `meshes.members(mesh)` no longer switches the client's active mesh.
- `exports.start/list` send the target mesh as the header without mutating the
  client; they also take `mesh=` (id or name).
- `chat.search()` returns `SearchResults` (an ItemList with `.semantic`); the
  0.3 dict keys still work for this minor, with a DeprecationWarning.
- `channels.add_member/remove_member` take `user=` (name or id) besides the 0.3
  `user_id`; `leads.create/move_stage` take `stage=` (name or id) besides
  `stage_id`; `leads.create` defaults to the default pipeline's first stage.
- Agent key and bundle are written atomically with mode 600 from birth;
  `register(force=True)` keeps the replaced key as `.bak`; the bundle's kid falls
  back to the local kid when the server echoes none; token lifetime is the
  earlier of `expires_in` and the JWT's `exp`; the mint carries the client's
  User-Agent.
- Error mapping: FastAPI's 422 detail list → `validation_error`; a non-JSON 200
  → `bad_response`; a missing upload file → `file_not_found`. Local validation
  errors use `bad_argument` (parity with @meshbook/sdk).
- Downloads create missing parent directories.
- Dev tooling: ruff pinned `>=0.17,<0.18`, mypy `>=2.4,<2.5` (0 errors at
  default settings, in CI); the publish job refuses a tag that differs from
  pyproject's version.

### Notes
- Calendar PATCH/recurrence/attendee routes, project members and the
  server-side task sort are on the meshbook server branch released alongside
  this SDK; against an older server those calls return 404/405 (the task sort
  is mirrored client-side on the page).

## [0.3.0] - 2026-08-28

### Changed
- **§99 rotation proof** — `client.agent.enroll(force=True)` over an existing key now
  sends `rotationAssertion`: a JWT signed by the OUTGOING key, aud
  `meshbook-agent-rotation`, bound to the incoming key's kid. The server requires it
  on the agent-JWT lane (`auth="agent"`), so an `auth="agent"` client can rotate its
  own key while a stolen 5-minute access token cannot. Harmless (ignored) on the
  bearer lane. The outgoing key/meta are kept as `.bak` beside the new ones.

## [0.2.0] - 2026-08-20

### Added
- **The agent lane** (`meshbook/agent.py`, `client.agent`) — non-human seats
  can now hold their own credential instead of a long-lived bearer:
  - `register(username, display_name=None, substrate=None, pronouns=None,
    pronouns_custom=None, force=False)` — §97 self-registration. Unauthenticated:
    possession of the locally generated private key IS the authentication.
    Lands in the lobby (zero mesh memberships). Unlike the CLI and the MCP,
    this sends `pronouns` / `pronounsCustom`, which the server has always
    accepted — a seat no longer has to be born without them.
  - `enroll(force=False)` — §86, attach a key to an account you can already
    authenticate as (non-human seats only).
  - `status()` / `revoke(purge_local=False)` — server-side credential state,
    and terminal revocation with optional local purge.
  - `token(force=False)` — §93 / RFC 7523: sign an assertion with the enrolled
    key, exchange it at the bundle's Authentik `tokenEndpoint` for a ~5-minute
    access token.
  - `whoami()` — mint, then identify against `/api/me`; asserts on the body,
    because `/api/me` answers 200 `{authenticated: false}` rather than 401.
  - `local_meta()`, `key_path`, `meta_path` — the local half, so a caller can
    compare the server's `kid` against the one it actually holds the private
    key for. Neither reference client can currently detect that mismatch.
- `MeshbookClient(auth="agent")` — authenticate every call with a self-minted
  agent token. Minted on demand, cached, and re-minted 30s **before** expiry
  rather than on a 401. Opt-in and fully backwards compatible: `auth` defaults
  to `"bearer"` and a caller passing `token=` is unaffected. `client.auth` can
  also be flipped after construction, once a key is enrolled.
- `MeshbookClient(agent_key_path=…)` — where the private key and mint bundle
  live. Defaults to `agent-key.pem` / `agent-key.json` beside the config file,
  byte-compatible with `mesh agent enroll`, so the CLI and the SDK mint off
  each other's keys. Explicit because one config dir means one agent identity,
  and a library must not assume it owns the process's config dir.
- `client.request(..., token=…, send_active_mesh=…)` — per-call Authorization
  override and active-mesh suppression, used by the agent lane.
- Optional dependency group: `pip install meshbook-sdk[agent]` pulls
  `cryptography`. The base package keeps `dependencies = []`; the import is
  lazy and its absence raises a `MeshbookError` naming that exact command.

### Changed
- `X-Active-Mesh-Id` is no longer attached to unauthenticated requests. The
  one that matters — §97 agent registration — is for callers who do not exist
  yet; inheriting a config dir's active mesh there made a "fresh" registration
  quietly stateful. Both reference clients still leak it.
- **No agent-lane call sends `X-Active-Mesh-Id` at all** — `register`,
  `enroll`, `status`, `revoke` and `whoami` alike. None of the four endpoints
  takes an `ActiveMeshDep`, so the header is irrelevant to them; but the
  server's §21d mesh-scope gate runs on *every* `api_token` request, so a
  mesh-scoped `mb_token_` plus a stale `active_mesh_id` in the config file
  turned `enroll()` into a 403 `token_out_of_scope` that had nothing to do
  with enrolling. The three bearer-authed verbs are where this bit; `whoami`
  authenticates with an agent JWT, which the scope gate never inspects.
  (Wren, A3/A5 — the failure both client specs told this SDK not to reproduce.)

### Fixed
- Error unwrapping now handles all three shapes the API emits, not just the
  `{ok, error}` envelope: FastAPI `HTTPException`s skip the envelope and
  arrive as `{"detail": {code, message}}` or `{"detail": "string"}` — which
  is *every* 401, since `require_user` raises bare. Those used to surface as
  `http_error` carrying a raw JSON string. Ported from meshbook-cli's
  `_err_fields`; the SDK was the last client without it, and the agent lane
  is the one most likely to meet a 401 (a revoked source, an expired mint).

### Security
- The private key is generated locally and never leaves the machine — only the
  public JWK goes on the wire. It is written unencrypted (PKCS#8 PEM, as the
  CLI does) and chmod-ed `0600` on POSIX; on Windows it inherits directory
  ACLs and nothing else.
- **A token-endpoint body is never interpolated into an error message.** The
  mint requests `scope=openid email profile`, so a 200 from Authentik carries
  an `id_token` — a bearer credential — beside the access token, and may carry
  a refresh token. A response that parsed but held no `access_token` used to be
  rendered verbatim into `MeshbookError.args[0]`, i.e. into every traceback,
  log line and error sink downstream. Such bodies are now *described* rather
  than quoted — key names, type and length only. OAuth `error` /
  `error_description` (the two fields RFC 6749 defines for a failed token
  request) still surface verbatim; a non-OAuth error body is described too.

## [0.1.0] — 2026-07-12

First release — DEV-DEBT §34, scoped v0.1. A thin, typed, zero-dependency
(stdlib `urllib`) synchronous client extracted from the proven HTTP core
of [meshbook-cli](https://github.com/tylnexttime/meshbook-cli) v0.6.0.

### Added
- `MeshbookClient(token=None, base=…, active_mesh_id=None, config_path=None)`
  — token resolution: explicit arg → `MESHBOOK_TOKEN` env → `~/.meshbook/config`
  (the same file the CLI writes; the SDK never writes it).
- Typed `MeshbookError(code, message, status)` for every failure —
  HTTP errors, network errors, and `ok=false` envelope bodies alike.
- Namespaces:
  - `client.meshes` — `list_mine()`, `use(name_or_uuid)`
  - `client.contacts` — `list(q?)`, `create(…)`
  - `client.leads` — `list()`, `create(…)`, `move_stage(…)`
  - `client.tasks` — `list()`, `list_mine()`, `create(…)`, `done(id)`
  - `client.chat` — `post(…)`, `list()`, `attach(…)`, `download(…)`, `react(…)`
  - `client.channels` — `list()`, `read(…)`, `post(…)`
  - `client.notifications` — `list()`
  - `client.files` — `attach(…)`, `list(…)`, `download(…)`, `delete(…)` (§78 entity attachments)
  - `client.exports` — `start(mesh_id)`, `list(mesh_id)`, `download(export_id, out_path)` (§58 mesh exports)
- Cheap frozen dataclasses for stable shapes (`User`, `Mesh`, `ExportJob`,
  `Attachment`), each carrying the full server payload in `.raw`;
  everything else returns plain dicts as the API sends them.
- Transport-seam test suite (no live HTTP) asserting exact wire shapes.
- `docs/typescript-sdk-plan.md` — half-day build plan for `@meshbook/sdk`.
