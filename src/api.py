# SPDX-License-Identifier: GPL-2.0
"""The Lattice library interface (RFC 0001 section 6.1).

Lattice is the class a program creates to use the package manager from
Python. The ``lattice`` command is a thin wrapper around it, so anything
the command does, a program can do too. A Lattice object holds only its
settings and event handler; it keeps no state between calls and reads no
settings from the machine on its own.

Methods return immutable result objects and raise LatticeError
subclasses. Each method states the phase of the development plan that
implements it, and raises NotImplementedError until then.

Example::

    from lattice import Lattice, Settings

    lattice = Lattice(Settings(sandbox_backend="namespaces"))
    result = lattice.evaluate("hello.pyb")
    for name, drv in result.derivations.items():
        print(name, drv.store_path)
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass

import pybonsai
from pybonsai import FrozenMap, Host, PyBonsaiError

from .derivation import Derivation
from .errors import EvaluationFailed
from .events import EvaluationFinished, EvaluationStarted, EventHandler, emit
from .settings import Settings
from .stdlib import build_host


@dataclass(frozen=True)
class Evaluation:
    """The result of evaluating one PyBonsai file.

    ``names`` holds every top-level name the file assigns, in assignment
    order. ``seconds`` is the evaluation time reported by section 15.
    """

    filename: str
    names: FrozenMap
    seconds: float

    @property
    def derivations(self) -> dict[str, Derivation]:
        """Return the names bound to a Derivation, in assignment order."""
        return {str(k): v for k, v in self.names.items() if isinstance(v, Derivation)}


@dataclass(frozen=True)
class BuildResult:
    """The outcome of realising one derivation.

    ``built`` is False when the path was already in the store or was
    substituted from a cache instead of being built.
    """

    derivation: Derivation
    store_path: str
    built: bool
    seconds: float


@dataclass(frozen=True)
class GCResult:
    """The store paths one collection removed and the bytes it freed."""

    removed: tuple[str, ...]
    freed_bytes: int
    seconds: float


@dataclass(frozen=True)
class Generation:
    """One generation of a profile (section 13.1)."""

    profile: str
    number: int
    store_path: str


class Lattice:
    """Entry point for using Lattice from Python.

    ``settings`` defaults to Settings(). ``on_event`` receives an Event for
    each step of an operation (see events.py).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        on_event: EventHandler | None = None,
    ) -> None:
        """Store the settings and event handler. Nothing is read or created."""
        self.settings = settings if settings is not None else Settings()
        self.on_event = on_event
        self._host: Host | None = None

    def _get_host(self) -> Host:
        if self._host is None:
            self._host = build_host()
        return self._host

    def evaluate(self, path: str | os.PathLike[str]) -> Evaluation:
        """Evaluate the PyBonsai file at ``path`` with the standard library. Phase 1.

        Nothing is built. Raises EvaluationFailed if the file is rejected or
        fails, and OSError if it cannot be read.
        """
        filename = os.fspath(path)
        emit(self.on_event, EvaluationStarted(filename))
        start = time.perf_counter()
        try:
            names = pybonsai.evaluate_file(path, host=self._get_host())
        except PyBonsaiError as error:
            raise EvaluationFailed(error) from error
        return self._finish(filename, names, start)

    def evaluate_source(self, source: str, filename: str = "<string>") -> Evaluation:
        """Evaluate PyBonsai source text, as evaluate does for a file. Phase 1."""
        emit(self.on_event, EvaluationStarted(filename))
        start = time.perf_counter()
        try:
            names = pybonsai.evaluate_source(source, filename, host=self._get_host())
        except PyBonsaiError as error:
            raise EvaluationFailed(error) from error
        return self._finish(filename, names, start)

    def _finish(self, filename: str, names: FrozenMap, start: float) -> Evaluation:
        result = Evaluation(filename, names, time.perf_counter() - start)
        emit(
            self.on_event,
            EvaluationFinished(filename, len(result.derivations), result.seconds),
        )
        return result

    def build(self, drv: Derivation) -> BuildResult:
        """Realise ``drv`` and its inputs and register the outputs. Phases 2 and 3.

        Paths already in the store are reused, and from Phase 5 paths in a
        trusted cache are substituted. Raises BuildFailed,
        BackendUnavailable or StoreError.
        """
        raise NotImplementedError

    def check(self, drv: Derivation) -> BuildResult:
        """Rebuild the registered output of ``drv`` and compare it (section 16). Phase 3.

        Raises BuildFailed if the new output differs from the stored one.
        """
        raise NotImplementedError

    def gc(self) -> GCResult:
        """Remove every store path not reachable from a root (section 12). Phase 3."""
        raise NotImplementedError

    def switch(self, profile: str, drv: Derivation) -> Generation:
        """Build ``drv`` and make it the current generation of ``profile``. Phase 4."""
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> None:
        """Sign and upload store paths and their provenance to ``cache``. Phase 5."""
        raise NotImplementedError
