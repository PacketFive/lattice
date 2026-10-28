# SPDX-License-Identifier: GPL-2.0
"""Fetching store paths from binary caches (RFC 0001 sections 14.1, 14.5 and 14.6).

A Substituter asks the configured caches, in order of priority, for the
metadata of a store path. The first cache whose entry is signed by a
trusted key, and has valid provenance if the settings require it,
supplies the path. A cache that cannot be read or whose entry is not
valid is skipped with a CacheSkipped event.

substitute fetches the paths an entry refers to first, so the store never
holds a path whose references are missing. Each path is checked twice:
the archive against the SHA-256 in the signed entry, and the unpacked
tree against the signed content hash and size. Only then is it moved to
the store and registered, with the references the signed entry lists.

A path from a CernVM-FS cache may instead be shallow: a symlink to the
directory in the repository, registered with location ``shallow`` and the
signed hash and size. Nothing is copied and nothing is hashed, as that
would fetch every file (section 14.6).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass

from ..errors import StoreError, SubstitutionFailed
from ..events import (
    CacheSkipped,
    EventHandler,
    SubstitutionFinished,
    SubstitutionStarted,
    emit,
)
from ..models import SubstitutionResult
from ..settings import Settings
from ..store import database, lock
from ..store.layout import StoreLayout
from ..store.realise import copy_tree, make_read_only, normalise, remove_tree
from ..store.verify import content_hash
from . import archive, signing
from .base import Cache, open_cache
from .metadata import CacheEntry, entry_name, parse_untrusted
from .provenance import open_envelope


@dataclass(frozen=True)
class Found:
    """A valid entry for a store path and the cache that holds it."""

    cache: Cache
    entry: CacheEntry


class Substituter:
    """Finds and fetches store paths from the caches named in ``settings``."""

    def __init__(
        self,
        layout: StoreLayout,
        settings: Settings,
        *,
        on_event: EventHandler | None = None,
        caches: list[Cache] | None = None,
    ) -> None:
        """Keep the store, settings and event handler; open caches when first needed."""
        self.layout = layout
        self.settings = settings
        self.on_event = on_event
        self._caches = caches
        self._lock = threading.Lock()
        self._found: dict[str, Found | None] = {}

    def _emit(self, event: SubstitutionStarted | SubstitutionFinished | CacheSkipped) -> None:
        with self._lock:
            emit(self.on_event, event)

    def caches(self) -> list[Cache]:
        """Return the usable caches, lowest priority value first, then in settings order."""
        with self._lock:
            if self._caches is not None:
                return self._caches
        ranked: list[tuple[int, int, Cache]] = []
        for index, url in enumerate(self.settings.substituters):
            cache = open_cache(url, self.settings)
            try:
                ranked.append((cache.info().priority, index, cache))
            except SubstitutionFailed as error:
                self._emit(CacheSkipped(path="", cache=url, reason=str(error)))
        ranked.sort(key=lambda item: item[:2])
        with self._lock:
            self._caches = [cache for _, _, cache in ranked]
            return self._caches

    def query(self, path: str) -> Found | None:
        """Return the first valid entry for ``path``, or None if no cache has one."""
        with self._lock:
            if path in self._found:
                return self._found[path]
        found = None
        for cache in self.caches():
            try:
                found = self._lookup(cache, path)
            except SubstitutionFailed as error:
                self._emit(CacheSkipped(path=path, cache=cache.url, reason=str(error)))
                continue
            if found is not None:
                break
        with self._lock:
            self._found[path] = found
        return found

    def _lookup(self, cache: Cache, path: str) -> Found | None:
        name = entry_name(path)
        where = f"{cache.url}/{name}.json"
        data = cache.read(f"{name}.json")
        if data is None:
            return None
        entry = parse_untrusted(CacheEntry, data, where)
        if entry.store_path != path:
            raise SubstitutionFailed(f"{where} describes {entry.store_path}")
        trusted = self.settings.trusted_keys
        if signing.verify(trusted, entry.fingerprint(), entry.signatures) is None:
            raise SubstitutionFailed(f"{where} has no signature from a trusted key")
        if cache.shallow_capable:
            if cache.tree(path) is None:
                raise SubstitutionFailed(f"{cache.url} has {where} but not the path")
        elif entry.archive is None:
            raise SubstitutionFailed(f"{where} names no archive")
        if self.settings.require_provenance:
            proof = f"{name}.provenance.json"
            envelope = cache.read(proof)
            if envelope is None:
                raise SubstitutionFailed(f"{cache.url}/{proof} is missing")
            open_envelope(envelope, f"{cache.url}/{proof}", trusted, entry)
        return Found(cache=cache, entry=entry)

    def substitute(
        self,
        connection: sqlite3.Connection,
        path: str,
        *,
        shallow: Collection[str] = (),
        held: Collection[str] = (),
    ) -> tuple[str, ...]:
        """Fetch and register ``path`` and every path it refers to that is missing.

        The caller holds the store lock shared. Paths in ``shallow`` become
        shallow paths if their cache allows it. ``held`` names derivation
        hashes whose lock the caller already holds. Returns the paths
        registered, references first. Raises SubstitutionFailed if a path
        cannot be fetched or does not match its signed entry.
        """
        done: list[str] = []
        self._fetch(connection, path, shallow, held, (), done)
        return tuple(done)

    def _fetch(
        self,
        connection: sqlite3.Connection,
        path: str,
        shallow: Collection[str],
        held: Collection[str],
        trail: tuple[str, ...],
        done: list[str],
    ) -> None:
        if path in trail:
            raise SubstitutionFailed(f"cache entries refer to each other in a cycle at {path}")
        if database.registered(connection, (path,)):
            return
        found = self.query(path)
        if found is None:
            raise SubstitutionFailed(f"no trusted cache has {path}")
        for reference in found.entry.references:
            self._fetch(connection, reference, shallow, held, (*trail, path), done)
        drv_hash = found.entry.drv_hash
        if drv_hash in held:
            installed = self._install(connection, found, path in shallow)
        else:
            with lock.derivation(self.layout, drv_hash):
                installed = self._install(connection, found, path in shallow)
        if installed:
            done.append(path)

    def _install(self, connection: sqlite3.Connection, found: Found, shallow: bool) -> bool:
        """Fetch, check, publish and register one path; False if it was already there."""
        entry, cache = found.entry, found.cache
        path = entry.store_path
        if database.registered(connection, (path,)):
            return False
        started = time.monotonic()
        self._emit(SubstitutionStarted(path=path, cache=cache.url))
        shallow = shallow and cache.shallow_capable
        physical = self.layout.physical(path)
        try:
            if shallow:
                tree = cache.tree(path)
                if tree is None:
                    raise SubstitutionFailed(f"{cache.url} no longer has {path}")
                remove_tree(physical)
                os.symlink(tree, physical)
            else:
                work = tempfile.mkdtemp(prefix=f"{entry.drv_hash[:32]}-", dir=self.layout.staging)
                try:
                    out = os.path.join(work, "out")
                    self._unpack(found, work, out)
                    normalise(out)
                    digest, size = content_hash(out)
                    if (digest, size) != (entry.content_sha256, entry.size):
                        raise SubstitutionFailed(
                            f"{path} from {cache.url} has content {digest} of {size} bytes,"
                            f" but its entry says {entry.content_sha256} of {entry.size}"
                        )
                    remove_tree(physical)
                    os.rename(out, physical)
                    make_read_only(physical)
                finally:
                    remove_tree(work)
        except (OSError, StoreError) as error:
            raise SubstitutionFailed(f"cannot substitute {path}: {error}") from None
        database.register(
            connection,
            (
                database.Registration(
                    path=path,
                    drv_hash=entry.drv_hash,
                    content_sha256=entry.content_sha256,
                    size=entry.size,
                    references=frozenset(entry.references),
                    location="shallow" if shallow else "local",
                    source=cache.url,
                ),
            ),
        )
        self._emit(
            SubstitutionFinished(
                path=path, cache=cache.url, shallow=shallow, seconds=time.monotonic() - started
            )
        )
        return True

    def _unpack(self, found: Found, work: str, out: str) -> None:
        entry, cache = found.entry, found.cache
        tree = cache.tree(entry.store_path) if cache.shallow_capable else None
        if tree is not None:
            copy_tree(tree, out)
            return
        if entry.archive is None:
            raise SubstitutionFailed(f"{cache.url} has no archive of {entry.store_path}")
        ref = entry.archive
        file = os.path.join(work, "archive")
        digest, size = cache.download(ref.url, file, ref.size)
        if (digest, size) != (ref.sha256, ref.size):
            raise SubstitutionFailed(
                f"{cache.url}/{ref.url} has SHA-256 {digest} of {size} bytes,"
                f" but its entry says {ref.sha256} of {ref.size}"
            )
        archive.extract(file, entry.store_path.rpartition("/")[2], out, entry.size)
        os.unlink(file)


def substitute(
    layout: StoreLayout,
    settings: Settings,
    path: str,
    *,
    shallow: bool = False,
    on_event: EventHandler | None = None,
) -> SubstitutionResult:
    """Fetch ``path`` and its missing references into the store of ``layout``.

    If ``shallow``, ``path`` itself may become a shallow path; its
    references are full copies.
    """
    started = time.monotonic()
    database.connect(layout, create=True).close()
    substituter = Substituter(layout, settings, on_event=on_event)
    with lock.shared(layout):
        connection = database.connect(layout)
        try:
            done = substituter.substitute(connection, path, shallow={path} if shallow else ())
        finally:
            connection.close()
    return SubstitutionResult(path=path, substituted=done, seconds=time.monotonic() - started)


def materialise(layout: StoreLayout, path: str) -> database.StorePathInfo:
    """Turn the shallow path ``path`` into a full copy (RFC 0001 section 14.6).

    The files are copied into staging and checked against the registered
    hash under the shared store lock. The symlink is then replaced under
    the exclusive lock, so a collection cannot run in between. A path
    that is already local is left as it is. Raises StoreError if ``path``
    is not registered or its copy does not match.
    """
    physical = layout.physical(path)
    connection = database.connect(layout)
    try:
        with lock.shared(layout):
            info = database.path_info(connection, path)
            if info is None:
                raise StoreError(f"{path} is not registered")
            if info.location == "local":
                return info
            work = tempfile.mkdtemp(prefix="materialise-", dir=layout.staging)
            out = os.path.join(work, "out")
            try:
                copy_tree(os.readlink(physical), out)
                normalise(out)
                digest, size = content_hash(out)
            except BaseException:
                remove_tree(work)
                raise
            if (digest, size) != (info.content_sha256, info.size):
                remove_tree(work)
                raise StoreError(
                    f"{path} has content {digest} of {size} bytes in its cache,"
                    f" but was registered with {info.content_sha256} of {info.size}"
                )
        try:
            with lock.exclusive(layout):
                if not os.path.isdir(out):
                    raise StoreError(f"the copy of {path} was removed from staging; try again")
                if os.path.islink(physical):
                    os.unlink(physical)
                    os.rename(out, physical)
                    make_read_only(physical)
                    database.set_location(connection, path, "local", digest, size)
        finally:
            remove_tree(work)
        info = database.path_info(connection, path)
        if info is None:
            raise StoreError(f"{path} was collected while it was being copied")
        return info
    finally:
        connection.close()
