"""Backup archives: create, list, restore and prune timestamped .tar.gz files.

Pure file handling (no Docker, no HTTP). Runs as the GameDock user with no sudo,
so files owned by another user (typically root, written by a game container) cannot
be read; that is reported as a clear AccessError and nothing is saved.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .errors import AccessError, OperationError, ValidationError

KINDS = ("pre-restore", "pre-update", "pre-wipe", "pre-delete")
NAME_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<ts>\d{8}-\d{6})(?:-(?P<kind>" + "|".join(KINDS) + r"))?(?:-(?P<n>\d+))?\.tar\.gz$"
)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$")


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int
    created_at: str  # ISO 8601, UTC
    kind: str  # "manual" or one of KINDS (automatic safety backups)
    path: str = ""


def _info(path: Path) -> BackupInfo | None:
    m = NAME_RE.match(path.name)
    if not m or not path.is_file() or path.is_symlink():
        return None
    ts = datetime.strptime(m["ts"], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    return BackupInfo(path.name, path.stat().st_size, ts.isoformat().replace("+00:00", "Z"),
                      m["kind"] or "manual", str(path))


def _sort_key(b: BackupInfo):
    m = NAME_RE.match(b.name)
    return (m["ts"], int(m["n"] or 1))  # type: ignore[index]


def list_backups(directory: Path) -> list[BackupInfo]:
    """Backups in `directory`, newest first."""
    if not directory.is_dir():
        return []
    items = [i for p in directory.iterdir() if (i := _info(p))]
    return sorted(items, key=_sort_key, reverse=True)


def backup_path(directory: Path, name: str) -> Path:
    """Resolve a backup name inside `directory`, refusing anything that is not a plain backup file name."""
    if not SAFE_NAME_RE.match(name or "") or not NAME_RE.match(name):
        raise ValidationError("Not a valid backup name")
    p = directory / name
    if not p.is_file() or p.is_symlink():
        from .errors import NotFoundError
        raise NotFoundError(f"No backup named '{name}'")
    return p


def _unique_name(directory: Path, prefix: str, kind: str | None, now: datetime) -> str:
    stem = f"{prefix}-{now.strftime('%Y%m%d-%H%M%S')}" + (f"-{kind}" if kind else "")
    name, n = f"{stem}.tar.gz", 2
    while (directory / name).exists():
        name = f"{stem}-{n}.tar.gz"
        n += 1
    return name


def create_archive(source: Path, directory: Path, prefix: str, now: datetime, kind: str | None = None) -> BackupInfo:
    """Archive the contents of `source` into directory/<prefix>-<timestamp>[-kind].tar.gz.

    Atomic: the file is written under a temporary name and renamed when complete, and nothing is left
    behind on failure.
    """
    if not source.is_dir():
        raise ValidationError("There is no data folder to back up yet")
    directory.mkdir(parents=True, exist_ok=True)
    name = _unique_name(directory, prefix, kind, now)
    tmp = directory / f".{name}.partial"
    unreadable: list[str] = []

    def on_walk_error(e: OSError) -> None:
        unreadable.append(str(getattr(e, "filename", "") or e))

    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for root, dirs, files in os.walk(source, followlinks=False, onerror=on_walk_error):
                for entry in [*dirs, *files]:
                    full = os.path.join(root, entry)
                    arc = os.path.relpath(full, source)
                    try:
                        tar.add(full, arcname=arc, recursive=False)
                    except PermissionError:
                        unreadable.append(full)
                    except FileNotFoundError:
                        pass  # vanished while we were reading (log rotation etc.)
            if unreadable:
                raise AccessError(
                    f"Cannot back up: {len(unreadable)} file(s) could not be read, for example "
                    f"{unreadable[0]}. They are probably owned by another user (such as root, because the game "
                    f"container wrote them). Fix with: sudo chown -R $(id -u):$(id -g) '{source}' "
                    f"and try again. Nothing was saved.")
        os.replace(tmp, directory / name)
    except (AccessError, ValidationError):
        tmp.unlink(missing_ok=True)
        raise
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise OperationError(f"Backup failed: {e.strerror or e}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    info = _info(directory / name)
    assert info is not None
    return info


def prune(directory: Path, keep: int, protect: set[str] = frozenset()) -> list[str]:
    """Delete all but the newest `keep` backups (never those named in `protect`). Returns removed names."""
    removed = []
    for b in list_backups(directory)[max(keep, 1):]:
        if b.name in protect:
            continue
        try:
            Path(b.path).unlink()
            removed.append(b.name)
        except OSError:
            pass
    return removed


def _check_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for m in members:
        p = PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts:
            raise ValidationError(f"Backup contains an unsafe path ({m.name!r}); refusing to restore it")
        if m.islnk() or m.isdev():
            raise ValidationError(f"Backup contains an unsupported entry ({m.name!r}); refusing to restore it")
        if m.issym():
            target = PurePosixPath(m.linkname)
            resolved = os.path.normpath(str(p.parent / target)) if not target.is_absolute() else None
            if resolved is None or resolved.startswith(".."):
                raise ValidationError(f"Backup contains a link that points outside the data ({m.name!r}); refusing to restore it")
    return members


def restore_archive(archive: Path, target: Path) -> list[str]:
    """Replace the contents of `target` with the archive. Returns warnings.

    The archive is validated and unpacked next to the target first, then swapped in, so a bad
    archive never damages the current data.
    """
    warnings: list[str] = []
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{target.name}.restore-{os.getpid()}"
    old = parent / f".{target.name}.replaced-{os.getpid()}"
    for leftover in (tmp, old):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = _check_members(tar)
            tmp.mkdir()
            try:
                tar.extractall(tmp, members=members, filter="data")  # type: ignore[call-arg]
            except TypeError:  # Python without extraction filters; members were validated above
                tar.extractall(tmp, members=members)
    except (tarfile.TarError, EOFError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(e, PermissionError):
            raise AccessError(f"Cannot restore: {e}") from e
        raise OperationError(f"Backup file is damaged or unreadable: {e}") from e
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o755
        os.chmod(tmp, mode)
        if target.exists():
            os.rename(target, old)
        try:
            os.rename(tmp, target)
        except OSError:
            if old.exists():
                os.rename(old, target)  # put the original back
            raise
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OperationError(f"Could not swap in the restored data: {e.strerror or e}") from e
    if old.exists():
        try:
            shutil.rmtree(old)
        except OSError as e:
            warnings.append(f"Restored, but could not remove the previous data copy at {old}: {e.strerror or e}")
    return warnings


def empty_directory(target: Path) -> None:
    """Delete everything inside `target` but keep the folder itself."""
    try:
        for child in target.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    except PermissionError as e:
        raise AccessError(
            f"Could not delete {e.filename}: permission denied (owned by another user?). "
            f"Some files may already be gone; the safety backup is intact.") from e
    except OSError as e:
        raise OperationError(f"Could not delete data: {e.strerror or e}") from e


def remove_tree(target: Path) -> None:
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        pass
    except PermissionError as e:
        raise AccessError(
            f"Could not delete {e.filename}: permission denied (owned by another user?). "
            f"The safety backup is intact.") from e
    except OSError as e:
        raise OperationError(f"Could not delete data: {e.strerror or e}") from e


def folder_usage(target: Path, cap: int = 100_000) -> tuple[int, int, bool]:
    """(file count, total bytes, truncated) for a folder, not following symlinks. Stops after `cap` entries."""
    files = size = seen = 0
    for root, dirs, names in os.walk(target, followlinks=False):
        for n in names:
            seen += 1
            if seen > cap:
                return files, size, True
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue
            files += 1
            size += st.st_size
    return files, size, False
