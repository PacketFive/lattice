# SPDX-License-Identifier: GPL-2.0
"""Closures and registration files (RFC 0005 section 8.1).

A disk image holds store paths that its first boot must register before
Lattice can use them. ``registration.json`` lists each such path with
its references, content hash and size. ``closure(drv)`` writes it, with
the sorted list ``paths``, at build time; ``lattice export-registration``
writes it for paths already in a store; and ``lattice activate-boot``
imports it into an empty store database after hashing every path again.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from typing import Literal

from ..derivation import BUILTIN_CLOSURE, Derivation
from ..errors import BuildFailed, StoreError
from ..models import Model
from . import database
from .database import Registration
from .layout import StoreLayout, check_store_path
from .verify import content_hash

REGISTRATION_FILE = "registration.json"
PATHS_FILE = "paths"


class RegisteredPath(Model):
    """One store path of a registration file."""

    path: str
    drv_hash: str
    content_sha256: str
    size: int
    references: tuple[str, ...]


class RegistrationFile(Model):
    """The content of ``registration.json``: paths sorted by name."""

    version: Literal[1] = 1
    paths: tuple[RegisteredPath, ...]


def registration_path(layout: StoreLayout) -> str:
    """Return where a disk image places ``registration.json`` for its first boot."""
    return os.path.join(layout.root, "var", REGISTRATION_FILE)


def export(connection: sqlite3.Connection, paths: Iterable[str]) -> RegistrationFile:
    """Return the registration of the closure of ``paths``.

    Raises StoreError if a path is not registered or is shallow, since a
    path whose contents are not here cannot be copied into an image.
    """
    requested = tuple(check_store_path(path) for path in paths)
    missing = sorted(set(requested) - database.registered(connection, requested))
    if missing:
        raise StoreError(f"{', '.join(missing)} is not registered")
    entries = []
    for path in sorted(database.closure(connection, requested)):
        info = database.path_info(connection, path)
        assert info is not None  # noqa: S101
        if info.location != "local":
            raise StoreError(f"{path} is shallow; materialise it first")
        entries.append(
            RegisteredPath(
                path=path,
                drv_hash=info.drv_hash,
                content_sha256=info.content_sha256,
                size=info.size,
                references=info.references,
            )
        )
    return RegistrationFile(paths=tuple(entries))


def encode(registration: RegistrationFile) -> bytes:
    """Return ``registration`` as JSON with sorted keys, the same bytes on every machine."""
    value = registration.model_dump(mode="json")
    return (json.dumps(value, sort_keys=True, indent=1) + "\n").encode()


def build_closure(layout: StoreLayout, drv: Derivation, destination: str) -> dict[str, str]:
    """Write ``paths`` and ``registration.json`` for ``closure(of)`` at ``destination``.

    ``drv.env["of"]`` is the derivation whose outputs and their closure
    are listed. Returns ``{"out": destination}``.
    """
    of = drv.env.get("of")
    if drv.builder != BUILTIN_CLOSURE or not isinstance(of, Derivation) or set(drv.env) != {"of"}:
        raise BuildFailed(f"{drv.name}: closure needs exactly one derivation, of, in env")
    if drv.outputs != ("out",):
        raise BuildFailed(f"{drv.name}: closure has the single output out")
    connection = database.connect(layout)
    try:
        registration = export(connection, of.output_paths.values())
    except StoreError as error:
        raise BuildFailed(f"{drv.name}: {error}") from None
    finally:
        connection.close()
    os.mkdir(destination, 0o755)
    with open(os.path.join(destination, PATHS_FILE), "w", encoding="utf-8") as file:
        file.writelines(f"{entry.path}\n" for entry in registration.paths)
    with open(os.path.join(destination, REGISTRATION_FILE), "wb") as file:
        file.write(encode(registration))
    return {"out": destination}


def import_registration(
    layout: StoreLayout, connection: sqlite3.Connection, data: bytes
) -> tuple[str, ...]:
    """Register the paths of the registration file ``data`` in an empty database.

    Every path must be present and hash to the recorded content hash and
    size, so a changed image is refused before anything is registered.
    Returns the registered paths. Raises StoreError if the database
    already holds paths, the file is not valid, or a path differs.
    """
    try:
        registration = RegistrationFile.model_validate_json(data)
    except ValueError as error:
        raise StoreError(f"{REGISTRATION_FILE} is not valid: {error}") from None
    if database.all_paths(connection):
        raise StoreError(f"the store database is not empty; {REGISTRATION_FILE} is not imported")
    for entry in registration.paths:
        physical = layout.physical(entry.path)
        if not os.path.lexists(physical):
            raise StoreError(f"{entry.path} is listed in {REGISTRATION_FILE} but missing")
        if content_hash(physical) != (entry.content_sha256, entry.size):
            raise StoreError(f"{entry.path} does not match its content hash")
    database.register(
        connection,
        (
            Registration(
                path=entry.path,
                drv_hash=entry.drv_hash,
                content_sha256=entry.content_sha256,
                size=entry.size,
                references=frozenset(entry.references),
            )
            for entry in registration.paths
        ),
    )
    return tuple(entry.path for entry in registration.paths)
