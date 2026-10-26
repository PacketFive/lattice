# SPDX-License-Identifier: GPL-2.0
"""Garbage collection (RFC 0001 section 12).

collect holds the store lock exclusively, so no build runs meanwhile,
and then:

1. marks every registered path reachable from a root or used by a
   running process (procscan.py);
2. deletes the other registered paths, each before the paths it refers
   to, and unregisters each one after its files are gone;
3. deletes entries in the store that are not registered, which a build
   interrupted between rename and registration leaves behind;
4. empties staging.

A crash part way leaves a path either registered with all its files or
unregistered, which the next collection removes.
"""

from __future__ import annotations

import os
import time

from .. import procscan
from ..derivation import STORE_DIR
from ..events import CollectionFinished, EventHandler, emit
from ..models import GCResult
from . import database, lock
from .layout import StoreLayout
from .realise import remove_tree


def _disk_usage(path: str) -> int:
    """Return the bytes of the files under ``path``, not following links."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                continue
    return total + os.lstat(path).st_size


def _referrers_first(dead: set[str], edges: dict[str, frozenset[str]]) -> list[str]:
    """Order ``dead`` so each path comes before every path it refers to."""
    order: list[str] = []
    remaining = set(dead)
    while remaining:
        referred = {ref for path in remaining for ref in edges[path] if ref in remaining}
        layer = sorted(remaining - referred) or sorted(remaining)
        order.extend(layer)
        remaining.difference_update(layer)
    return order


def collect(layout: StoreLayout, on_event: EventHandler | None = None) -> GCResult:
    """Remove every store path no root or process keeps, and return what was removed."""
    started = time.monotonic()
    removed: list[str] = []
    freed = 0
    connection = database.connect(layout, create=True)
    try:
        with lock.exclusive(layout):
            in_use = {use.path for use in procscan.scan(layout.store)}
            live = database.live_paths(connection, in_use)
            dead = set(database.all_paths(connection)) - live
            edges = {
                path: frozenset(info.references)
                for path in dead
                if (info := database.path_info(connection, path)) is not None
            }
            for path in _referrers_first(dead, edges):
                physical = layout.physical(path)
                if os.path.lexists(physical):
                    freed += _disk_usage(physical)
                    remove_tree(physical)
                database.unregister(connection, path)
                removed.append(path)
            known = set(database.all_paths(connection))
            for name in sorted(os.listdir(layout.store)):
                path = f"{STORE_DIR}/{name}"
                if path not in known:
                    physical = os.path.join(layout.store, name)
                    freed += _disk_usage(physical)
                    remove_tree(physical)
                    removed.append(path)
            for name in os.listdir(layout.staging):
                remove_tree(os.path.join(layout.staging, name))
    finally:
        connection.close()
    result = GCResult(
        removed=tuple(sorted(removed)), freed_bytes=freed, seconds=time.monotonic() - started
    )
    emit(on_event, CollectionFinished(removed=len(removed), freed_bytes=freed))
    return result
