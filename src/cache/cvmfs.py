# SPDX-License-Identifier: GPL-2.0
"""CernVM-FS repositories as caches (RFC 0001 sections 14.5 and 14.6).

A cache URL ``cvmfs://<repo>`` names the repository mounted at
``<cvmfs_root>/<repo>``. It holds each store path unpacked at
``store/<hash>-<name>`` next to the same ``<hash>-<name>.json`` an HTTP
cache holds, with no archive. CernVM-FS checks every file against its signed
catalog, and Lattice checks the signed metadata, so a path can be used in
place through a symlink: a shallow path.

Publishing runs ``cvmfs_server`` on the machine that owns the repository:
``transaction``, then the copies, then ``publish``, or ``abort`` if a copy
fails, so readers see all of a push or none of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - runs cvmfs_server with fixed arguments
import urllib.parse
from collections.abc import Callable, Iterable

from ..errors import SubstitutionFailed
from .base import Cache
from .metadata import MAX_METADATA

Runner = Callable[[list[str]], None]


def run(command: list[str]) -> None:
    """Run ``command``, raising SubstitutionFailed if it fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as error:
        raise SubstitutionFailed(f"cannot run {command[0]}: {error}") from None
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise SubstitutionFailed(f"{' '.join(command)} failed: {output}")


class CvmfsCache(Cache):
    """The repository named by ``cvmfs://<repo>``, mounted under ``cvmfs_root``."""

    shallow_capable = True

    def __init__(
        self, url: str, cvmfs_root: str, server: str = "cvmfs_server", runner: Runner = run
    ) -> None:
        """Remember the repository and how to publish to it."""
        super().__init__(url)
        self.repo = urllib.parse.urlsplit(url).netloc
        self.root = os.path.join(cvmfs_root, self.repo)
        self.server = server
        self.runner = runner

    def read(self, name: str, limit: int = MAX_METADATA) -> bytes | None:
        """Return the file ``name`` from the repository, or None if it has none."""
        try:
            with open(os.path.join(self.root, name), "rb") as file:
                data = file.read(limit + 1)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SubstitutionFailed(f"cannot read {self.url}/{name}: {error}") from None
        if len(data) > limit:
            raise SubstitutionFailed(f"{self.url}/{name} is larger than {limit} bytes")
        return data

    def tree(self, path: str) -> str | None:
        """Return the directory in the repository that holds store path ``path``."""
        tree = os.path.join(self.root, "store", path.rpartition("/")[2])
        return tree if os.path.isdir(tree) and not os.path.islink(tree) else None

    def exists(self, name: str) -> bool:
        """Return True if the repository has the file ``name``."""
        return os.path.lexists(os.path.join(self.root, name))

    def publish(self, items: Iterable[tuple[str, str, dict[str, bytes]]]) -> None:
        """Copy store paths and their files into the repository in one transaction.

        Each item is the physical directory of a store path, its store
        path and the files to write beside it, such as ``<hash>-<name>.json``.
        Items are copied in the order given, which must put every path
        after the paths it refers to.
        """
        self.runner([self.server, "transaction", self.repo])
        try:
            os.makedirs(os.path.join(self.root, "store"), exist_ok=True)
            for source, path, files in items:
                target = os.path.join(self.root, "store", path.rpartition("/")[2])
                if not os.path.lexists(target):
                    shutil.copytree(source, target, symlinks=True)
                for name, data in files.items():
                    with open(os.path.join(self.root, name), "wb") as file:
                        file.write(data)
        except BaseException:
            self.runner([self.server, "abort", "-f", self.repo])
            raise
        self.runner([self.server, "publish", self.repo])
