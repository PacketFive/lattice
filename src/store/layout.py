# SPDX-License-Identifier: GPL-2.0
"""Where the parts of the store are on disk (RFC 0001 sections 10 to 12).

A store path is always named ``/lattice/store/<hash>-<name>``, because
that name is what builds record in their outputs. StoreLayout maps that
name to the directory that holds it, which is ``<root>/store/<hash>-<name>``,
so a store under a temporary root behaves like the real one.

======================  ========================================================
Path                    Contents
======================  ========================================================
``store``               The store paths, read-only once registered
``staging``             Build working directories and per-derivation lock files,
                        on the same filesystem as ``store`` so a rename publishes
``var/db.sqlite``       The dependency database (section 11)
``var/lock``            The store lock (section 12)
``var/db.lock``         Held while a connection sets up and migrates the database
``var/profiles.lock``   Held while a profile or slot changes (section 13)
``profiles``            Profile and generation symlinks (section 13.1)
======================  ========================================================
"""

from __future__ import annotations

import os
import re

from ..derivation import STORE_DIR
from ..errors import StoreError
from ..models import Model
from ..settings import DEFAULT_STACKS

_BASENAME = re.compile(r"[0-9a-z]{32}-[A-Za-z0-9+._?=-]+")


def check_store_path(path: str) -> str:
    """Return ``path`` if it names a store path, and raise StoreError if not.

    A store path is a direct child of ``/lattice/store`` whose name is a
    hash and a derivation name. Paths inside a store path are refused.
    """
    parent, _, name = path.rpartition("/")
    if parent != STORE_DIR or not _BASENAME.fullmatch(name) or name.endswith((".", "..")):
        raise StoreError(f"{path!r} is not a store path")
    return path


class StoreLayout(Model):
    """The directories of a store whose files are under ``root``.

    ``stacks`` is the directory of slot symlinks (section 13.2). It is
    outside ``root`` because slots describe the running system and are
    created again at every boot.
    """

    root: str
    stacks: str = DEFAULT_STACKS

    @property
    def store(self) -> str:
        """The directory that holds the store paths."""
        return os.path.join(self.root, "store")

    @property
    def staging(self) -> str:
        """The directory for builds in progress and per-derivation locks."""
        return os.path.join(self.root, "staging")

    @property
    def database(self) -> str:
        """The SQLite dependency database."""
        return os.path.join(self.root, "var", "db.sqlite")

    @property
    def lock(self) -> str:
        """The file locked to keep realisation and collection apart."""
        return os.path.join(self.root, "var", "lock")

    @property
    def database_lock(self) -> str:
        """The file locked while a connection sets up the database."""
        return os.path.join(self.root, "var", "db.lock")

    @property
    def profiles_lock(self) -> str:
        """The file locked while a profile or slot changes."""
        return os.path.join(self.root, "var", "profiles.lock")

    @property
    def profiles(self) -> str:
        """The directory of profile and generation symlinks."""
        return os.path.join(self.root, "profiles")

    def active_slots(self) -> dict[str, str]:
        """Return the store path each slot symlink in ``stacks`` points at, by slot name."""
        found: dict[str, str] = {}
        try:
            names = sorted(os.listdir(self.stacks))
        except FileNotFoundError:
            return found
        for name in names:
            link = os.path.join(self.stacks, name)
            if not name.startswith(".") and os.path.islink(link):
                found[name] = os.readlink(link)
        return found

    def physical(self, path: str) -> str:
        """Return the directory that holds store path ``path``."""
        return os.path.join(self.store, check_store_path(path).rpartition("/")[2])

    def exists(self) -> bool:
        """Return True if the store has been created."""
        return os.path.exists(self.database)

    def create(self) -> None:
        """Create the directories of the store if they are missing.

        ``store`` and ``var`` can be read by every user, and ``staging`` by
        the owner only, since it holds builds that have not been checked.
        """
        os.makedirs(self.store, mode=0o755, exist_ok=True)
        os.makedirs(os.path.dirname(self.database), mode=0o755, exist_ok=True)
        os.makedirs(self.staging, mode=0o700, exist_ok=True)
