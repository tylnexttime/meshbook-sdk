"""meshbook.config — the single owner of ``~/.meshbook/config``.

Until 0.4.0 three clients each carried their own copy of the config code
(this SDK read it; meshbook-cli and meshbook-mcp both read AND wrote it,
non-atomically, without a lock, in the locale encoding). This module is
now the one place that touches the file; the CLI and the MCP call only
:func:`config_dir`, :func:`config_path`, :func:`load_config`,
:func:`save_config` and :func:`reset_config`.

The file
--------
JSON, utf-8. Known keys:

=================  ============================================================
``token``          the long-lived ``mb_token_`` bearer (``mesh login``)
``base``           API base URL (apex domain; www 301s and downgrades POST)
``active_mesh_id`` the persisted active mesh (written only by ``meshes.use``)
``auth_mode``      ``"agent"`` when the enrolled key is the everyday login
``schema_version`` ``1`` — stamped on every write by this module
=================  ============================================================

Unknown keys are preserved on every write: :func:`save_config` merges
*updates* into what is on disk at the moment of writing, under the lock,
so a CLI and an MCP session writing different keys at once both land.

Directory resolution (unchanged from every earlier client, so existing
installs keep working):

1. ``MESHBOOK_CONFIG_DIR`` if set
2. ``~/.meshbook`` if it already exists (the legacy, canonical home)
3. ``$XDG_CONFIG_HOME/meshbook`` if ``XDG_CONFIG_HOME`` is set
4. ``~/.meshbook``

Writes are atomic (a temp file in the same directory, fsync, then
``os.replace``), guarded by ``config.lock`` beside the file (best-effort,
cross-platform, short timeout; a lock older than the stale threshold is
treated as left behind by a crashed process and broken), and land with
mode 600 on POSIX. Windows has no equivalent of that mode: the file
inherits the profile directory's ACL (DPAPI protection is noted as open
work, not done).
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .errors import MeshbookError

#: The schema version this module writes. Readers accept any version and
#: never downgrade a newer stamp they find on disk.
SCHEMA_VERSION = 1

#: The keys meshbook clients understand. Anything else in the file is
#: somebody else's and survives every write untouched.
KNOWN_KEYS = ("token", "base", "active_mesh_id", "auth_mode", "schema_version")

CONFIG_FILENAME = "config"
LOCK_FILENAME = "config.lock"

#: How long a writer waits for the lock before giving up (seconds).
LOCK_TIMEOUT = 5.0
#: A lock file older than this is assumed orphaned by a crashed writer.
#: Writes take milliseconds, so anything this old is not a live writer.
LOCK_STALE_AFTER = 15.0
_LOCK_POLL = 0.05


def config_dir() -> Path:
    """The meshbook config directory (see the module docstring for the order)."""
    explicit = os.environ.get("MESHBOOK_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    legacy = Path.home() / ".meshbook"
    if legacy.exists():
        return legacy
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "meshbook"
    return legacy


def config_path(directory: str | os.PathLike[str] | None = None) -> Path:
    """The config FILE path: ``<config_dir()>/config`` unless *directory* is given."""
    return (Path(directory) if directory is not None else config_dir()) / CONFIG_FILENAME


def _resolve(path: str | os.PathLike[str] | None) -> Path:
    return Path(path).expanduser() if path is not None else config_path()


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read the config file; ``{}`` when it is missing, unreadable or not a JSON object.

    Read as utf-8 (``utf-8-sig``, so a BOM left by a Windows editor is
    tolerated). Files written by older clients in the locale encoding are
    plain ASCII — ``json.dumps`` escapes everything else — so they read
    identically.
    """
    p = _resolve(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


@contextlib.contextmanager
def _locked(
    directory: Path,
    timeout: float = LOCK_TIMEOUT,
    stale_after: float = LOCK_STALE_AFTER,
) -> Iterator[Path]:
    """Hold ``config.lock`` in *directory* for the duration of the block.

    ``O_CREAT | O_EXCL`` is atomic on every local filesystem Python
    supports, which is all a best-effort lock needs. A lock older than
    *stale_after* seconds is broken (a crashed writer); a live one makes
    us wait up to *timeout* seconds and then raise ``config_locked``
    rather than write over another process's change.
    """
    lock = directory / LOCK_FILENAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # vanished between open and stat: just retry
            if age > stale_after:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                raise MeshbookError(
                    "config_locked",
                    f"{lock} is held by another process. If no meshbook client is "
                    "running, delete that file and try again.",
                ) from None
            time.sleep(_LOCK_POLL)
            continue
        try:
            with contextlib.suppress(OSError):
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        finally:
            os.close(fd)
        break
    try:
        yield lock
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically, mode 600 on POSIX from the first byte.

    ``mkstemp`` creates the temp file 0600, so the secret is never briefly
    world-readable the way ``write_text`` + ``chmod`` left it.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
        # Windows refuses os.replace while another process has the target
        # open; a reader holds it for microseconds, so retry briefly.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save_config(
    updates: Mapping[str, Any],
    path: str | os.PathLike[str] | None = None,
    *,
    lock_timeout: float = LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Merge *updates* into the config file and write it atomically.

    * A value of ``None`` DELETES that key (``save_config({"auth_mode": None})``
      is how ``mesh agent revoke`` clears the agent lane).
    * Every other key already on disk — known or not — is preserved.
    * The file is re-read under the lock, so the merge is against what is
      there NOW, not against whatever the caller loaded earlier.
    * ``schema_version`` is stamped (never downgraded).

    Returns the config as written. Raises :class:`MeshbookError`
    (``config_locked``) if another writer holds the lock past the timeout,
    or (``config_write_failed``) if the directory or file cannot be written.
    """
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise MeshbookError("config_write_failed", f"Cannot create {p.parent}: {e}") from e
    with _locked(p.parent, timeout=lock_timeout):
        current = load_config(p)
        for key, value in updates.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        existing_version = current.get("schema_version")
        if not isinstance(existing_version, int) or existing_version < SCHEMA_VERSION:
            current["schema_version"] = SCHEMA_VERSION
        try:
            _atomic_write_text(p, json.dumps(current, indent=2) + "\n")
        except OSError as e:
            raise MeshbookError("config_write_failed", f"Cannot write {p}: {e}") from e
    return current


def reset_config(
    path: str | os.PathLike[str] | None = None,
    *,
    lock_timeout: float = LOCK_TIMEOUT,
) -> bool:
    """Delete the config file (``mesh logout``). Returns True if a file was removed.

    Agent key files beside it are NOT touched: logging out of the bearer
    lane is not the same act as destroying a seat's only credential.
    """
    p = _resolve(path)
    if not p.parent.exists():
        return False
    with _locked(p.parent, timeout=lock_timeout):
        try:
            p.unlink()
        except FileNotFoundError:
            return False
        except OSError as e:
            raise MeshbookError("config_write_failed", f"Cannot remove {p}: {e}") from e
    return True


__all__ = [
    "CONFIG_FILENAME",
    "KNOWN_KEYS",
    "LOCK_FILENAME",
    "LOCK_STALE_AFTER",
    "LOCK_TIMEOUT",
    "SCHEMA_VERSION",
    "config_dir",
    "config_path",
    "load_config",
    "reset_config",
    "save_config",
]
