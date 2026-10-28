# SPDX-License-Identifier: GPL-2.0
"""Loading and unloading kernel modules (RFC 0001 section 13.3).

KernelModules is what activation needs from the running kernel. LinuxModules
implements it with the ``finit_module``, ``init_module`` and
``delete_module`` system calls and the state in ``/proc``, so Lattice
checks and reports every step itself instead of running ``modprobe``.
Tests pass a fake that records the calls.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from .. import procscan
from ..errors import ActivationFailed
from ..models import Model
from ..procscan import ProcessUse
from . import syscalls
from .modinfo import read_image


class LoadedModule(Model):
    """A module the kernel has loaded.

    ``refcount`` is the kernel's use count, which counts open device files
    and ``holders``, the loaded modules that depend on this one.
    """

    name: str
    refcount: int
    holders: tuple[str, ...]


@runtime_checkable
class KernelModules(Protocol):
    """The kernel module operations activation uses."""

    def release(self) -> str:
        """Return the release of the running kernel, as ``uname -r`` prints it."""
        ...

    def loaded(self) -> dict[str, LoadedModule]:
        """Return the loaded modules by name."""
        ...

    def load(self, path: str) -> None:
        """Load the module file at ``path``. Raises ActivationFailed."""
        ...

    def unload(self, name: str) -> None:
        """Unload module ``name`` if nothing uses it. Raises ActivationFailed."""
        ...

    def open_devices(self, patterns: tuple[str, ...]) -> tuple[ProcessUse, ...]:
        """Return the processes holding a file that matches one of ``patterns`` open."""
        ...


class LinuxModules:
    """KernelModules for the running Linux kernel. Changing modules needs CAP_SYS_MODULE."""

    def __init__(self, proc_root: str = "/proc") -> None:
        """Read process and module state from ``proc_root``."""
        self.proc_root = proc_root

    def release(self) -> str:
        """Return ``os.uname().release``."""
        return os.uname().release

    def loaded(self) -> dict[str, LoadedModule]:
        """Parse ``/proc/modules``."""
        found: dict[str, LoadedModule] = {}
        try:
            with open(os.path.join(self.proc_root, "modules"), encoding="utf-8") as file:
                lines = file.read().splitlines()
        except FileNotFoundError:
            return found
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or not fields[2].lstrip("-").isdigit():
                continue
            holders = tuple(h for h in fields[3].split(",") if h and h != "-")
            found[fields[0]] = LoadedModule(
                name=fields[0], refcount=int(fields[2]), holders=holders
            )
        return found

    def load(self, path: str) -> None:
        """Load ``path`` with finit_module, or init_module if it is compressed."""
        try:
            if path.endswith(".ko"):
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    syscalls.finit_module(fd)
                finally:
                    os.close(fd)
            else:
                syscalls.init_module(read_image(path))
        except OSError as error:
            raise ActivationFailed(f"cannot load {path}: {error.strerror}") from None

    def unload(self, name: str) -> None:
        """Unload ``name`` with delete_module, without waiting for users to go away."""
        try:
            syscalls.delete_module(name)
        except OSError as error:
            raise ActivationFailed(f"cannot unload {name}: {error.strerror}") from None

    def open_devices(self, patterns: tuple[str, ...]) -> tuple[ProcessUse, ...]:
        """Scan ``/proc/<pid>/fd`` with procscan.open_devices."""
        return procscan.open_devices(patterns, self.proc_root)
