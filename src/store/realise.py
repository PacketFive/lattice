# SPDX-License-Identifier: GPL-2.0
"""Publishing build outputs in the store (RFC 0001 sections 10.2 and 10.3).

realise takes the outputs of one derivation from a working directory in
staging, which is on the same filesystem as the store, and for each one:

1. sets the modification time of every entry to one second after the
   epoch, so times recorded by later tools do not depend on the build;
2. computes its content hash and finds its references in one pass;
3. moves it to its store path with a single rename;
4. removes write permission from everything in it.

The outputs are then registered with their references in one database
transaction. A crash before that leaves an unregistered entry in the
store, which the next build of the same derivation or the next
collection removes.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from collections.abc import Iterable, Mapping

from ..derivation import Derivation
from ..errors import BuildFailed, StoreError
from . import database
from .database import Registration
from .layout import StoreLayout
from .references import ReferenceScanner
from .verify import content_hash

EPOCH_NS = 1_000_000_000
"""The modification time of every entry in the store: one second after the epoch."""


def _walk(path: str) -> list[str]:
    """Return ``path`` and every entry below it, parents before children."""
    entries = [path]
    index = 0
    while index < len(entries):
        current = entries[index]
        index += 1
        if stat.S_ISDIR(os.lstat(current).st_mode):
            entries.extend(os.path.join(current, name) for name in sorted(os.listdir(current)))
    return entries


def normalise(path: str) -> None:
    """Set the modification and access time of every entry under ``path``."""
    for entry in reversed(_walk(path)):
        os.utime(entry, ns=(EPOCH_NS, EPOCH_NS), follow_symlinks=False)


def make_read_only(path: str) -> None:
    """Remove write permission from every file and directory under ``path``."""
    for entry in reversed(_walk(path)):
        mode = os.lstat(entry).st_mode
        if not stat.S_ISLNK(mode):
            os.chmod(entry, stat.S_IMODE(mode) & ~0o222)


def copy_tree(source: str, destination: str) -> None:
    """Copy ``source`` to ``destination`` and give the owner write permission on its directories.

    Store trees are read-only, and a directory must be writable before
    it can be renamed into the store.
    """
    shutil.copytree(source, destination, symlinks=True)
    for entry in _walk(destination):
        mode = os.lstat(entry).st_mode
        if stat.S_ISDIR(mode):
            os.chmod(entry, stat.S_IMODE(mode) | 0o700)


def remove_tree(path: str) -> None:
    """Delete ``path`` and everything under it, restoring write permission first."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        os.unlink(path)
        return
    for entry in _walk(path):
        entry_mode = os.lstat(entry).st_mode
        if stat.S_ISDIR(entry_mode):
            os.chmod(entry, stat.S_IMODE(entry_mode) | 0o700)
    shutil.rmtree(path)


def _check_cycles(outputs: Mapping[str, Registration]) -> None:
    """Raise BuildFailed if the outputs of one derivation refer to each other in a cycle."""
    paths = {entry.path for entry in outputs.values()}
    edges = {entry.path: entry.references & paths for entry in outputs.values()}
    state: dict[str, int] = {}

    def visit(path: str, trail: tuple[str, ...]) -> None:
        state[path] = 1
        for target in sorted(edges[path]):
            if state.get(target) == 1:
                cycle = " -> ".join((*trail[trail.index(target) :], target))
                raise BuildFailed(f"outputs refer to each other in a cycle: {cycle}")
            if target not in state:
                visit(target, (*trail, target))
        state[path] = 2

    for path in sorted(paths):
        if path not in state:
            visit(path, (path,))


def scan(
    drv: Derivation, built: Mapping[str, str], inputs: Iterable[str]
) -> dict[str, Registration]:
    """Normalise each built output and describe it for registration.

    ``built`` maps each output name to the directory or file that holds it,
    and ``inputs`` names the store paths the build could see. Raises
    BuildFailed if an output is missing or the outputs refer to each other
    in a cycle.
    """
    outputs = drv.output_paths
    candidates = {*inputs, *outputs.values()}
    entries: dict[str, Registration] = {}
    for name, path in outputs.items():
        source = built.get(name)
        if source is None or not os.path.lexists(source):
            raise BuildFailed(f"{drv.name}: did not create output {name}")
        normalise(source)
        scanner = ReferenceScanner(candidates)
        digest, size = content_hash(source, scanner.feed)
        entries[name] = Registration(
            path=path,
            drv_hash=drv.hash,
            content_sha256=digest,
            size=size,
            references=scanner.finish() - {path},
        )
    try:
        _check_cycles(entries)
    except BuildFailed as error:
        raise BuildFailed(f"{drv.name}: {error}") from None
    return entries


def realise(
    layout: StoreLayout,
    connection: sqlite3.Connection,
    drv: Derivation,
    built: Mapping[str, str],
    inputs: Iterable[str],
) -> tuple[Registration, ...]:
    """Publish and register the outputs of ``drv`` (section 10.2).

    The caller holds the store lock shared and the lock of ``drv``, and
    ``built`` is on the same filesystem as the store. Raises BuildFailed
    as scan does, and StoreError if an output is already registered.
    """
    entries = scan(drv, built, inputs)
    already = database.registered(connection, (entry.path for entry in entries.values()))
    if already:
        raise StoreError(f"{', '.join(sorted(already))} is already registered")
    for name, entry in entries.items():
        destination = layout.physical(entry.path)
        remove_tree(destination)
        os.rename(built[name], destination)
        make_read_only(destination)
    database.register(connection, entries.values())
    return tuple(entries.values())
