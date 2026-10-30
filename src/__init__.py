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

Phase 1 provides derivations, the standard library and evaluation,
Phase 2 the build sandbox (``lattice.sandbox``, built on the Cloche
package), Phase 3 the store, the dependency database and the garbage
collector (``lattice.store``), and Phase 4 profiles, slots and kernel
modules (``lattice.profiles`` and ``lattice.kmod``). Phase 5 adds binary
caches.
"""

from __future__ import annotations

from .api import Lattice
from .client import DEFAULT_URL, Client, LatticeAPI
from .derivation import Derivation
from .errors import (
    ActivationFailed,
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
from .events import (
    ActivationDeferred,
    BuildFinished,
    BuildLog,
    BuildStarted,
    CacheSkipped,
    CollectionFinished,
    EvaluationFinished,
    EvaluationStarted,
    Event,
    EventHandler,
    KernelModuleLoaded,
    KernelModuleUnloaded,
    PathPushed,
    ProfileSwitched,
    SlotChanged,
    SubstitutionFinished,
    SubstitutionStarted,
)
from .kmod import KernelModules, LinuxModules, LoadedModule, ModuleInfo
from .models import (
    BuildResult,
    Evaluation,
    GCResult,
    Generation,
    Identity,
    PushResult,
    Role,
    SlotState,
    SubstitutionResult,
)
from .sandbox import BackendStatus, OciConfig
from .settings import (
    DEFAULT_SOCKET,
    SANDBOX_BACKENDS,
    CacheSettings,
    ClientGrant,
    DaemonSettings,
    MicroVMSettings,
    SandboxBackend,
    SandboxSettings,
    Settings,
    ShallowRules,
    StoreSettings,
    TokenGrant,
)
from .store import BuildPlan, GCRootInfo, PlannedStep, StorePathInfo, VerifyResult
from .transport import Daemon

__version__ = "0.10.0"

__all__ = [
    "ActivationDeferred",
    "ActivationFailed",
    "KernelModuleLoaded",
    "KernelModuleUnloaded",
    "KernelModules",
    "LinuxModules",
    "LoadedModule",
    "ModuleInfo",
    "ProfileSwitched",
    "SlotChanged",
    "SlotState",
    "VerifyResult",
    "StoreSettings",
    "StorePathInfo",
    "PlannedStep",
    "GCRootInfo",
    "CollectionFinished",
    "BuildStarted",
    "BuildPlan",
    "BuildLog",
    "BuildFinished",
    "DEFAULT_SOCKET",
    "DEFAULT_URL",
    "SANDBOX_BACKENDS",
    "AuthenticationFailed",
    "BackendStatus",
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
    "MicroVMSettings",
    "OciConfig",
    "PermissionDenied",
    "ProtocolError",
    "Role",
    "SandboxBackend",
    "SandboxSettings",
    "Settings",
    "SettingsError",
    "StoreError",
    "SubstitutionFailed",
    "TokenGrant",
    "CacheSettings",
    "CacheSkipped",
    "PathPushed",
    "PushResult",
    "ShallowRules",
    "SubstitutionFinished",
    "SubstitutionResult",
    "SubstitutionStarted",
]
