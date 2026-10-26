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
import sqlite3
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

import pybonsai
from pybonsai import FrozenMap, Host, PyBonsaiError

from .derivation import Derivation
from .errors import EvaluationFailed, StoreError
from .events import EvaluationFinished, EvaluationStarted, EventHandler, emit
from .models import BuildResult, Evaluation, GCResult, Generation
from .sandbox import BackendStatus, OciConfig, make_spec, oci_config
from .sandbox import backend_status as probe_backends
from .sandbox.libc import machine
from .settings import Settings
from .stdlib import build_host
from .store import (
    Builder,
    BuildPlan,
    GCRootInfo,
    StoreLayout,
    StorePathInfo,
    VerifyResult,
    check_store_path,
    collect,
    content_hash,
    database,
    lock,
)
from .store.builder import direct_inputs

if TYPE_CHECKING:
    from .sandbox import Backend


class Lattice:
    """Entry point for using Lattice from Python.

    ``settings`` defaults to Settings(). ``on_event`` receives an Event for
    each step of an operation (see events.py). ``backend`` is the sandbox
    backend for builds; None chooses the one the settings name when the
    first build needs it.

    The store under ``settings.store.root`` is created by the first
    operation that writes to it. Operations that only read it raise
    StoreError while it does not exist.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        on_event: EventHandler | None = None,
        backend: Backend | None = None,
    ) -> None:
        """Store the settings, event handler and backend. Nothing is read or created."""
        self.settings = settings if settings is not None else Settings()
        self.on_event = on_event
        self.backend = backend
        self.layout = StoreLayout(root=self.settings.store.root)
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

    def _builder(self) -> Builder:
        return Builder(self.layout, self.settings, backend=self.backend, on_event=self.on_event)

    def build(self, drv: Derivation) -> BuildResult:
        """Realise ``drv`` and its inputs and register the outputs. Phase 3.

        Outputs already registered are reused, and from Phase 5 paths in
        a trusted cache are substituted. The outputs are not made roots,
        so the next collection removes them unless add_root keeps them.
        Raises BuildFailed, BackendUnavailable or StoreError.
        """
        return self._builder().build(drv)

    def check(self, drv: Derivation) -> BuildResult:
        """Rebuild the registered outputs of ``drv`` and compare them (section 16). Phase 3.

        The rebuilt outputs are thrown away. Raises StoreError if ``drv`` is
        not built and BuildFailed, naming the files that differ, if the
        outputs are not the same.
        """
        return self._builder().check(drv)

    def plan(self, drv: Derivation) -> BuildPlan:
        """List what building ``drv`` would download, build or reuse. Phase 3."""
        return self._builder().plan(drv)

    def _connect(self) -> sqlite3.Connection:
        return database.connect(self.layout)

    def path_info(self, path: str) -> StorePathInfo:
        """Return what the store database records about ``path``. Phase 3.

        Raises StoreError if ``path`` is not a registered store path.
        """
        check_store_path(path)
        connection = self._connect()
        try:
            info = database.path_info(connection, path)
        finally:
            connection.close()
        if info is None:
            raise StoreError(f"{path} is not registered")
        return info

    def verify(self, path: str) -> VerifyResult:
        """Hash the contents of ``path`` again and compare with the database (section 11).

        Phase 3. Raises StoreError if ``path`` is not registered or its
        files are missing.
        """
        self.path_info(path)
        with lock.shared(self.layout):
            info = self.path_info(path)
            try:
                digest, size = content_hash(self.layout.physical(path))
            except FileNotFoundError:
                raise StoreError(f"{path} is registered but its files are missing") from None
        return VerifyResult(
            path=path,
            ok=digest == info.content_sha256 and size == info.size,
            expected_sha256=info.content_sha256,
            actual_sha256=digest,
            expected_size=info.size,
            actual_size=size,
        )

    def sandbox_config(self, drv: Derivation) -> OciConfig:
        """Describe the sandbox that would build ``drv`` as an OCI runtime configuration.

        Uses the sandbox settings and this process's uid and gid; nothing
        runs. When the inputs of ``drv`` are registered, the sandbox has
        them and the paths they refer to, as a build would; otherwise it
        has the outputs of every derivation ``drv`` depends on. Raises
        BuildFailed if ``drv`` cannot be built in the sandbox and
        BackendUnavailable if the machine is not supported. Phase 2.
        """
        spec = make_spec(
            drv,
            self.settings.sandbox,
            uid=os.getuid(),
            gid=os.getgid(),
            machine=machine(),
            inputs=self._registered_inputs(drv),
        )
        return oci_config(spec)

    def _registered_inputs(self, drv: Derivation) -> tuple[str, ...] | None:
        if not self.layout.exists():
            return None
        direct = direct_inputs(drv)
        connection = self._connect()
        try:
            found = database.closure(connection, direct)
        finally:
            connection.close()
        return tuple(sorted(found)) if set(direct) <= found else None

    def backend_status(self) -> tuple[BackendStatus, ...]:
        """Report which sandbox backends can run on this machine (section 9.7). Phase 2."""
        return probe_backends()

    def gc(self) -> GCResult:
        """Remove every store path no root or running process keeps (section 12). Phase 3."""
        return collect(self.layout, self.on_event)

    def add_root(self, name: str, path: str) -> GCRootInfo:
        """Keep ``path`` and everything it refers to under the root ``name``. Phase 3.

        A root of the same name is replaced. Raises StoreError if the name
        is not valid or ``path`` is not registered.
        """
        check_store_path(path)
        connection = database.connect(self.layout, create=True)
        try:
            # Shared, so that a collection that has already marked path dead cannot be running.
            with lock.shared(self.layout):
                return database.add_root(connection, name, path)
        finally:
            connection.close()

    def remove_root(self, name: str) -> GCRootInfo:
        """Remove the root ``name`` and return it. Phase 3.

        Raises StoreError if there is no such root.
        """
        connection = self._connect()
        try:
            return database.remove_root(connection, name)
        finally:
            connection.close()

    def roots(self) -> tuple[GCRootInfo, ...]:
        """Return every root, sorted by name. Phase 3."""
        if not self.layout.exists():
            return ()
        connection = self._connect()
        try:
            return database.roots(connection)
        finally:
            connection.close()

    def switch(self, profile: str, drv: Derivation) -> Generation:
        """Build ``drv`` and make it the current generation of ``profile``. Phase 4."""
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> None:
        """Sign and upload store paths and their provenance to ``cache``. Phase 5."""
        raise NotImplementedError
