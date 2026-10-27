# SPDX-License-Identifier: GPL-2.0
"""Kernel module files and their metadata.

A kernel module is an ELF64 relocatable object whose ``.modinfo`` section
holds NUL-separated ``key=value`` strings: its name, the ``vermagic`` of
the kernel it was built for, the modules it depends on and more. read_info
reads that section with a minimal ELF parser, so no external tool such as
``modinfo`` is needed. Modules compressed with xz or gzip are decompressed
in memory first.
"""

from __future__ import annotations

import gzip
import lzma
import os
import struct

from ..errors import ActivationFailed
from ..models import Model

SUFFIXES: tuple[str, ...] = (".ko", ".ko.xz", ".ko.gz")

_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_BYTE_ORDER = {1: "<", 2: ">"}


class ModuleInfo(Model):
    """What one kernel module file says about itself.

    ``name`` is the module name the kernel uses, with ``-`` replaced by
    ``_``. ``release`` is the first word of ``vermagic``, the release of
    the kernel the module was built for, or empty if it has none.
    """

    name: str
    file: str
    release: str
    depends: tuple[str, ...]


def module_name(name: str) -> str:
    """Return ``name`` as the kernel spells module names."""
    return name.replace("-", "_")


def read_image(path: str) -> bytes:
    """Return the uncompressed contents of the module file at ``path``."""
    try:
        if path.endswith(".xz"):
            with lzma.open(path) as file:
                return file.read()
        if path.endswith(".gz"):
            with gzip.open(path) as file:
                return file.read()
        with open(path, "rb") as file:
            return file.read()
    except (OSError, lzma.LZMAError, EOFError) as error:
        raise ActivationFailed(f"{path}: cannot read: {error}") from None


def section(image: bytes, wanted: str) -> bytes | None:
    """Return the contents of section ``wanted`` of the ELF64 file ``image``, or None.

    Raises ActivationFailed if ``image`` is not a well-formed ELF64 file.
    """
    if len(image) < 64 or image[:4] != _ELF_MAGIC or image[4] != _ELFCLASS64:
        raise ActivationFailed("not an ELF64 file")
    order = _BYTE_ORDER.get(image[5])
    if order is None:
        raise ActivationFailed("unknown ELF byte order")
    (shoff,) = struct.unpack_from(order + "Q", image, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from(order + "HHH", image, 0x3A)
    if shentsize < 64 or shstrndx >= shnum or shoff + shnum * shentsize > len(image):
        raise ActivationFailed("the ELF section table is not valid")

    def header(index: int) -> tuple[int, int, int]:
        name, _, _, _, offset, size = struct.unpack_from(
            order + "IIQQQQ", image, shoff + index * shentsize
        )
        if offset + size > len(image):
            raise ActivationFailed("an ELF section lies outside the file")
        return name, offset, size

    _, names_offset, names_size = header(shstrndx)
    names = image[names_offset : names_offset + names_size]
    for index in range(shnum):
        name, offset, size = header(index)
        if name >= len(names):
            continue
        end = names.find(b"\0", name)
        if names[name : end if end >= 0 else len(names)] == wanted.encode():
            return image[offset : offset + size]
    return None


def parse_info(path: str, image: bytes) -> ModuleInfo:
    """Return the ModuleInfo of the module ``image`` read from ``path``."""
    try:
        data = section(image, ".modinfo")
    except ActivationFailed as error:
        raise ActivationFailed(f"{path}: {error}") from None
    if data is None:
        raise ActivationFailed(f"{path}: has no .modinfo section")
    fields: dict[str, str] = {}
    for item in data.split(b"\0"):
        key, sep, value = item.decode("utf-8", "replace").partition("=")
        if sep and key not in fields:
            fields[key] = value
    base = os.path.basename(path)
    for suffix in SUFFIXES:
        base = base.removesuffix(suffix)
    depends = tuple(module_name(d) for d in fields.get("depends", "").split(",") if d)
    return ModuleInfo(
        name=module_name(fields.get("name") or base),
        file=path,
        release=fields.get("vermagic", "").split(" ", 1)[0],
        depends=depends,
    )


def read_info(path: str) -> ModuleInfo:
    """Return the ModuleInfo of the module file at ``path``. Raises ActivationFailed."""
    return parse_info(path, read_image(path))


def find_modules(directory: str) -> tuple[str, ...]:
    """Return every module file under ``directory``, sorted, without following links."""
    found: list[str] = []
    for top, dirs, files in os.walk(directory):
        dirs.sort()
        found.extend(os.path.join(top, name) for name in files if name.endswith(SUFFIXES))
    return tuple(sorted(found))


def load_order(modules: tuple[ModuleInfo, ...]) -> tuple[ModuleInfo, ...]:
    """Return ``modules`` with each one after the modules among them it depends on.

    Dependencies outside ``modules`` are left to the kernel. Raises
    ActivationFailed if two files have the same module name or the
    dependencies form a cycle.
    """
    by_name: dict[str, ModuleInfo] = {}
    for info in modules:
        if info.name in by_name:
            raise ActivationFailed(
                f"{by_name[info.name].file} and {info.file} are both {info.name}"
            )
        by_name[info.name] = info
    order: list[ModuleInfo] = []
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ActivationFailed(f"kernel module {name} depends on itself")
        state[name] = 1
        for dependency in by_name[name].depends:
            if dependency in by_name:
                visit(dependency)
        state[name] = 2
        order.append(by_name[name])

    for name in sorted(by_name):
        visit(name)
    return tuple(order)
