# SPDX-License-Identifier: GPL-2.0
"""The builder behind ``unpack`` (RFC 0002 section 5.3).

A derivation with the builder ``builtin:unpack`` runs here, in Lattice,
because before the bootstrap seed is unpacked no program in the store
could do it. It runs with the rights of Lattice, so an archive must not be
able to write outside its output: absolute names, ``..``, links that leave
the output, device files and FIFOs are refused, and setuid, setgid and
sticky bits and write permission for group and others are cleared.
"""

from __future__ import annotations

import os
import posixpath
import tarfile

from ..derivation import Derivation
from ..errors import BuildFailed
from .layout import StoreLayout

_KEEP_MODE = 0o755


def _check_member(drv: Derivation, member: tarfile.TarInfo) -> None:
    name = member.name
    if name.startswith("/") or ".." in name.split("/"):
        raise BuildFailed(f"{drv.name}: archive entry {name!r} leaves the output")
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise BuildFailed(f"{drv.name}: archive entry {name!r} is not a file, directory or link")
    if member.issym() or member.islnk():
        target = member.linkname
        base = posixpath.dirname(name) if member.issym() else ""
        resolved = posixpath.normpath(posixpath.join(base, target))
        if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
            raise BuildFailed(f"{drv.name}: archive link {name!r} points outside the output")


def _filter(drv: Derivation, member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    _check_member(drv, member)
    parts = member.name.rstrip("/").split("/")[:-1]
    for end in range(1, len(parts) + 1):
        if os.path.islink(os.path.join(destination, *parts[:end])):
            raise BuildFailed(f"{drv.name}: archive entry {member.name!r} passes through a link")
    try:
        member = tarfile.data_filter(member, destination)
    except tarfile.FilterError as error:
        raise BuildFailed(f"{drv.name}: archive entry {member.name!r}: {error}") from None
    mode = member.mode
    if mode is not None:
        member = member.replace(mode=mode & _KEEP_MODE | 0o200, deep=False)
    return member


def unpack(layout: StoreLayout, drv: Derivation, destination: str) -> dict[str, str]:
    """Unpack the archive of ``drv`` into the new directory ``destination``.

    ``drv.env["archive"]`` must be a derivation whose ``out`` is one file,
    a tar archive that is not compressed or is compressed with gzip, xz
    or bzip2. Returns ``{"out": destination}``. Raises BuildFailed, naming
    the entry, if the archive breaks a rule of the module docstring; the
    partly written ``destination`` is left for the caller to remove with
    its work directory.
    """
    archive = drv.env.get("archive")
    if not isinstance(archive, Derivation) or set(drv.env) != {"archive"}:
        raise BuildFailed(f"{drv.name}: unpack needs exactly one derivation, archive, in env")
    if drv.outputs != ("out",):
        raise BuildFailed(f"{drv.name}: unpack has the single output out")
    source = layout.physical(archive.store_path)
    if not os.path.isfile(source):
        raise BuildFailed(f"{drv.name}: {archive.store_path} is not a file")
    os.mkdir(destination, 0o755)
    try:
        with tarfile.open(source, "r:*") as tar:
            for member in tar:
                tar.extract(member, destination, filter=lambda m, d: _filter(drv, m, d))
    except (tarfile.TarError, EOFError) as error:
        raise BuildFailed(f"{drv.name}: cannot read {archive.name}: {error}") from None
    _check_links(drv, destination)
    return {"out": destination}


def _check_links(drv: Derivation, destination: str) -> None:
    """Raise BuildFailed if a symlink in ``destination`` resolves outside it.

    A link can only be judged once every entry is written, since a later
    entry can turn a directory its target passes through into a link.
    """
    root = os.path.realpath(destination)
    for directory, names, files in os.walk(destination):
        for name in (*names, *files):
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            if target != root and not target.startswith(root + os.sep):
                entry = os.path.relpath(path, destination)
                raise BuildFailed(f"{drv.name}: archive link {entry!r} points outside the output")
