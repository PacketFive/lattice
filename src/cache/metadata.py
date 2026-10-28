# SPDX-License-Identifier: GPL-2.0
"""The files that describe a cache and its entries (RFC 0001 sections 14.2 and 14.3).

Every file read from a cache is untrusted until its signature is checked,
so it is parsed with a size limit and strict validation first. A field a
model does not know, a malformed hash or a store path that does not fit
its derivation hash is an error, never ignored.

A CacheEntry names its archive by its SHA-256 alone, at
``archive/<sha256>.tar.xz``. A cache therefore cannot point a reader at
another server or file.
"""

from __future__ import annotations

import base64
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from ..derivation import STORE_HASH_LEN
from ..errors import SubstitutionFailed
from ..models import Model
from ..store.layout import check_store_path

FORMAT_VERSION = 1
MAX_METADATA = 1 << 20
"""The largest metadata or provenance file read from a cache, in bytes."""

DEFAULT_PRIORITY = 50
_HEX = Field(pattern=r"^[0-9a-f]{64}$")


def _store_path(path: str) -> str:
    try:
        return check_store_path(path)
    except Exception as error:
        raise ValueError(str(error)) from None


def path_hash(path: str) -> str:
    """Return the hash part of store path ``path``."""
    return path.rpartition("/")[2][:STORE_HASH_LEN]


def entry_name(path: str) -> str:
    """Return the name of the metadata of ``path`` in a cache, without ``.json``.

    This is the whole ``<hash>-<name>``, since the outputs of one
    derivation share their hash.
    """
    return path.rpartition("/")[2]


class ArchiveRef(Model):
    """Where an HTTP cache keeps the archive of an entry, and what it must hash to."""

    sha256: str = _HEX
    size: int = Field(ge=0)
    compression: Literal["xz"] = "xz"
    url: str

    @model_validator(mode="after")
    def _check_url(self) -> Self:
        if self.url != f"archive/{self.sha256}.tar.xz":
            raise ValueError(f"archive url must be archive/{self.sha256}.tar.xz")
        return self


class CacheEntry(Model):
    """The metadata of one store path in a cache, ``<hash>-<name>.json``.

    ``archive`` is None in a CernVM-FS cache, which holds the path itself.
    The signatures cover fingerprint(), not the archive, so one signed
    entry is valid however the path is transported.
    """

    version: Literal[1] = FORMAT_VERSION
    store_path: str
    drv_hash: str = _HEX
    content_sha256: str = _HEX
    size: int = Field(ge=0)
    references: tuple[str, ...] = ()
    archive: ArchiveRef | None = None
    signatures: tuple[str, ...] = ()

    @field_validator("store_path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _store_path(value)

    @field_validator("references")
    @classmethod
    def _check_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            _store_path(path)
        if list(value) != sorted(set(value)):
            raise ValueError("references must be sorted and without repeats")
        return value

    @model_validator(mode="after")
    def _check_hashes(self) -> Self:
        expected = base64.b32encode(bytes.fromhex(self.drv_hash)).decode().lower()
        if path_hash(self.store_path) != expected[:STORE_HASH_LEN]:
            raise ValueError("store_path does not belong to drv_hash")
        if self.store_path in self.references:
            raise ValueError("a path does not list itself as a reference")
        return self

    def fingerprint(self) -> bytes:
        """Return the bytes the signatures cover (section 14.3)."""
        fields = (
            "lattice-entry-1",
            self.store_path,
            self.content_sha256,
            str(self.size),
            ",".join(self.references),
        )
        return ";".join(fields).encode()


class CacheInfo(Model):
    """``lattice-cache.json``: the format of a cache and its priority.

    Caches with a lower ``priority`` are asked first; caches with the same
    priority are asked in the order the settings list them.
    """

    version: Literal[1] = FORMAT_VERSION
    priority: int = Field(default=DEFAULT_PRIORITY, ge=0, le=1000)


def parse_untrusted[M: Model](model: type[M], data: bytes, where: str) -> M:
    """Validate ``data`` read from a cache as ``model``.

    Raises SubstitutionFailed naming ``where`` if it is too large or invalid.
    """
    if len(data) > MAX_METADATA:
        raise SubstitutionFailed(f"{where} is larger than {MAX_METADATA} bytes")
    try:
        return model.model_validate_json(data)
    except ValidationError as error:
        first = error.errors()[0]
        place = ".".join(str(part) for part in first["loc"])
        raise SubstitutionFailed(
            f"{where}: {place}: {first['msg']}" if place else f"{where}: {first['msg']}"
        ) from None
