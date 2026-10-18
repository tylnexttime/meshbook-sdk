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
``os.replace``), serialised by an OS advisory lock on ``config.lock``
beside the file (``fcntl.flock`` / ``msvcrt.locking``; released by the OS
if a writer dies, short timeout, then ``config_locked``), and land with
mode 600 on POSIX. The lock file itself is left in place. Windows has no
equivalent of that mode: the file inherits the profile directory's ACL
(DPAPI protection is noted as open work, not done).

A file that cannot be read or parsed is re-read briefly (an older client
may be mid-rewrite) and then reported, never overwritten: an update is
refused with ``config_unreadable``, and :func:`quarantine_config` is the
explicit repair.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
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
#: Fallback lock only (no fcntl/msvcrt): an exclusive-create lock file
#: older than this is assumed orphaned by a crashed writer.
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


#: A config that fails to read or parse is re-read this many times, this
#: far apart, before it is judged broken: an older client (pre-0.4 CLI or
#: MCP) truncates and rewrites the file non-atomically and without the
#: lock, so a reader can catch it empty or half-written for an instant.
READ_ATTEMPTS = 4
READ_RETRY_DELAY = 0.1


def _unreadable(p: Path, why: str) -> MeshbookError:
    return MeshbookError(
        "config_unreadable",
        f"{p} exists but {why}. It was left untouched (it may still hold your token): "
        "fix it by hand, or move it aside with meshbook.config.quarantine_config() "
        "(`mesh login` then writes a fresh one).",
    )


def _read_parsed(p: Path) -> dict[str, Any]:
    """The config as a dict, retrying transient failures; never guesses.

    Missing -> ``{}``. Empty or whitespace after the retries -> ``{}``
    (there is nothing in it to lose). Still unreadable after the retries ->
    raises ``config_write_failed`` (an OS error) or ``config_unreadable``
    (not utf-8, not JSON, or not a JSON object).
    """
    problem: MeshbookError | None = None
    for attempt in range(READ_ATTEMPTS):
        if attempt:
            time.sleep(READ_RETRY_DELAY)
        try:
            text = p.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError:
            problem = _unreadable(p, "is not utf-8 text")
            continue
        except OSError as e:
            problem = MeshbookError("config_write_failed", f"Cannot read {p}: {e}")
            continue
        if not text.strip():
            problem = None  # empty: maybe mid-rewrite, so look again
            continue
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            problem = _unreadable(p, "is not valid JSON")
            continue
        if isinstance(loaded, dict):
            return loaded
        problem = _unreadable(p, "is not a JSON object")
    if problem is None:
        return {}
    raise problem


def load_config(
    path: str | os.PathLike[str] | None = None, *, strict: bool = False
) -> dict[str, Any]:
    """Read the config file.

    Read as utf-8 (``utf-8-sig``, so a BOM left by a Windows editor is
    tolerated). Files written by older clients in the locale encoding are
    plain ASCII (``json.dumps`` escapes everything else), so they read
    identically. A failed read or parse is retried briefly first (an older
    client may be mid-rewrite).

    Missing -> ``{}``. A file that exists but cannot be read or parsed ->
    ``{}`` by default (0.4.0 behaviour), or with ``strict=True`` a
    ``config_unreadable`` / ``config_write_failed`` error, so a caller can
    say "your config is broken" instead of "you have no token" (the client
    does this).
    """
    p = _resolve(path)
    try:
        return _read_parsed(p)
    except MeshbookError:
        if strict:
            raise
        return {}


def _read_for_update(p: Path) -> dict[str, Any]:
    """The current file, for a read-modify-write under the lock.

    Never guesses, because whatever comes back here is WRITTEN BACK: a
    config that cannot be read or parsed (after the retries) raises and
    nothing is written, so an update can never drop the token (critique
    C-04; 0.4.0 renamed it to config.bad and wrote only the update).
    Repair is explicit: :func:`quarantine_config`.
    """
    return _read_parsed(p)


