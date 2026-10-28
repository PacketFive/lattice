# SPDX-License-Identifier: GPL-2.0
"""Caches of static files over HTTP, HTTPS or the local filesystem (RFC 0001 section 14.2).

An HTTP cache needs no server software: any web server or object store
that serves files works for reading, and one that accepts ``PUT`` works
for pushing. A ``file://`` URL names a directory and is useful for
testing, for caches on shared filesystems and for copying a cache to a
machine without a network. Redirects are followed only to HTTP and
HTTPS URLs. File names are percent-encoded in URLs, since a store path
name may hold ``?`` or ``+``, so a web server that serves a directory
cache finds the same files.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import IO

from ..errors import SubstitutionFailed
from ..store.fetch import OPENER, TIMEOUT
from .base import Cache
from .metadata import MAX_METADATA

_CHUNK = 1 << 16


def _copy(source: IO[bytes], target: IO[bytes] | None, limit: int, where: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_CHUNK):
        size += len(chunk)
        if size > limit:
            raise SubstitutionFailed(f"{where} is larger than {limit} bytes")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return digest.hexdigest(), size


class HttpCache(Cache):
    """A cache of static files at an ``https``, ``http`` or ``file`` URL."""

    def __init__(self, url: str) -> None:
        """Remember ``url``, which the settings have checked."""
        super().__init__(url)
        parts = urllib.parse.urlsplit(url)
        self._directory = parts.path if parts.scheme == "file" else None
        self._base = url.rstrip("/") + "/"

    def _where(self, name: str) -> str:
        return self._base + urllib.parse.quote(name)

    def _open(self, name: str) -> IO[bytes] | None:
        if self._directory is not None:
            try:
                return open(os.path.join(self._directory, name), "rb")  # noqa: SIM115
            except FileNotFoundError:
                return None
            except OSError as error:
                raise SubstitutionFailed(f"cannot read {self._where(name)}: {error}") from None
        try:
            return OPENER.open(self._where(name), timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return None
            raise SubstitutionFailed(f"cannot read {self._where(name)}: {error}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            reason = getattr(error, "reason", None) or error
            raise SubstitutionFailed(f"cannot read {self._where(name)}: {reason}") from None

    def read(self, name: str, limit: int = MAX_METADATA) -> bytes | None:
        """Return the file ``name``, or None if the cache does not have it."""
        source = self._open(name)
        if source is None:
            return None
        with source:
            try:
                data = source.read(limit + 1)
            except (OSError, http.client.HTTPException) as error:
                raise SubstitutionFailed(f"cannot read {self._where(name)}: {error}") from None
        if len(data) > limit:
            raise SubstitutionFailed(f"{self._where(name)} is larger than {limit} bytes")
        return data

    def download(self, name: str, destination: str, limit: int) -> tuple[str, int]:
        """Copy the file ``name`` to the new file ``destination`` and return its hash and size."""
        source = self._open(name)
        if source is None:
            raise SubstitutionFailed(f"{self._where(name)} is missing")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with source, os.fdopen(fd, "wb") as target:
            try:
                return _copy(source, target, limit, self._where(name))
            except (OSError, http.client.HTTPException) as error:
                raise SubstitutionFailed(f"cannot read {self._where(name)}: {error}") from None

    def exists(self, name: str) -> bool:
        """Return True if the cache has the file ``name``."""
        source = self._open(name)
        if source is None:
            return False
        source.close()
        return True

    def write(self, name: str, source: str) -> None:
        """Store the local file ``source`` as ``name`` in the cache.

        A directory cache gets the file by an atomic rename, so a reader
        never sees part of it. A web cache gets an HTTP ``PUT``.
        """
        if self._directory is not None:
            target = os.path.join(self._directory, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".lattice-", dir=os.path.dirname(target))
            try:
                with os.fdopen(fd, "wb") as out, open(source, "rb") as data:
                    shutil.copyfileobj(data, out)
                os.chmod(temporary, 0o644)
                os.rename(temporary, target)
            except BaseException:
                os.unlink(temporary)
                raise
            return
        with open(source, "rb") as data:
            request = urllib.request.Request(  # noqa: S310 - the scheme is checked in settings
                self._where(name),
                data=data,
                method="PUT",
                headers={"Content-Length": str(os.fstat(data.fileno()).st_size)},
            )
            try:
                with OPENER.open(request, timeout=TIMEOUT):
                    pass
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
                reason = getattr(error, "reason", None) or error
                raise SubstitutionFailed(f"cannot write {self._where(name)}: {reason}") from None
