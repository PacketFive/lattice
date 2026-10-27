# SPDX-License-Identifier: GPL-2.0
"""Finding the store paths and device files running processes use (RFC 0001 sections 12, 13).

Collection keeps every store path a running process has open, maps into
memory, runs or works in, even if no root refers to it. scan reads this
from ``/proc``. Processes of other users that this process may not
inspect are skipped, so a collection run by an unprivileged user can
only see its own processes; the daemon runs as root and sees them all.
"""

from __future__ import annotations

import fnmatch
import os

from .derivation import STORE_DIR
from .models import Model


class ProcessUse(Model):
    """Process ``pid`` uses the store path ``path``."""

    pid: int
    path: str


def _store_path(target: str, prefixes: tuple[str, ...]) -> str | None:
    """Return the store path ``target`` lies in, under one of ``prefixes``, or None."""
    for prefix in prefixes:
        if target.startswith(prefix + "/"):
            name = target[len(prefix) + 1 :].split("/", 1)[0]
            if name:
                return f"{STORE_DIR}/{name}"
    return None


def _targets(proc: str) -> list[str]:
    """Return the files process directory ``proc`` refers to. Unreadable parts are skipped."""
    found: list[str] = []
    links = [os.path.join(proc, name) for name in ("exe", "cwd", "root")]
    try:
        links.extend(os.path.join(proc, "fd", fd) for fd in os.listdir(os.path.join(proc, "fd")))
    except OSError:
        pass
    for link in links:
        try:
            found.append(os.readlink(link))
        except OSError:
            continue
    try:
        with open(os.path.join(proc, "maps"), encoding="utf-8", errors="replace") as maps:
            for line in maps:
                fields = line.split(maxsplit=5)
                if len(fields) == 6:
                    found.append(fields[5].rstrip("\n"))
    except OSError:
        pass
    return found


def scan(host_store: str, proc_root: str = "/proc") -> tuple[ProcessUse, ...]:
    """Return each store path a process uses, sorted by pid and path.

    ``host_store`` is the directory that holds the store on this machine;
    paths under it and under ``/lattice/store`` are both counted.
    """
    prefixes = tuple(dict.fromkeys((os.path.normpath(host_store), STORE_DIR)))
    uses: set[tuple[int, str]] = set()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return ()
    for entry in entries:
        if not entry.isdigit():
            continue
        for target in _targets(os.path.join(proc_root, entry)):
            path = _store_path(target, prefixes)
            if path is not None:
                uses.add((int(entry), path))
    return tuple(ProcessUse(pid=pid, path=path) for pid, path in sorted(uses))


def open_devices(patterns: tuple[str, ...], proc_root: str = "/proc") -> tuple[ProcessUse, ...]:
    """Return each open file matching one of the glob ``patterns``, sorted by pid and path.

    Used to find processes that hold a stack's device files open (section
    13.3). ``path`` is the device file, not a store path.
    """
    if not patterns:
        return ()
    uses: set[tuple[int, str]] = set()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return ()
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = os.path.join(proc_root, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns):
                uses.add((int(entry), target))
    return tuple(ProcessUse(pid=pid, path=path) for pid, path in sorted(uses))
