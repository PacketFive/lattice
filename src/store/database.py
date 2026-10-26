# SPDX-License-Identifier: GPL-2.0
"""The dependency database (RFC 0001 sections 11, 12 and 14.8).

The SQL is plain ``sqlite3``. Rows leave this module as the Pydantic
models StorePathInfo and GCRootInfo, and enter it as Registration, so the
rest of Lattice never handles a database row.

Every connection enables foreign keys and write-ahead logging, so readers
are not blocked while a path is registered. ``references`` is an SQL
keyword and is quoted in every statement. The schema version is kept in
``PRAGMA user_version`` and MIGRATIONS brings an older database up to
date when it is opened.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from ..errors import StoreError
from ..models import Model
from . import lock
from .layout import StoreLayout

Location = Literal["local", "shallow"]

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE store_paths (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            drv_hash TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT 'local' CHECK (location IN ('local', 'shallow')),
            source TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE "references" (
            referrer INTEGER NOT NULL REFERENCES store_paths (id) ON DELETE CASCADE,
            reference INTEGER NOT NULL REFERENCES store_paths (id) ON DELETE RESTRICT,
            PRIMARY KEY (referrer, reference),
            CHECK (referrer != reference)
        ) WITHOUT ROWID""",
        'CREATE INDEX references_reference ON "references" (reference)',
        """CREATE TABLE gc_roots (
            name TEXT PRIMARY KEY,
            path INTEGER NOT NULL REFERENCES store_paths (id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX gc_roots_path ON gc_roots (path)",
    ),
)
"""The statements that create schema version ``index + 1`` from the one before."""

_ROOT_SEGMENT = re.compile(r"[A-Za-z0-9._+-]+")


class Registration(Model):
    """A store path to register, with the store paths it refers to.

    ``location`` and ``source`` are those of section 14.8: a path built
    here is ``local`` with an empty ``source``.
    """

    path: str
    drv_hash: str
    content_sha256: str
    size: int
    references: frozenset[str] = frozenset()
    location: Location = "local"
    source: str = ""


class StorePathInfo(Model):
    """What the database records about one store path."""

    path: str
    drv_hash: str
    content_sha256: str
    size: int
    registered_at: datetime
    location: Location
    source: str
    references: tuple[str, ...]


class GCRootInfo(Model):
    """A named root that collection keeps, with everything it refers to."""

    name: str
    path: str
    created_at: datetime


def check_root_name(name: str) -> str:
    """Return ``name`` if it is a valid root name, and raise StoreError if not.

    A name is one or more segments of letters, digits, ``.``, ``_``, ``+``
    and ``-``, joined by ``/``. ``.`` and ``..`` are not segments.
    """
    parts = name.split("/")
    if not all(_ROOT_SEGMENT.fullmatch(part) and part not in {".", ".."} for part in parts):
        raise StoreError(f"{name!r} is not a valid root name")
    return name


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise StoreError(f"the store database has schema {version}, newer than this Lattice")
    if version == len(MIGRATIONS):
        return
    with transaction(connection):
        # Another connection may have migrated while this one waited for the write lock.
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version >= len(MIGRATIONS):
            return
        for statements in MIGRATIONS[version:]:
            for statement in statements:
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")


def connect(layout: StoreLayout, *, create: bool = False) -> sqlite3.Connection:
    """Open the database of ``layout``, creating the store first if ``create``.

    Raises StoreError if the store does not exist and ``create`` is False.
    The connection is in autocommit mode; use transaction() to group
    statements. Close it when done.
    """
    if create:
        layout.create()
    elif not layout.exists():
        raise StoreError(f"there is no store under {layout.root}")
    with lock.database(layout):
        try:
            connection = sqlite3.connect(layout.database, timeout=60, isolation_level=None)
        except sqlite3.Error as error:
            raise StoreError(f"cannot open {layout.database}: {error}") from error
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            _migrate(connection)
        except BaseException:
            connection.close()
            raise
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run the statements of the ``with`` block in one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _ids(connection: sqlite3.Connection, paths: Iterable[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in paths:
        row = connection.execute("SELECT id FROM store_paths WHERE path = ?", (path,)).fetchone()
        if row is not None:
            found[path] = row[0]
    return found


def registered(connection: sqlite3.Connection, paths: Iterable[str]) -> frozenset[str]:
    """Return the members of ``paths`` that are registered."""
    return frozenset(_ids(connection, paths))


def register(connection: sqlite3.Connection, entries: Iterable[Registration]) -> None:
    """Register ``entries`` and their references in one transaction.

    The entries may refer to each other. Every other path they refer to
    must already be registered, and a path that is already registered is
    an error.
    """
    entries = tuple(entries)
    created_at = _now()
    with transaction(connection):
        for entry in entries:
            try:
                connection.execute(
                    "INSERT INTO store_paths (path, drv_hash, content_sha256, size,"
                    " registered_at, location, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.path,
                        entry.drv_hash,
                        entry.content_sha256,
                        entry.size,
                        created_at,
                        entry.location,
                        entry.source,
                    ),
                )
            except sqlite3.IntegrityError:
                raise StoreError(f"{entry.path} is already registered") from None
        for entry in entries:
            ids = _ids(connection, {entry.path, *entry.references})
            missing = sorted(entry.references - ids.keys())
            if missing:
                raise StoreError(f"{entry.path} refers to unregistered {', '.join(missing)}")
            connection.executemany(
                'INSERT INTO "references" (referrer, reference) VALUES (?, ?)',
                [(ids[entry.path], ids[ref]) for ref in sorted(entry.references - {entry.path})],
            )


def path_info(connection: sqlite3.Connection, path: str) -> StorePathInfo | None:
    """Return what is recorded about ``path``, or None if it is not registered."""
    row = connection.execute(
        "SELECT id, drv_hash, content_sha256, size, registered_at, location, source"
        " FROM store_paths WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    references = connection.execute(
        'SELECT s.path FROM "references" r JOIN store_paths s ON s.id = r.reference'
        " WHERE r.referrer = ? ORDER BY s.path",
        (row[0],),
    ).fetchall()
    return StorePathInfo(
        path=path,
        drv_hash=row[1],
        content_sha256=row[2],
        size=row[3],
        registered_at=datetime.fromisoformat(row[4]),
        location=row[5],
        source=row[6],
        references=tuple(ref for (ref,) in references),
    )


def _with_temp_paths(connection: sqlite3.Connection, paths: Iterable[str]) -> None:
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS start_paths (path TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM start_paths")
    connection.executemany(
        "INSERT OR IGNORE INTO start_paths (path) VALUES (?)", [(p,) for p in paths]
    )


def closure(connection: sqlite3.Connection, paths: Iterable[str]) -> frozenset[str]:
    """Return ``paths`` and every registered path they refer to, directly or not.

    Paths that are not registered are left out.
    """
    _with_temp_paths(connection, paths)
    rows = connection.execute(
        """WITH RECURSIVE reachable (id) AS (
            SELECT s.id FROM store_paths s JOIN start_paths t ON t.path = s.path
            UNION
            SELECT r.reference FROM "references" r JOIN reachable ON r.referrer = reachable.id
        )
        SELECT s.path FROM store_paths s JOIN reachable ON reachable.id = s.id"""
    ).fetchall()
    return frozenset(path for (path,) in rows)


def live_paths(connection: sqlite3.Connection, temporary: Iterable[str]) -> frozenset[str]:
    """Return the paths reachable from a root or from ``temporary`` (section 12)."""
    _with_temp_paths(connection, temporary)
    rows = connection.execute(
        """WITH RECURSIVE reachable (id) AS (
            SELECT path FROM gc_roots
            UNION
            SELECT s.id FROM store_paths s JOIN start_paths t ON t.path = s.path
            UNION
            SELECT r.reference FROM "references" r JOIN reachable ON r.referrer = reachable.id
        )
        SELECT s.path FROM store_paths s JOIN reachable ON reachable.id = s.id"""
    ).fetchall()
    return frozenset(path for (path,) in rows)


def all_paths(connection: sqlite3.Connection) -> dict[str, int]:
    """Return every registered path with its size."""
    return dict(connection.execute("SELECT path, size FROM store_paths").fetchall())


def referrers(connection: sqlite3.Connection, path: str) -> frozenset[str]:
    """Return the registered paths that refer to ``path``."""
    rows = connection.execute(
        'SELECT a.path FROM "references" r JOIN store_paths a ON a.id = r.referrer'
        " JOIN store_paths b ON b.id = r.reference WHERE b.path = ?",
        (path,),
    ).fetchall()
    return frozenset(p for (p,) in rows)


def unregister(connection: sqlite3.Connection, path: str) -> None:
    """Remove ``path`` and its outgoing references from the database.

    Raises StoreError if a root or another path still refers to it.
    """
    with transaction(connection):
        try:
            connection.execute("DELETE FROM store_paths WHERE path = ?", (path,))
        except sqlite3.IntegrityError:
            raise StoreError(f"{path} is still referred to") from None


def add_root(connection: sqlite3.Connection, name: str, path: str) -> GCRootInfo:
    """Make ``path`` the root called ``name``, replacing any root of that name."""
    check_root_name(name)
    created_at = _now()
    with transaction(connection):
        ids = _ids(connection, (path,))
        if not ids:
            raise StoreError(f"{path} is not registered")
        connection.execute(
            "INSERT INTO gc_roots (name, path, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET path = excluded.path,"
            " created_at = excluded.created_at",
            (name, ids[path], created_at),
        )
    return GCRootInfo(name=name, path=path, created_at=datetime.fromisoformat(created_at))


def remove_root(connection: sqlite3.Connection, name: str) -> GCRootInfo:
    """Remove the root called ``name`` and return it. Raises StoreError if there is none."""
    check_root_name(name)
    with transaction(connection):
        row = connection.execute(
            "SELECT s.path, g.created_at FROM gc_roots g JOIN store_paths s ON s.id = g.path"
            " WHERE g.name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise StoreError(f"there is no root called {name!r}")
        connection.execute("DELETE FROM gc_roots WHERE name = ?", (name,))
    return GCRootInfo(name=name, path=row[0], created_at=datetime.fromisoformat(row[1]))


def roots(connection: sqlite3.Connection) -> tuple[GCRootInfo, ...]:
    """Return every root, sorted by name."""
    rows = connection.execute(
        "SELECT g.name, s.path, g.created_at FROM gc_roots g"
        " JOIN store_paths s ON s.id = g.path ORDER BY g.name"
    ).fetchall()
    return tuple(
        GCRootInfo(name=name, path=path, created_at=datetime.fromisoformat(created))
        for name, path, created in rows
    )
