# SPDX-License-Identifier: GPL-2.0
"""The build sandbox (RFC 0001 section 9).

spec.py describes a sandbox, environment.py the builder's environment
and seccomp.py the system calls it may not make. namespaces.py is the
backend that runs a build in Linux namespaces, runner.py supervises a
build and probe.py reports which backends work on this machine.

namespaces.py is also the program the backend runs, so it is imported
only when a backend is chosen.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from ..errors import BackendUnavailable
from ..settings import SandboxBackend
from .probe import BackendStatus, backend_status
from .runner import BuildOutcome, BuildRequest, run_build
from .spec import OciConfig, SandboxJob, SandboxSpec, make_spec, oci_config


class Backend(Protocol):
    """A way to run a SandboxSpec (section 9.7)."""

    name: str

    def status(self) -> BackendStatus:
        """Test whether this machine can run the backend."""
        ...

    def start(self, job: SandboxJob, *, status_fd: int, log_fd: int) -> subprocess.Popen[bytes]:
        """Start ``job``, reporting on ``status_fd`` and logging to ``log_fd``."""
        ...


def select_backend(name: SandboxBackend) -> Backend:
    """Return the backend called ``name``.

    Raises BackendUnavailable when it cannot run here. Lattice never falls
    back to a weaker backend.
    """
    if name != "namespaces":
        raise BackendUnavailable(f"the {name} sandbox backend is not available in this release")
    from .namespaces import NamespaceBackend

    backend = NamespaceBackend()
    status = backend.status()
    if not status.available:
        raise BackendUnavailable(f"the {name} sandbox backend cannot run here: {status.reason}")
    return backend


__all__ = [
    "Backend",
    "BackendStatus",
    "BuildOutcome",
    "BuildRequest",
    "OciConfig",
    "SandboxJob",
    "SandboxSpec",
    "backend_status",
    "make_spec",
    "oci_config",
    "run_build",
    "select_backend",
]
