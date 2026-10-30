# SPDX-License-Identifier: GPL-2.0
"""The builder ``builtin:system`` (RFC 0004 section 6).

It writes a whole system into one store path from the description that
``machine`` recorded in ``env["system"]``:

- ``lattice-system.json``: the description, which activation reads.
- ``lattice-profile.json``: the manifest of ``sw``, so the system is
  also a profile generation and its stacks can be activated.
- ``sw``: a link to the profile of the system's packages and stacks.
- ``kernel`` and ``initrd``: links to the kernel image and the initial
  RAM disk, if the description has them.
- ``kernel-params``: the kernel command line the description adds.
- ``init``: the script the kernel runs first.
- ``etc``: the tree activation links ``/etc/static`` to.
- ``units``: a link to ``etc/systemd/system``.

Every file is written from the description alone, so two builds of the
same description give the same tree.
"""

from __future__ import annotations

import os
import re

from ..derivation import BUILTIN_PROFILE, BUILTIN_SYSTEM, STORE_DIR, Derivation
from ..errors import BuildFailed, StoreError
from ..settings import DEFAULT_STACKS
from ..store.combine import PROFILE_MANIFEST, ProfileManifest, read_manifest
from ..store.layout import StoreLayout
from .accounts import accounts_json
from .spec import SystemSpec

SYSTEM_MANIFEST = "lattice-system.json"
CURRENT_SYSTEM = "/run/current-system"
KERNEL_IMAGE = "bzImage"
INITRD_IMAGE = "initrd"
UNITS = "etc/systemd/system"
WANTS = "multi-user.target.wants"

_KEYS = re.compile(r"system|sw|kernel|initrd|etc_source_(0|[1-9][0-9]*)")


def _input(drv: Derivation, key: str) -> Derivation | None:
    value = drv.env.get(key)
    if value is not None and not isinstance(value, Derivation):
        raise BuildFailed(f"{drv.name}: {key} must be a derivation")
    return value


def system_spec(drv: Derivation) -> SystemSpec:
    """Return the description in ``drv`` after checking it against its inputs.

    Raises BuildFailed if ``env`` is not what ``machine`` writes.
    """
    for key in drv.env:
        if not _KEYS.fullmatch(str(key)):
            raise BuildFailed(f"{drv.name}: unexpected env entry {key!r}")
    text = drv.env.get("system")
    if not isinstance(text, str):
        raise BuildFailed(f"{drv.name}: env system must be the description as JSON")
    try:
        spec = SystemSpec.model_validate_json(text)
    except ValueError as error:
        raise BuildFailed(f"{drv.name}: the description is not valid: {error}") from None
    sw = _input(drv, "sw")
    if sw is None or sw.builder != BUILTIN_PROFILE or sw.store_path != spec.sw:
        raise BuildFailed(f"{drv.name}: sw must be the profile the description names")
    for key in ("kernel", "initrd"):
        given = _input(drv, key)
        if (given.store_path if given is not None else None) != getattr(spec, key):
            raise BuildFailed(f"{drv.name}: {key} does not match the description")
    sources = {
        value.store_path
        for key, value in drv.env.items()
        if str(key).startswith("etc_source_") and isinstance(value, Derivation)
    }
    if sum(str(key).startswith("etc_source_") for key in drv.env) != len(sources):
        raise BuildFailed(f"{drv.name}: every etc_source entry must be a different derivation")
    if sources != {e.source for e in spec.etc if e.source is not None}:
        raise BuildFailed(f"{drv.name}: the etc sources do not match the description")
    return spec


def _image(layout: StoreLayout, path: str, name: str) -> str:
    """Return ``path``, or ``path/name`` if the output is a directory."""
    physical = layout.physical(path)
    return f"{path}/{name}" if os.path.isdir(physical) else path


def _write(path: str, text: str, mode: int = 0o444) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path, "x", encoding="utf-8") as file:
        file.write(text)
    os.chmod(path, mode)


