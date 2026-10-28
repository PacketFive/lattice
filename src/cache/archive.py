# SPDX-License-Identifier: GPL-2.0
"""Deterministic tar.xz archives of store paths (RFC 0001 section 14.2).

The same store path always gives the same archive bytes. Entries come
in the order of the ``lattice-tree-1`` serialisation (store/verify.py):
each directory before its contents, and the names in a directory sorted
by their bytes. Every entry has modification time 0, owner and group 0
with empty names, and mode 0755 for directories and executable files,
0644 for other files and 0777 for symbolic links. The top entry is named
after the store path, for example ``<hash>-hello``, and the others are
below it.

extract trusts nothing in an archive. It accepts only directories,
regular files and symbolic links under the expected top entry, creates
every entry itself without following links, never writes through a
symbolic link, and stops once the files hold more bytes than the signed
size of the store path.
"""

from __future__ import annotations

import hashlib
import lzma
import os
import stat
import tarfile
from typing import IO

from ..errors import SubstitutionFailed
from ..models import Model
from ..store.verify import FORMAT

_CHUNK = 1 << 16
_PRESET = 6


class ArchiveInfo(Model):
    """The SHA-256 and size of the compressed archive."""

    sha256: str
    size: int


class _HashingWriter:
    """A write-only file that hashes and counts what passes through to ``target``."""

    def __init__(self, target: IO[bytes]) -> None:
        self.target = target
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.size += len(data)
        return self.target.write(data)

    def flush(self) -> None:
        self.target.flush()


def _info(name: str, kind: bytes, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add(tar: tarfile.TarFile, path: bytes, name: str) -> None:
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        info = _info(name, tarfile.SYMTYPE, 0o777)
        info.linkname = os.fsdecode(os.readlink(path))
        tar.addfile(info)
    elif stat.S_ISREG(mode):
        info = _info(name, tarfile.REGTYPE, 0o755 if mode & stat.S_IXUSR else 0o644)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as file:
            info.size = os.fstat(file.fileno()).st_size
            tar.addfile(info, file)
    elif stat.S_ISDIR(mode):
        tar.addfile(_info(name, tarfile.DIRTYPE, 0o755))
        for child in sorted(os.listdir(path)):
            _add(tar, os.path.join(path, child), f"{name}/{os.fsdecode(child)}")
    else:
        raise SubstitutionFailed(f"{os.fsdecode(path)} is not a file, directory or link")


def create(tree: str, name: str, destination: str) -> ArchiveInfo:
    """Write the archive of the store path at ``tree``, named ``name``, to ``destination``.

    ``destination`` must not exist. Returns the hash and size of the file.
    """
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "wb") as raw:
        writer = _HashingWriter(raw)
        with lzma.LZMAFile(writer, "wb", format=lzma.FORMAT_XZ, preset=_PRESET) as compressed:  # type: ignore[arg-type]
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                _add(tar, os.fsencode(tree), name)
    return ArchiveInfo(sha256=writer.digest.hexdigest(), size=writer.size)


def _relative(member: str, name: str) -> str:
    """Return the part of ``member`` below the top entry ``name``, or raise."""
    if member == name:
        return ""
    if not member.startswith(name + "/"):
        raise SubstitutionFailed(f"archive entry {member!r} is not under {name}")
    relative = member[len(name) + 1 :]
    if "\0" in relative or any(part in ("", ".", "..") for part in relative.split("/")):
        raise SubstitutionFailed(f"archive entry {member!r} is not a plain path")
    return relative


def extract(archive: str, name: str, destination: str, limit: int) -> None:
    """Unpack the archive file ``archive`` of store path ``name`` to ``destination``.

    ``destination`` must not exist and becomes the top entry. Raises
    SubstitutionFailed if the archive is malformed, holds anything but
    the tree of ``name``, or its ``lattice-tree-1`` serialisation would
    be longer than ``limit``, the signed size. Every entry is charged its
    share of that serialisation before it is created, so an archive of
    many empty entries is refused as early as one large file.
    """
    directories: set[str] = set()
    written = len(FORMAT)
    first = True

    def charge(size: int) -> None:
        nonlocal written
        written += size
        if written > limit:
            raise SubstitutionFailed(f"archive of {name} is larger than its signed size")

    try:
        with (
            lzma.open(archive, "rb") as compressed,
            tarfile.open(fileobj=compressed, mode="r|") as tar,
        ):
            for member in tar:
                relative = _relative(member.name, name)
                if first != (relative == ""):
                    raise SubstitutionFailed(f"archive of {name} must start with its top entry")
                parent = relative.rpartition("/")[0]
                if not first and parent not in directories:
                    raise SubstitutionFailed(
                        f"archive entry {member.name!r} comes before its directory"
                    )
                if relative:
                    charge(8 + len(os.fsencode(relative.rpartition("/")[2])))
                first = False
                target = os.path.join(destination, relative) if relative else destination
                if member.isdir():
                    charge(9)
                    os.mkdir(target, 0o755)
                    directories.add(relative)
                elif member.issym():
                    if "\0" in member.linkname:
                        raise SubstitutionFailed(
                            f"archive entry {member.name!r} is not a plain link"
                        )
                    charge(9 + len(os.fsencode(member.linkname)))
                    os.symlink(member.linkname, target)
                elif member.isreg():
                    charge(10 + member.size)
                    _write(tar, member, target)
                else:
                    raise SubstitutionFailed(
                        f"archive entry {member.name!r} is not a file, directory or link"
                    )
    except (tarfile.TarError, lzma.LZMAError, EOFError) as error:
        raise SubstitutionFailed(f"archive of {name} is damaged: {error}") from None
    except FileExistsError:
        raise SubstitutionFailed(f"archive of {name} holds an entry twice") from None
    if first:
        raise SubstitutionFailed(f"archive of {name} is empty")


def _write(tar: tarfile.TarFile, member: tarfile.TarInfo, target: str) -> None:
    source = tar.extractfile(member)
    if source is None:  # pragma: no cover - isreg() members always have data
        raise SubstitutionFailed(f"archive entry {member.name!r} has no data")
    mode = 0o755 if member.mode & stat.S_IXUSR else 0o644
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as file:
        remaining = member.size
        while remaining > 0:
            chunk = source.read(min(_CHUNK, remaining))
            if not chunk:
                raise SubstitutionFailed(f"archive entry {member.name!r} is truncated")
            file.write(chunk)
            remaining -= len(chunk)
