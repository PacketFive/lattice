# SPDX-License-Identifier: GPL-2.0
"""Locks that keep realisation and collection apart (RFC 0001 section 12).

A build holds the store lock shared for as long as it runs, so its inputs
and outputs cannot be collected before they are registered, and
collection holds it exclusively. Each derivation also has its own lock
file in staging, so two requests for the same derivation build it once:
the second waits and then finds the outputs registered.

The locks are ``flock`` locks. The kernel releases them when the process
ends, so a crash never leaves the store locked.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from .layout import StoreLayout


@contextmanager
def _flock(path: str, operation: int) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def shared(layout: StoreLayout) -> AbstractContextManager[None]:
    """Hold the store lock shared, as realisation does."""
    return _flock(layout.lock, fcntl.LOCK_SH)


def exclusive(layout: StoreLayout) -> AbstractContextManager[None]:
    """Hold the store lock exclusively, as collection does."""
    return _flock(layout.lock, fcntl.LOCK_EX)


def database(layout: StoreLayout) -> AbstractContextManager[None]:
    """Hold the lock that serialises opening the database.

    SQLite can report the database as locked, without waiting, when two
    connections change the journal mode or create the schema at once.
    """
    return _flock(layout.database_lock, fcntl.LOCK_EX)


def derivation(layout: StoreLayout, drv_hash: str) -> AbstractContextManager[None]:
    """Hold the lock of the derivation whose store hash is ``drv_hash``."""
    return _flock(os.path.join(layout.staging, f"{drv_hash}.lock"), fcntl.LOCK_EX)
