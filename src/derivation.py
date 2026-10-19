# SPDX-License-Identifier: GPL-2.0
"""Derivations and their hashes (RFC 0001 section 8).

A Derivation is what a PyBonsai file produces through the Lattice standard
library, and the only output of evaluation that later phases consume.
Derivation is registered with pybonsai as a host type (see stdlib.py).
Its hash is the SHA-256 digest of a canonical JSON serialisation, so two
derivations with the same fields have the same hash on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from pybonsai import FrozenMap

STORE_DIR = "/lattice/store"
STORE_HASH_LEN = 32

# A builder that Lattice provides itself instead of a program in the
# sandbox. fetch_url uses it, with ``url`` and ``sha256`` in ``env``.
BUILTIN_FETCH_URL = "builtin:fetch_url"


@dataclass(frozen=True)
class Derivation:
    """An immutable description of one build.

    ``env`` maps strings to strings or other Derivations. Call
    validate_fields before constructing one from values a file supplied.
    """

    name: str
    builder: str
    args: tuple[str, ...]
    env: FrozenMap
    outputs: tuple[str, ...]

    @property
    def hash(self) -> str:
        """The full derivation hash as lowercase hex. See derivation_hash."""
        raise NotImplementedError

    @property
    def store_path(self) -> str:
        """The store path ``<STORE_DIR>/<store hash>-<name>``. See store_path."""
        raise NotImplementedError

    def __str__(self) -> str:
        """Return store_path, so ``f"{drv}/bin"`` in a file names a path in the store."""
        raise NotImplementedError


def validate_fields(
    name: object,
    builder: object,
    args: object,
    env: object,
    outputs: object,
) -> None:
    """Check field types and values before a Derivation is built.

    ``name`` is a non-empty string with no ``/`` and no leading ``.``.
    ``builder`` is a non-empty string. ``args`` and ``outputs`` are tuples
    of strings, and ``outputs`` is non-empty with no duplicates. ``env`` is
    a FrozenMap from strings to strings or Derivations. Raises TypeError or
    ValueError with a message naming the field.
    """
    raise NotImplementedError


def canonical_form(drv: Derivation) -> dict[str, object]:
    """Return the plain dict that is serialised for hashing.

    Every Derivation inside ``env`` is replaced by its own ``hash``, so the
    hash of a derivation covers all of its dependencies transitively.
    """
    raise NotImplementedError


def canonical_json(drv: Derivation) -> bytes:
    """Serialise canonical_form as JSON: keys sorted, no whitespace, UTF-8.

    Equivalent to ``json.dumps(form, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False).encode("utf-8")``.
    """
    raise NotImplementedError


def derivation_hash(drv: Derivation) -> str:
    """Return the SHA-256 of canonical_json(drv) as 64 lowercase hex characters."""
    raise NotImplementedError


def store_hash(drv: Derivation) -> str:
    """Return the hash used in store paths.

    The SHA-256 digest encoded with the RFC 4648 base32 alphabet, lowercased,
    with padding removed and truncated to STORE_HASH_LEN characters.
    """
    raise NotImplementedError


def store_path(drv: Derivation, store_dir: str = STORE_DIR) -> str:
    """Return ``f"{store_dir}/{store_hash(drv)}-{drv.name}"``."""
    raise NotImplementedError
