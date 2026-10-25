# SPDX-License-Identifier: GPL-2.0
"""Lattice, the PyrixOS package manager.

Lattice evaluates PyBonsai files with the ``pybonsai`` package, passing
its own standard library as the host (RFC 0001 sections 7.6 and 8).

Programs use Lattice through the names exported here (RFC 0001 section
6.1). The ``lattice`` command is a thin wrapper around the same class::

    from lattice import Lattice

    result = Lattice().evaluate("hello.pyb")

Names in ``__all__`` follow semantic versioning from release 1.0. A name
is removed only after one minor release in which using it raises a
DeprecationWarning. Modules and names not listed here are internal.

Phase 1 provides derivations, the standard library and evaluation. Later
phases add the build sandbox, the store, the dependency database, the
garbage collector, profiles and binary caches as modules of this package.
"""

from __future__ import annotations

from .api import BuildResult, Evaluation, GCResult, Generation, Lattice
from .derivation import Derivation
from .errors import (
    BackendUnavailable,
    BuildFailed,
    EvaluationFailed,
    LatticeError,
    SettingsError,
    StoreError,
    SubstitutionFailed,
)
from .events import EvaluationFinished, EvaluationStarted, Event, EventHandler
from .settings import SANDBOX_BACKENDS, SandboxBackend, Settings

__version__ = "0.0.0"

__all__ = [
    "SANDBOX_BACKENDS",
    "BackendUnavailable",
    "BuildFailed",
    "BuildResult",
    "Derivation",
    "Evaluation",
    "EvaluationFailed",
    "EvaluationFinished",
    "EvaluationStarted",
    "Event",
    "EventHandler",
    "GCResult",
    "Generation",
    "Lattice",
    "LatticeError",
    "SandboxBackend",
    "Settings",
    "SettingsError",
    "StoreError",
    "SubstitutionFailed",
]
