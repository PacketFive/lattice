# SPDX-License-Identifier: GPL-2.0
"""Publishing store paths to a binary cache (RFC 0001 sections 14.4, 14.5 and 14.9).

push publishes store paths with their whole closure, references first,
and skips the paths whose metadata the cache already has. Each path is
hashed again before it is published, so a path changed since it was
registered is never signed.

For an HTTP cache each path is uploaded as its archive, then its signed
provenance, then its signed metadata, so a reader that finds the
metadata can always find the rest. For a CernVM-FS cache the paths and
their files are copied into the repository in one transaction.

Provenance states how a path was built, so only paths built on this
machine can be pushed. A path that was substituted or built elsewhere
must be pushed from the machine that built it.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import time
from collections.abc import Iterable

from ..errors import SettingsError, StoreError, SubstitutionFailed
from ..events import EventHandler, PathPushed, emit
from ..models import PushResult
from ..settings import Settings, check_cache_url
from ..store import database, lock
from ..store.layout import StoreLayout
from ..store.realise import remove_tree
from ..store.verify import content_hash
from . import archive, provenance
from .base import INFO_FILE, open_cache
from .cvmfs import CvmfsCache
from .http import HttpCache
from .metadata import ArchiveRef, CacheEntry, CacheInfo, entry_name
from .signing import SecretKey


def _ordered(connection: sqlite3.Connection, paths: Iterable[str]) -> list[str]:
    """Return ``paths`` and their closure, each path after the paths it refers to."""
    order: list[str] = []
    seen: set[str] = set()
    for start in sorted(paths):
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            path, expanded = stack.pop()
            if expanded:
                order.append(path)
                continue
            if path in seen:
                continue
            seen.add(path)
            info = database.path_info(connection, path)
            if info is None:
                raise StoreError(f"{path} is not registered")
            stack.append((path, True))
            stack.extend((ref, False) for ref in reversed(info.references) if ref not in seen)
    return order


class _Publisher:
    def __init__(self, layout: StoreLayout, settings: Settings, key: SecretKey) -> None:
        self.layout = layout
        self.settings = settings
        self.key = key
        self.builder_id = settings.cache.builder_id or socket.gethostname()

    def entry(self, connection: sqlite3.Connection, path: str) -> tuple[CacheEntry, str, bytes]:
        """Return the unsigned entry of ``path``, its tree and its signed provenance."""
        info = database.path_info(connection, path)
        record = database.build_record(connection, path)
        if info is None:
            raise StoreError(f"{path} is not registered")
        if record is None:
            raise StoreError(
                f"{path} was not built on this machine, so it has no provenance;"
                " push it from the machine that built it"
            )
        tree = os.path.realpath(self.layout.physical(path))
        digest, size = content_hash(tree)
        if (digest, size) != (info.content_sha256, info.size):
            raise StoreError(f"{path} has changed since it was registered; run lattice verify")
        entry = CacheEntry(
            store_path=path,
            drv_hash=info.drv_hash,
            content_sha256=digest,
            size=size,
            references=info.references,
        )
        statement = provenance.statement(
            entry,
            record.inputs,
            self.builder_id,
            record.backend,
            record.started_at,
            record.finished_at,
        )
        envelope = provenance.sign(statement, self.key).model_dump_json().encode()
        return entry, tree, envelope

    def signed(self, entry: CacheEntry, ref: ArchiveRef | None = None) -> bytes:
        entry = entry.model_copy(update={"archive": ref})
        entry = entry.model_copy(update={"signatures": (self.key.sign(entry.fingerprint()),)})
        return entry.model_dump_json().encode()


def _write_bytes(cache: HttpCache, name: str, data: bytes, work: str) -> None:
    file = os.path.join(work, name.replace("/", "_"))
    with open(file, "wb") as out:
        out.write(data)
    cache.write(name, file)


def push(
    layout: StoreLayout,
    settings: Settings,
    paths: Iterable[str],
    url: str,
    *,
    on_event: EventHandler | None = None,
) -> PushResult:
    """Publish ``paths`` and their closures to the cache at ``url``.

    Raises SettingsError if ``cache.secret_key_file`` is not set or
    cannot be read, StoreError if a path is not registered, was not built
    here or has changed, and SubstitutionFailed if the cache cannot be
    written.
    """
    started = time.monotonic()
    try:
        check_cache_url(url)
    except ValueError as error:
        raise SettingsError(str(error)) from None
    key_file = settings.cache.secret_key_file
    if key_file is None:
        raise SettingsError("cache.secret_key_file must name the key that signs pushed paths")
    publisher = _Publisher(layout, settings, SecretKey.load(key_file))
    cache = open_cache(url, settings)
    pushed: list[str] = []
    skipped: list[str] = []
    with lock.shared(layout):
        connection = database.connect(layout)
        try:
            order = _ordered(connection, paths)
            todo: list[str] = []
            for path in order:
                (skipped if cache.exists(f"{entry_name(path)}.json") else todo).append(path)
            entries = [(path, *publisher.entry(connection, path)) for path in todo]
        finally:
            connection.close()
        if isinstance(cache, CvmfsCache):
            files = [] if cache.exists(INFO_FILE) else [(INFO_FILE, CacheInfo())]
            items = []
            for index, (path, entry, tree, envelope) in enumerate(entries):
                name = entry_name(path)
                extra = {
                    f"{name}.provenance.json": envelope,
                    f"{name}.json": publisher.signed(entry),
                }
                if index == 0:
                    extra.update({file: info.model_dump_json().encode() for file, info in files})
                items.append((tree, path, extra))
            try:
                cache.publish(items)
            except OSError as error:
                raise SubstitutionFailed(f"cannot publish to {url}: {error}") from None
            for path, *_ in entries:
                pushed.append(path)
                emit(on_event, PathPushed(path=path, cache=url))
        else:
            assert isinstance(cache, HttpCache)  # noqa: S101 - open_cache returns one of the two
            work = tempfile.mkdtemp(prefix="push-", dir=layout.staging)
            try:
                for path, entry, tree, envelope in entries:
                    name = entry_name(path)
                    file = os.path.join(work, f"{name}.tar.xz")
                    made = archive.create(tree, path.rpartition("/")[2], file)
                    ref = ArchiveRef(
                        sha256=made.sha256, size=made.size, url=f"archive/{made.sha256}.tar.xz"
                    )
                    cache.write(ref.url, file)
                    os.unlink(file)
                    _write_bytes(cache, f"{name}.provenance.json", envelope, work)
                    _write_bytes(cache, f"{name}.json", publisher.signed(entry, ref), work)
                    pushed.append(path)
                    emit(on_event, PathPushed(path=path, cache=url))
                if not cache.exists(INFO_FILE):
                    _write_bytes(cache, INFO_FILE, CacheInfo().model_dump_json().encode(), work)
            except OSError as error:
                raise SubstitutionFailed(f"cannot push to {url}: {error}") from None
            finally:
                remove_tree(work)
    return PushResult(
        cache=url, pushed=tuple(pushed), skipped=tuple(skipped), seconds=time.monotonic() - started
    )