def _link(target: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    os.symlink(target, path)


def init_script(spec: SystemSpec, system: str) -> str:
    """Return the ``init`` script of the system at store path ``system``.

    It mounts the kernel file systems the initial RAM disk did not, makes
    the store read only, runs ``lattice activate-boot`` and then starts
    systemd, or a shell if the system has no systemd.
    """
    sw = spec.sw
    return "\n".join(
        [
            f"#!{sw}/bin/sh",
            f"# The first program of system {spec.name}, written by Lattice.",
            f"PATH={sw}/bin",
            "export PATH",
            "mounted() {",
            "    while read -r _ point _; do",
            '        [ "$point" = "$1" ] && return 0',
            "    done < /proc/mounts",
            "    return 1",
            "}",
            "[ -e /proc/self ] || mount -t proc proc /proc",
            "mounted /sys || mount -t sysfs sysfs /sys",
            "mounted /dev || mount -t devtmpfs devtmpfs /dev",
            "mounted /run || mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run",
            f"if ! mounted {STORE_DIR}; then",
            f"    mount --bind {STORE_DIR} {STORE_DIR} &&",
            f"        mount -o remount,ro,bind {STORE_DIR}",
            "fi",
            f"lattice activate-boot --system {system} ||",
            '    echo "init: activation failed; starting anyway" >&2',
            f"if [ -x {sw}/lib/systemd/systemd ]; then",
            f"    exec {sw}/lib/systemd/systemd",
            "fi",
            f"exec {sw}/bin/sh",
            "",
        ]
    )


def _profile(slots: list[str]) -> str:
    lines = [
        "# Written by Lattice from the system description.",
        f"PATH={CURRENT_SYSTEM}/sw/bin",
    ]
    lines += [f'PATH="$PATH:{DEFAULT_STACKS}/{slot}/bin"' for slot in slots]
    lines += ["export PATH", ""]
    return "\n".join(lines)


def write_etc(spec: SystemSpec, etc: str, slots: list[str]) -> None:
    """Write the ``etc`` tree of ``spec`` into the new directory ``etc``."""
    os.mkdir(etc, 0o755)
    fstab = ["# Written by Lattice from the system description."]
    fstab += [f.fstab_line for f in spec.file_systems]
    _write(os.path.join(etc, "fstab"), "\n".join(fstab) + "\n")
    _write(os.path.join(etc, "hostname"), spec.host_name + "\n")
    _link(f"{spec.sw}/share/zoneinfo/{spec.time_zone}", os.path.join(etc, "localtime"))
    _write(os.path.join(etc, "locale.conf"), "LANG=C.UTF-8\n")
    _write(os.path.join(etc, "profile"), _profile(slots))
    libraries = "".join(f"{DEFAULT_STACKS}/{slot}/lib\n" for slot in slots)
    _write(os.path.join(etc, "ld.so.conf.d", "lattice-stacks.conf"), libraries)
    _write(os.path.join(etc, "pyrixos", "system-users.json"), accounts_json(spec))
    units = os.path.join(etc, "systemd", "system")
    os.makedirs(os.path.join(units, WANTS), mode=0o755)
    for service in spec.services:
        _write(os.path.join(units, service.unit_name), service.unit_text())
        _link(f"../{service.unit_name}", os.path.join(units, WANTS, service.unit_name))
    for entry in spec.etc:
        path = os.path.join(etc, entry.path)
        if entry.source is not None:
            _link(entry.source, path)
        else:
            _write(path, entry.text or "", int(entry.mode, 8))


def build_system(layout: StoreLayout, drv: Derivation, destination: str) -> dict[str, str]:
    """Write the system ``drv`` at ``destination`` and return its outputs.

    Raises BuildFailed if ``env`` does not match the description or ``sw``
    is not a registered profile.
    """
    if drv.builder != BUILTIN_SYSTEM or drv.outputs != ("out",):
        raise BuildFailed(f"{drv.name}: a system has the single output out")
    spec = system_spec(drv)
    try:
        manifest = read_manifest(layout, spec.sw, ProfileManifest)
    except StoreError as error:
        raise BuildFailed(f"{drv.name}: {error}") from None
    if manifest is None:
        raise BuildFailed(f"{drv.name}: {spec.sw} is not a profile")
    os.mkdir(destination, 0o755)
    _write(
        os.path.join(destination, SYSTEM_MANIFEST),
        spec.model_dump_json(indent=2) + "\n",
    )
    _write(
        os.path.join(destination, PROFILE_MANIFEST),
        manifest.model_dump_json(indent=2) + "\n",
    )
    os.symlink(spec.sw, os.path.join(destination, "sw"))
    if spec.kernel is not None:
        os.symlink(_image(layout, spec.kernel, KERNEL_IMAGE), os.path.join(destination, "kernel"))
    if spec.initrd is not None:
        os.symlink(_image(layout, spec.initrd, INITRD_IMAGE), os.path.join(destination, "initrd"))
    _write(os.path.join(destination, "kernel-params"), " ".join(spec.kernel_params) + "\n")
    _write(os.path.join(destination, "init"), init_script(spec, drv.store_path), 0o555)
    write_etc(spec, os.path.join(destination, "etc"), sorted(manifest.stacks))
    os.symlink(UNITS, os.path.join(destination, "units"))
    return {"out": destination}


def read_system(layout: StoreLayout, path: str) -> SystemSpec | None:
    """Return the description of the system at store path ``path``, or None if it is not one.

    Raises StoreError if ``lattice-system.json`` is there but not valid.
    """
    try:
        with open(os.path.join(layout.physical(path), SYSTEM_MANIFEST), "rb") as file:
            text = file.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return SystemSpec.model_validate_json(text)
    except ValueError as error:
        raise StoreError(f"{path}/{SYSTEM_MANIFEST} is not valid: {error}") from None
