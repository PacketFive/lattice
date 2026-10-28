# SPDX-License-Identifier: GPL-2.0
"""What every kind of cache provides, and choosing one from its URL.

A cache is read through read, which returns a small file such as
``<hash>-<name>.json``, and either download, which copies an archive,
or tree, which names the directory that already holds a store path.
Every name passed in is made by Lattice from a validated store path or
hash, never taken from a cache.
"""

from __future__ import annotations

import urllib.parse
from abc import ABC, abstractmethod

from ..errors import SubstitutionFailed
from ..settings import Settings
from .metadata import MAX_METADATA, CacheInfo, parse_untrusted

INFO_FILE = "lattice-cache.json"


class Cache(ABC):
    """A binary cache named by ``url`` (RFC 0001 section 14)."""

    url: str
    shallow_capable = False
    """True if store paths can be used from the cache in place (section 14.5)."""

    def __init__(self, url: str) -> None:
        """Remember the URL the settings name the cache by."""
        self.url = url
        self._info: CacheInfo | None = None

    @abstractmethod
    def read(self, name: str, limit: int = MAX_METADATA) -> bytes | None:
        """Return the file ``name``, or None if the cache does not have it.

        Raises SubstitutionFailed if the cache cannot be read or the file
        is larger than ``limit`` bytes.
        """

    @abstractmethod
    def exists(self, name: str) -> bool:
        """Return True if the cache has the file ``name``."""

    def download(self, name: str, destination: str, limit: int) -> tuple[str, int]:
        """Copy the file ``name`` to the new file ``destination``.

        Returns its SHA-256 and size. Raises SubstitutionFailed if it is
        missing or larger than ``limit`` bytes.
        """
        raise SubstitutionFailed(f"{self.url} holds no archives")

    def tree(self, path: str) -> str | None:
        """Return the directory that holds store path ``path``, or None."""
        return None

    def info(self) -> CacheInfo:
        """Return ``lattice-cache.json``, or the defaults if there is none."""
        if self._info is None:
            data = self.read(INFO_FILE)
            where = f"{self.url}/{INFO_FILE}"
            self._info = CacheInfo() if data is None else parse_untrusted(CacheInfo, data, where)
        return self._info


def open_cache(url: str, settings: Settings) -> Cache:
    """Return the cache that ``url`` names, as the settings check it."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme == "cvmfs":
        from .cvmfs import CvmfsCache

        return CvmfsCache(url, settings.cache.cvmfs_root, settings.cache.cvmfs_server)
    from .http import HttpCache

    return HttpCache(url)
