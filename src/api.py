# SPDX-License-Identifier: GPL-2.0
"""The Lattice library interface (RFC 0001 section 6.1).

Lattice is the class a program creates to use the package manager from
Python. The ``lattice`` command is a thin wrapper around it, so anything
the command does, a program can do too. A Lattice object holds only its
settings and event handler; it keeps no state between calls and reads no
settings from the machine on its own.

Methods return immutable result models (models.py) and raise LatticeError
subclasses. Client (client.py) has the same methods and talks to the
daemon instead; both satisfy the LatticeAPI protocol. Each method states
the phase of the development plan that implements it, and raises
NotImplementedError until then.

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

import pybonsai
from pybonsai import FrozenMap, Host, PyBonsaiError

from .derivation import Derivation
from .errors import EvaluationFailed
from .events import EvaluationFinished, EvaluationStarted, EventHandler, emit
from .models import BuildResult, Evaluation, GCResult, Generation
from .sandbox import BackendStatus, OciConfig, make_spec, oci_config
from .sandbox import backend_status as probe_backends
from .sandbox.libc import machine
from .settings import Settings
from .stdlib import build_host


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
        emit(self.on_event, EvaluationStarted(filename=filename))
        start = time.perf_counter()
        try:
            names = pybonsai.evaluate_file(path, host=self._get_host())
        except PyBonsaiError as error:
            raise EvaluationFailed(error) from error
        return self._finish(filename, names, start)

    def evaluate_source(self, source: str, filename: str = "<string>") -> Evaluation:
        """Evaluate PyBonsai source text, as evaluate does for a file. Phase 1."""
        emit(self.on_event, EvaluationStarted(filename=filename))
        start = time.perf_counter()
        try:
            names = pybonsai.evaluate_source(source, filename, host=self._get_host())
        except PyBonsaiError as error:
            raise EvaluationFailed(error) from error
        return self._finish(filename, names, start)

    def _finish(self, filename: str, names: FrozenMap, start: float) -> Evaluation:
        derivations = {str(k): v for k, v in names.items() if isinstance(v, Derivation)}
        result = Evaluation(
            filename=filename, derivations=derivations, seconds=time.perf_counter() - start
        )
        emit(
            self.on_event,
            EvaluationFinished(
                filename=filename, derivations=len(derivations), seconds=result.seconds
            ),
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

    def sandbox_config(self, drv: Derivation) -> OciConfig:
        """Describe the sandbox that would build ``drv`` as an OCI runtime configuration.

        Uses the sandbox settings and this process's uid and gid; nothing
        runs. Raises BuildFailed if ``drv`` cannot be built in the sandbox
        and BackendUnavailable if the machine is not supported. Phase 2.
        """
        spec = make_spec(
            drv, self.settings.sandbox, uid=os.getuid(), gid=os.getgid(), machine=machine()
        )
        return oci_config(spec)

    def backend_status(self) -> tuple[BackendStatus, ...]:
        """Report which sandbox backends can run on this machine (section 9.7). Phase 2."""
        return probe_backends()

    def gc(self) -> GCResult:
        """Remove every store path not reachable from a root (section 12). Phase 3."""
        raise NotImplementedError

    def switch(self, profile: str, drv: Derivation) -> Generation:
        """Build ``drv`` and make it the current generation of ``profile``. Phase 4."""
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> None:
        """Sign and upload store paths and their provenance to ``cache``. Phase 5."""
        raise NotImplementedError
