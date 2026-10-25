# SPDX-License-Identifier: GPL-2.0
"""Lattice, the PyrixOS package manager.

Lattice evaluates PyBonsai files with the ``pybonsai`` package, passing
its own standard library as the host (RFC 0001 sections 7.6 and 8).

Programs use Lattice through the names exported here (RFC 0001 sections
6.1 and 6.2). Lattice runs operations in the calling process, Client sends
them to ``lattice daemon`` over its Unix socket or the remote API, and
Daemon is the daemon itself. Lattice and Client both satisfy LatticeAPI.
The ``lattice`` command is a thin wrapper around them::

    from lattice import Client, Lattice

    result = Lattice().evaluate("hello.pyb")
    result = Client().evaluate("hello.pyb")

Names in ``__all__`` follow semantic versioning from release 1.0. A name
is removed only after one minor release in which using it raises a
DeprecationWarning. Modules and names not listed here are internal.

Phase 1 provides derivations, the standard library and evaluation. Later
phases add the build sandbox, the store, the dependency database, the
garbage collector, profiles and binary caches as modules of this package.
"""

from __future__ import annotations

from .api import Lattice
from .client import DEFAULT_URL, Client, LatticeAPI
from .derivation import Derivation
from .errors import (
    AuthenticationFailed,
    BackendUnavailable,
    BuildFailed,
    EvaluationFailed,
    LatticeError,
    PermissionDenied,
    ProtocolError,
    SettingsError,
    StoreError,
    SubstitutionFailed,
)
from .events import EvaluationFinished, EvaluationStarted, Event, EventHandler
from .models import BuildResult, Evaluation, GCResult, Generation, Identity, Role
from .settings import (
    DEFAULT_SOCKET,
    SANDBOX_BACKENDS,
    ClientGrant,
    DaemonSettings,
    SandboxBackend,
    Settings,
    TokenGrant,
)
from .transport import Daemon

__version__ = "0.0.0"

__all__ = [
    "DEFAULT_SOCKET",
    "DEFAULT_URL",
    "SANDBOX_BACKENDS",
    "AuthenticationFailed",
    "BackendUnavailable",
    "BuildFailed",
    "BuildResult",
    "Client",
    "ClientGrant",
    "Daemon",
    "DaemonSettings",
    "Derivation",
    "Evaluation",
    "EvaluationFailed",
    "EvaluationFinished",
    "EvaluationStarted",
    "Event",
    "EventHandler",
    "GCResult",
    "Generation",
    "Identity",
    "Lattice",
    "LatticeAPI",
    "LatticeError",
    "PermissionDenied",
    "ProtocolError",
    "Role",
    "SandboxBackend",
    "Settings",
    "SettingsError",
    "StoreError",
    "SubstitutionFailed",
    "TokenGrant",
]
