# SPDX-License-Identifier: GPL-2.0
"""Boot loader entries of system generations (RFC 0004 section 9.1).

Each generation of the ``system`` profile gets the entry
``boot/loader/entries/pyrixos-<n>.conf`` under the system root, in the
format of the Boot Loader Specification, type 1, which systemd-boot
reads. The boot loader cannot read the store, so the kernel and the
initial RAM disk are copied to ``boot/pyrixos/`` under names made from
their store paths; a file already there is not copied again. The
command line holds the system's ``kernel-params``, ``init=<system>/init``
and, unless the parameters already name one, the root file system of the
description.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid

from ..errors import ActivationFailed
from ..store.layout import StoreLayout
from .accounts import write_atomically
from .spec import STORE_REFERENCE, SystemSpec

ENTRIES = "boot/loader/entries"
IMAGES = "boot/pyrixos"
LOADER_CONF = "boot/loader/loader.conf"
_ENTRY = re.compile(r"pyrixos-(0|[1-9][0-9]*)\.conf")


def store_file(layout: StoreLayout, path: str) -> str:
    """Return where the file at ``path``, inside a store path, is on this machine."""
    match = STORE_REFERENCE.match(path)
    if match is None:
        raise ActivationFailed(f"{path} is not in the store")
    return layout.physical(match.group(0)) + path[match.end() :]


def image_name(path: str) -> str:
    """Return the name in ``boot/pyrixos`` of the file at store path ``path``."""
    return path.split("/", 3)[3].replace("/", "-")


def entry_path(root: str, number: int) -> str:
    """Return the path of the boot entry of generation ``number``."""
    return os.path.join(root, ENTRIES, f"pyrixos-{number}.conf")


def entries(root: str) -> dict[int, str]:
    """Return the path of every PyrixOS boot entry under ``root``, by generation number."""
    directory = os.path.join(root, ENTRIES)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return {}
    found = {}
    for name in names:
        match = _ENTRY.fullmatch(name)
        if match:
            found[int(match.group(1))] = os.path.join(directory, name)
    return dict(sorted(found.items()))


def _copy_image(layout: StoreLayout, root: str, system: str, entry: str) -> str | None:
    """Copy the file the system's link ``entry`` names into ``boot/pyrixos``."""
    link = os.path.join(layout.physical(system), entry)
    if not os.path.islink(link):
        return None
    target = os.readlink(link)
    name = image_name(target)
    destination = os.path.join(root, IMAGES, name)
    if not os.path.exists(destination):
        os.makedirs(os.path.dirname(destination), mode=0o755, exist_ok=True)
        temporary = os.path.join(os.path.dirname(destination), f".{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(store_file(layout, target), temporary)
            os.chmod(temporary, 0o644)
            os.rename(temporary, destination)
        except FileNotFoundError:
            raise ActivationFailed(f"{target} is not a file") from None
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    return f"/pyrixos/{name}"


def command_line(spec: SystemSpec, system: str) -> str:
    """Return the kernel command line of the system ``spec`` at store path ``system``."""
    params = list(spec.kernel_params)
    root_fs = spec.root_file_system
    if root_fs is not None and not any(p.startswith("root=") for p in params):
        params.append(f"root={root_fs.device}")
        if root_fs.fs_type != "auto" and not any(p.startswith("rootfstype=") for p in params):
            params.append(f"rootfstype={root_fs.fs_type}")
    params.append(f"init={system}/init")
    return " ".join(params)


def write_entry(
    layout: StoreLayout, root: str, number: int, system: str, spec: SystemSpec
) -> str | None:
    """Write the boot entry of generation ``number`` and return its path.

    Returns None if the system has no kernel, since it cannot boot.
    """
    kernel = _copy_image(layout, root, system, "kernel")
    if kernel is None:
        return None
    lines = [
        f"title PyrixOS {spec.name} (generation {number})",
        f"version {number}",
        "sort-key pyrixos",
        f"linux {kernel}",
    ]
    initrd = _copy_image(layout, root, system, "initrd")
    if initrd is not None:
        lines.append(f"initrd {initrd}")
    lines.append(f"options {command_line(spec, system)}")
    path = entry_path(root, number)
    write_atomically(path, "\n".join(lines) + "\n", 0o644)
    return path


def set_default(root: str, number: int) -> None:
    """Make the entry of generation ``number`` the default in ``loader.conf``."""
    path = os.path.join(root, LOADER_CONF)
    try:
        with open(path, encoding="utf-8") as file:
            lines = file.read().splitlines()
    except FileNotFoundError:
        lines = ["timeout 3"]
    lines = [line for line in lines if line.split(maxsplit=1)[:1] != ["default"]]
    lines.append(f"default pyrixos-{number}.conf")
    write_atomically(path, "\n".join(lines) + "\n", 0o644)


def remove_entries(root: str, numbers: set[int]) -> None:
    """Remove the entries of generations ``numbers`` and the images no entry uses."""
    for number, path in entries(root).items():
        if number in numbers:
            os.unlink(path)
    used: set[str] = set()
    for path in entries(root).values():
        with open(path, encoding="utf-8") as file:
            for line in file:
                key, _, value = line.strip().partition(" ")
                if key in ("linux", "initrd"):
                    used.add(os.path.basename(value.strip()))
    directory = os.path.join(root, IMAGES)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in names:
        if name not in used and not name.startswith("."):
            os.unlink(os.path.join(directory, name))