def quarantine_config(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Move a config that cannot be parsed aside, so a fresh one can be written.

    The explicit repair for ``config_unreadable`` (``mesh login`` can call
    it when the user agrees). Under the lock, re-checks that the file is
    still unreadable (an older client may have finished its rewrite), then
    renames it to ``config.bad.<UTC timestamp>`` (``.1``, ``.2`` ... if
    that exists), never overwriting an earlier one, since it may hold the
    only copy of a token. Returns the new path, or None when there was
    nothing to quarantine (missing, or readable after all).
    """
    p = _resolve(path)
    if not p.parent.exists():
        return None
    with _locked(p.parent):
        try:
            _read_parsed(p)
            return None
        except MeshbookError as e:
            if e.code != "config_unreadable":
                raise
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        n = 0
        while True:
            target = p.with_name(f"{p.name}.bad.{stamp}" + (f".{n}" if n else ""))
            try:
                os.link(p, target)  # fails if target exists: no-clobber
            except FileExistsError:
                n += 1
                continue
            except OSError:  # no hard links on this filesystem
                if target.exists():
                    n += 1
                    continue
                os.replace(p, target)
                return target
            p.unlink()
            return target


if sys.platform == "win32":
    import msvcrt

    def _try_os_lock(fd: int) -> bool | None:
        """Non-blocking exclusive lock on byte 0: True held, False busy."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    _fcntl: Any
    try:
        import fcntl as _fcntl
    except ImportError:  # pragma: no cover - exotic platforms (WASI …)
        _fcntl = None

    def _try_os_lock(fd: int) -> bool | None:
        """Non-blocking exclusive flock: True held, False busy, None unsupported."""
        if _fcntl is None:  # pragma: no cover
            return None
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _os_unlock(fd: int) -> None:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)


def _busy(lock: Path) -> MeshbookError:
    return MeshbookError(
        "config_locked",
        f"{lock} is held by another meshbook process; try again in a moment.",
    )


@contextlib.contextmanager
def _locked(directory: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[Path]:
    """Hold ``config.lock`` in *directory* for the duration of the block.

    An OS advisory lock (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
    Windows) on a lock file that is never deleted. The OS releases it when
    the holder exits — even by crashing — so there is no staleness to
    guess at. (The first version created and deleted the file and broke
    "stale" locks by age; two waiters breaking the same stale lock could
    both end up "holding" it.) A busy lock makes us wait up to *timeout*
    seconds, then raise ``config_locked`` rather than write over another
    process's change. Platforms with neither primitive fall back to
    :func:`_locked_exclusive_create`.
    """
    lock = directory / LOCK_FILENAME
    try:
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise MeshbookError("config_write_failed", f"Cannot open {lock}: {e}") from e
    try:
        deadline = time.monotonic() + timeout
        while True:
            got = _try_os_lock(fd)
            if got is None:
                break
            if got:
                try:
                    yield lock
                finally:
                    with contextlib.suppress(OSError):
                        _os_unlock(fd)
                return
            if time.monotonic() >= deadline:
                raise _busy(lock)
            time.sleep(_LOCK_POLL)
    finally:
        os.close(fd)
    with _locked_exclusive_create(directory, timeout) as held:
        yield held


@contextlib.contextmanager
def _locked_exclusive_create(directory: Path, timeout: float) -> Iterator[Path]:
    """Fallback lock: ``O_CREAT | O_EXCL`` on ``config.lock.excl``, removed on
    release; one older than LOCK_STALE_AFTER is assumed orphaned. Best-effort
    by nature (two waiters can race to break the same stale file); used only
    where the OS offers no advisory lock."""
    lock = directory / (LOCK_FILENAME + ".excl")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError):
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0.0
            if age > LOCK_STALE_AFTER:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                raise _busy(lock) from None
            time.sleep(_LOCK_POLL)
            continue
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
    (``config_write_failed``) if the directory or file cannot be read or
    written, or (``config_unreadable``) if the existing file is not a JSON
    object even after a brief re-read. Nothing is written then; see
    :func:`quarantine_config`.
    """
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise MeshbookError("config_write_failed", f"Cannot create {p.parent}: {e}") from e
    with _locked(p.parent, timeout=lock_timeout):
        current = _read_for_update(p)
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
        # Quarantined copies and orphaned temp files may hold the token too:
        # logging out removes them (0.4.0 left config.bad behind).
        for leftover in [*p.parent.glob(p.name + ".bad*"), *p.parent.glob(".config.*.tmp")]:
            with contextlib.suppress(OSError):
                leftover.unlink()
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
    "READ_ATTEMPTS",
    "READ_RETRY_DELAY",
    "SCHEMA_VERSION",
    "config_dir",
    "config_path",
    "load_config",
    "quarantine_config",
    "reset_config",
    "save_config",
]
