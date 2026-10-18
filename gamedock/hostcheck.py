"""Small checks of the machine GameDock runs on: can a host port be used, who owns the files, free space.

Used before starting a server and by the `doctor` command. Linux is the supported platform; things that
do not exist elsewhere (file owners on Windows) are skipped quietly.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path


def port_free(protocol: str, port: int, host: str = "0.0.0.0") -> bool | None:
    """Try to bind the port. False = taken by something else, True = free, None = cannot tell
    (for example a privileged port when not root: Docker itself runs as root and may still succeed)."""
    kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, kind)
    try:
        if protocol == "tcp" and sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # like the Docker proxy; ignores TIME_WAIT
        s.bind((host, port))
        return True
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            return False
        return None
    finally:
        s.close()


def foreign_owners(root: Path, cap: int = 20000) -> tuple[set[int], str | None]:
    """User ids that own files under `root` other than the current user, plus an example path.
    Looks at up to `cap` entries and never follows symlinks. Empty on platforms without file owners."""
    if not hasattr(os, "getuid"):
        return set(), None
    me = os.getuid()
    owners: set[int] = set()
    example = None
    seen = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            seen += 1
            if seen > cap:
                return owners, example
            try:
                uid = os.lstat(os.path.join(dirpath, name)).st_uid
            except OSError:
                continue
            if uid != me:
                owners.add(uid)
                example = example or os.path.join(dirpath, name)
    return owners, example


def free_space(path: Path) -> tuple[int, int]:
    """(free bytes, total bytes) of the disk holding `path` (or its nearest existing parent)."""
    import shutil
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return u.free, u.total
