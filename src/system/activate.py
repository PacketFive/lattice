# SPDX-License-Identifier: GPL-2.0
"""Activation of a system (RFC 0004 section 8.1).

Activation changes the machine under a system root so that it runs the
system at a store path. The Lattice class (api.py) runs the steps in
order and handles generations and slots; this module holds the steps
that act on the root:

``etc``
    Points ``/etc/static`` at the system's ``etc`` and makes each file the
    system manages a symlink into ``/etc/static``. Files the previous
    system managed and this one does not are removed. A file Lattice did
    not create stops activation before anything changes.
``users``
    Writes accounts (accounts.py).
``file systems``
    Mounts new file systems that are not needed for boot, and reports
    changed ones, and new ones needed for boot that are not mounted yet,
    which apply at the next boot.
``units``
    Reloads systemd, stops removed units, restarts changed units and
    starts new ones.
``current``
    Points ``/run/current-system`` at the system.

Each step is idempotent, so running activation again after a failure
finishes the work.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..errors import ActivationFailed, StoreError
from ..events import ActivationNotice, ActivationStep, EventHandler, emit
from ..profiles import replace_link
from ..store.layout import StoreLayout
from .accounts import write_accounts
from .build import read_system
from .manager import SystemManager
from .spec import SystemSpec

STATIC = "/etc/static"
CURRENT = "run/current-system"
BOOTED = "run/booted-system"
UNIT_DIRECTORY = "systemd/system"


def _under(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/"))


def managed_files(layout: StoreLayout, system: str) -> set[str]:
    """Return the path, relative to ``/etc``, of every file the system at ``system`` manages."""
    top = os.path.join(layout.physical(system), "etc")
    found: set[str] = set()
    for directory, subdirectories, files in os.walk(top):
        relative = os.path.relpath(directory, top)
        links = [d for d in subdirectories if os.path.islink(os.path.join(directory, d))]
        for name in files + links:
            found.add(os.path.normpath(os.path.join(relative, name)))
    return found


def _units(layout: StoreLayout, system: str | None) -> dict[str, bytes]:
    """Return the unit files of ``system`` by name, read through the store."""
    if system is None:
        return {}
    directory = os.path.join(layout.physical(system), "etc", UNIT_DIRECTORY)
    units: dict[str, bytes] = {}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return units
    for name in sorted(names):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as file:
                units[name] = file.read()
    return units


def previous_system(layout: StoreLayout, root: str) -> str | None:
    """Return the system ``/etc/static`` points at under ``root``, if it is in the store."""
    try:
        target = os.readlink(_under(root, STATIC))
    except OSError:
        return None
    system = os.path.dirname(target)
    try:
        if os.path.basename(target) != "etc" or read_system(layout, system) is None:
            return None
    except StoreError:
        return None
    return system


class Activation:
    """The steps that make the root ``root`` run the system at ``system``.

    ``previous`` is the system the root ran before, found from
    ``/etc/static``. Raises ActivationFailed if ``system`` is not a
    registered system.
    """

    def __init__(
        self,
        layout: StoreLayout,
        root: str,
        system: str,
        manager: SystemManager,
        on_event: EventHandler | None = None,
    ) -> None:
        """Read the system and find the previous one. Nothing under ``root`` changes."""
        self.layout = layout
        self.root = root
        self.system = system
        self.manager = manager
        self.on_event = on_event
        try:
            spec = read_system(layout, system)
        except StoreError as error:
            raise ActivationFailed(str(error)) from None
        if spec is None:
            raise ActivationFailed(f"{system} is not a system")
        self.spec: SystemSpec = spec
        self.previous = previous_system(layout, root)
        self.previous_spec = read_system(layout, self.previous) if self.previous else None

    def notice(self, text: str) -> None:
        """Report ``text`` as an ActivationNotice."""
        emit(self.on_event, ActivationNotice(text=text))

    def run(self, step: str, action: Callable[[], None]) -> None:
        """Run ``action`` as ``step``, naming the step in any failure."""
        emit(self.on_event, ActivationStep(step=step, store_path=self.system))
        try:
            action()
        except ActivationFailed as error:
            raise ActivationFailed(f"activation step {step!r} failed: {error}") from None
        except OSError as error:
            raise ActivationFailed(f"activation step {step!r} failed: {error}") from None

    def etc(self) -> None:
        """Point ``/etc/static`` at the system's ``etc`` and link the managed files."""
        etc = _under(self.root, "/etc")
        new = managed_files(self.layout, self.system)
        old = managed_files(self.layout, self.previous) if self.previous else set()
        conflicts = sorted(path for path in new if not self._replaceable(etc, path))
        if conflicts:
            shown = ", ".join(f"/etc/{path}" for path in conflicts[:5])
            more = f" and {len(conflicts) - 5} more" if len(conflicts) > 5 else ""
            raise ActivationFailed(f"files not created by Lattice are in the way: {shown}{more}")
        os.makedirs(etc, mode=0o755, exist_ok=True)
        replace_link(os.path.join(etc, "static"), f"{self.system}/etc")
        for path in sorted(new):
            target = os.path.join(etc, path)
            if not os.path.lexists(target):
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                os.symlink(f"{STATIC}/{path}", target)
        for path in sorted(old - new, reverse=True):
            target = os.path.join(etc, path)
            if os.path.islink(target) and os.readlink(target) == f"{STATIC}/{path}":
                os.unlink(target)
                self._remove_empty(etc, os.path.dirname(path))

    @staticmethod
    def _replaceable(etc: str, path: str) -> bool:
        """Whether ``/etc/<path>`` is missing or a link Lattice made, with directories above it."""
        parent = os.path.dirname(path)
        while parent:
            above = os.path.join(etc, parent)
            if os.path.lexists(above) and (os.path.islink(above) or not os.path.isdir(above)):
                return False
            parent = os.path.dirname(parent)
        target = os.path.join(etc, path)
        if not os.path.lexists(target):
            return True
        return os.path.islink(target) and os.readlink(target) == f"{STATIC}/{path}"

    @staticmethod
    def _remove_empty(etc: str, relative: str) -> None:
        while relative:
            try:
                os.rmdir(os.path.join(etc, relative))
            except OSError:
                return
            relative = os.path.dirname(relative)

    def users(self) -> None:
        """Write the accounts of the system."""
        write_accounts(self.root, self.spec, self.manager, self.notice)

    def file_systems(self) -> None:
        """Mount new file systems that are not needed for boot; report changed ones."""
        old = {
            f.mount_point: f
            for f in (self.previous_spec.file_systems if self.previous_spec else ())
        }
        new = {f.mount_point: f for f in self.spec.file_systems}
        for point, fs in sorted(new.items()):
            if point not in old:
                if fs.needed_for_boot:
                    if not os.path.ismount(os.path.normpath(_under(self.root, point))):
                        self.notice(f"{point} is needed for boot and is mounted at the next boot")
                    continue
                os.makedirs(_under(self.root, point), mode=0o755, exist_ok=True)
                self.manager.mount(point)
            elif old[point] != fs:
                self.notice(f"the mount at {point} changed and applies at the next boot")
        for point in sorted(set(old) - set(new)):
            self.notice(f"{point} is no longer described and stays mounted until the next boot")

    def units(self) -> None:
        """Reload systemd, then stop, restart and start units as the systems differ."""
        old = _units(self.layout, self.previous)
        new = _units(self.layout, self.system)
        self.manager.daemon_reload()
        self.manager.stop(sorted(set(old) - set(new)))
        self.manager.restart(sorted(n for n in set(old) & set(new) if old[n] != new[n]))
        self.manager.start(sorted(set(new) - set(old)))

    def current(self) -> None:
        """Point ``/run/current-system`` at the system."""
        self._link(CURRENT)

    def booted(self) -> None:
        """Point ``/run/booted-system`` at the system, at boot."""
        self._link(BOOTED)

    def _link(self, relative: str) -> None:
        path = _under(self.root, relative)
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        replace_link(path, self.system)


def running_system(root: str, which: str = CURRENT) -> str | None:
    """Return the store path ``/run/current-system`` (or ``which``) points at, or None."""
    try:
        return os.readlink(_under(root, which))
    except OSError:
        return None
