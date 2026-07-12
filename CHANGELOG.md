# Changelog

All notable changes to meshbook-sdk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

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
