# SPDX-License-Identifier: GPL-2.0
r"""Content hashes of store paths and their verification (RFC 0001 section 11).

The ``content_sha256`` and ``size`` of a store path are the SHA-256 digest
and length of its serialisation in the ``lattice-tree-1`` format below. The
format covers what a program using the path can observe and nothing else:
the kind of each entry, the names in each directory, the contents of each
file, whether a file is executable, and the target of each symbolic link.
Owners, times and the remaining permission bits are not covered, so the
same tree gives the same hash on any machine and after any copy.

All integers are 8-byte big-endian unsigned numbers::

    tree      = b"lattice-tree-1\0" entry
    entry     = b"f" (b"x" | b"-") size contents   regular file
              | b"l" size target                    symbolic link
              | b"d" count (size name entry)*       directory, names sorted

Names are sorted by their bytes. Any other kind of file is an error.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator

from ..errors import StoreError
from ..models import Model

FORMAT = b"lattice-tree-1\0"
_CHUNK = 1 << 16


def _number(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _entry(path: bytes) -> Iterator[bytes]:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        yield b"l" + _number(len(target)) + target
    elif stat.S_ISREG(info.st_mode):
        executable = b"x" if info.st_mode & stat.S_IXUSR else b"-"
        yield b"f" + executable + _number(info.st_size)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            remaining = info.st_size
            while remaining > 0 and (chunk := os.read(fd, min(_CHUNK, remaining))):
                remaining -= len(chunk)
                yield chunk
            if remaining or os.read(fd, 1):
                raise StoreError(f"{os.fsdecode(path)} changed size while it was read")
        finally:
            os.close(fd)
    elif stat.S_ISDIR(info.st_mode):
        names = sorted(os.listdir(path))
        yield b"d" + _number(len(names))
        for name in names:
            yield _number(len(name)) + name
            yield from _entry(os.path.join(path, name))
    else:
        raise StoreError(f"{os.fsdecode(path)} is not a regular file, directory or symbolic link")


def serialise(path: str) -> Iterator[bytes]:
    """Yield the ``lattice-tree-1`` serialisation of the tree at ``path``."""
    yield FORMAT
    yield from _entry(os.fsencode(path))


def content_hash(path: str, observe: Callable[[bytes], None] | None = None) -> tuple[str, int]:
    """Return the SHA-256 in hex and the length of the serialisation of ``path``.

    ``observe`` receives every piece of the serialisation, so references
    can be scanned in the same pass (references.py).
    """
    digest = hashlib.sha256()
    size = 0
    for piece in serialise(path):
        digest.update(piece)
        size += len(piece)
        if observe is not None:
            observe(piece)
    return digest.hexdigest(), size


def _describe(path: bytes) -> tuple[str, bytes | int]:
    """Return what a tree comparison sees of one entry: its kind and a summary."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing", 0
    if stat.S_ISLNK(info.st_mode):
        return "link", os.readlink(path)
    if stat.S_ISDIR(info.st_mode):
        return "directory", 0
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            while chunk := file.read(_CHUNK):
                digest.update(chunk)
        kind = "executable" if info.st_mode & stat.S_IXUSR else "file"
        return kind, digest.digest()
    return "special", 0


def diff_trees(first: str, second: str) -> list[str]:
    """Return the relative paths whose entries differ between two trees.

    ``.`` stands for the top of the trees. Below a directory that is
    missing on one side only the directory itself is listed.
    """
    differences: list[str] = []
    pending = [b""]
    while pending:
        relative = pending.pop()
        a = os.path.join(os.fsencode(first), relative) if relative else os.fsencode(first)
        b = os.path.join(os.fsencode(second), relative) if relative else os.fsencode(second)
        left, right = _describe(a), _describe(b)
        if left != right:
            differences.append(os.fsdecode(relative) or ".")
            continue
        if left[0] == "directory":
            names = set(os.listdir(a)) | set(os.listdir(b))
            pending.extend(os.path.join(relative, name) for name in sorted(names, reverse=True))
    return differences


class VerifyResult(Model):
    """The registered and the recomputed content hash and size of one store path.

    ``ok`` is True if the path still has the contents it was registered with.
    """

    path: str
    ok: bool
    expected_sha256: str
    actual_sha256: str
    expected_size: int
    actual_size: int
