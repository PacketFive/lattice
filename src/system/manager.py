# SPDX-License-Identifier: GPL-2.0
"""What system activation asks of the machine it changes (RFC 0004 section 8).

Activation writes files itself and leaves units, mounts and ownership to a
SystemManager. SystemdManager acts on the running machine with
``systemctl`` and ``mount``. OfflineManager is for a root that is not
running, such as a disk an installer prepares: it only records what it
was asked, and changes owners when it runs as root.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - runs systemctl and mount with fixed arguments
from collections.abc import Sequence
from typing import Protocol

from ..errors import ActivationFailed

SEARCH_PATH = "/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class SystemManager(Protocol):
    """The operations activation needs from the init system and the kernel."""

    def daemon_reload(self) -> None:
        """Make the init system read its unit files again."""

    def start(self, units: Sequence[str]) -> None:
        """Start ``units``."""

    def stop(self, units: Sequence[str]) -> None:
        """Stop ``units``."""

    def restart(self, units: Sequence[str]) -> None:
        """Restart ``units``."""

    def mount(self, mount_point: str) -> None:
        """Mount the file system ``/etc/fstab`` gives for ``mount_point``."""

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Make ``uid`` and ``gid`` the owners of ``path``."""


def _program(name: str) -> str:
    found = shutil.which(name, path=SEARCH_PATH)
    if found is None:
        raise ActivationFailed(f"{name} was not found in {SEARCH_PATH}")
    return found


class SystemdManager:
    """Acts on the running machine with ``systemctl`` and ``mount``."""

    def _run(self, *argv: str) -> None:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or f"status {result.returncode}"
            raise ActivationFailed(f"{' '.join(argv)}: {message}")

    def daemon_reload(self) -> None:
        """Run ``systemctl daemon-reload``."""
        self._run(_program("systemctl"), "daemon-reload")

    def start(self, units: Sequence[str]) -> None:
        """Run ``systemctl start`` for ``units``."""
        if units:
            self._run(_program("systemctl"), "start", "--", *units)

    def stop(self, units: Sequence[str]) -> None:
        """Run ``systemctl stop`` for ``units``."""
        if units:
            self._run(_program("systemctl"), "stop", "--", *units)

    def restart(self, units: Sequence[str]) -> None:
        """Run ``systemctl restart`` for ``units``."""
        if units:
            self._run(_program("systemctl"), "restart", "--", *units)

    def mount(self, mount_point: str) -> None:
        """Run ``mount`` for ``mount_point``, which ``/etc/fstab`` describes."""
        self._run(_program("mount"), "--", mount_point)

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change the owners of ``path`` without following a symlink."""
        os.chown(path, uid, gid, follow_symlinks=False)


class OfflineManager:
    """Records every request in ``actions`` instead of acting on a running machine.

    ``chown`` changes the owner when this process runs as root, so a root
    prepared by an installer has the right owners.
    """

    def __init__(self) -> None:
        """Start with no recorded actions."""
        self.actions: list[str] = []

    def daemon_reload(self) -> None:
        """Record the reload."""
        self.actions.append("daemon-reload")

    def start(self, units: Sequence[str]) -> None:
        """Record the units to start."""
        self.actions += [f"start {unit}" for unit in units]

    def stop(self, units: Sequence[str]) -> None:
        """Record the units to stop."""
        self.actions += [f"stop {unit}" for unit in units]

    def restart(self, units: Sequence[str]) -> None:
        """Record the units to restart."""
        self.actions += [f"restart {unit}" for unit in units]

    def mount(self, mount_point: str) -> None:
        """Record the mount."""
        self.actions.append(f"mount {mount_point}")

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Record the change, and make it when running as root."""
        self.actions.append(f"chown {uid}:{gid} {path}")
        if os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=False)
