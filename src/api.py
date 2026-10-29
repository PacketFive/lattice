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
the phase of the development plan that implemented it.

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
from platform import machine
from typing import TYPE_CHECKING

import pybonsai
from pybonsai import FrozenMap, Host, PyBonsaiError

from . import profiles
from .derivation import BUILTIN_PROFILE, Derivation
from .errors import ActivationFailed, EvaluationFailed, StoreError
from .events import EvaluationFinished, EvaluationStarted, EventHandler, ProfileSwitched, emit
from .kmod import KernelModules, LinuxModules
from .models import (
    BuildResult,
    Evaluation,
    GCResult,
    Generation,
    PushResult,
    SlotState,
    SubstitutionResult,
)
from .sandbox import BackendStatus, OciConfig, make_spec, oci_config
from .sandbox import backend_status as probe_backends
from .settings import SandboxBackend, Settings
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
from .store.combine import profile_manifest

if TYPE_CHECKING:
    from .sandbox import Backend


class Lattice:
    """Entry point for using Lattice from Python.

    ``settings`` defaults to Settings(). ``on_event`` receives an Event for
    each step of an operation (see events.py). ``backend`` is the sandbox
    backend for builds; None chooses the one the settings name when the
    first build needs it. ``kernel_modules`` loads and unloads the kernel
    modules of stacks; None uses the running Linux kernel.

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
        kernel_modules: KernelModules | None = None,
    ) -> None:
        """Store the settings, event handler and backends. Nothing is read or created."""
        self.settings = settings if settings is not None else Settings()
        self.on_event = on_event
        self.backend = backend
        self.kernel_modules = kernel_modules if kernel_modules is not None else LinuxModules()
        self.layout = StoreLayout(root=self.settings.store.root, stacks=self.settings.store.stacks)
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

    def check(self, drv: Derivation, backend: SandboxBackend | None = None) -> BuildResult:
        """Rebuild the registered outputs of ``drv`` and compare them (section 16). Phase 3.

        ``backend`` rebuilds on that sandbox backend instead of the
        configured one. The rebuilt outputs are thrown away. Raises
        StoreError if ``drv`` is not built and BuildFailed, naming the
        files that differ, if the outputs are not the same.
        """
        return self._builder().check(drv, backend)

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

        Phase 3. A shallow path is read through its link, which fetches
        every file from CernVM-FS (section 14.6). Raises StoreError if
        ``path`` is not registered or its files are missing.
        """
        self.path_info(path)
        with lock.shared(self.layout):
            info = self.path_info(path)
            physical = self.layout.physical(path)
            if info.location == "shallow" and os.path.islink(physical):
                physical = os.readlink(physical)
            try:
                digest, size = content_hash(physical)
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
        return probe_backends(self.settings.sandbox, self.layout.store)

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
        """Build ``drv`` and make it the current generation of ``profile``. Phase 4.

        The new generation gets the next number and a GC root. For the
        ``system`` profile the slots then change to the stacks it assigns
        (section 13.3); if they cannot change now, an ActivationDeferred
        event says why and they change at the next boot. Raises
        ActivationFailed if a kernel module step fails, leaving the
        previous generation current, and if a profile other than
        ``system`` assigns stacks.
        """
        profiles.check_profile_name(profile)
        if profile != profiles.SYSTEM and drv.builder == BUILTIN_PROFILE:
            if profile_manifest(drv).stacks:
                raise ActivationFailed(f"only the {profiles.SYSTEM} profile may assign stacks")
        connection = database.connect(self.layout, create=True)
        try:
            # Shared from the build until the root exists, so collection cannot remove the output.
            with lock.shared(self.layout):
                path = self.build(drv).store_path
                with lock.profiles(self.layout):
                    profiles.prepare(self.layout, profile)
                    number = max(profiles.numbers(self.layout, profile), default=0) + 1
                    database.add_root(connection, profiles.root_name(profile, number), path)
                    profiles.replace_link(profiles.link_path(self.layout, profile, number), path)
                    return self._make_current(profile, number, path)
        finally:
            connection.close()

    def _make_current(self, profile: str, number: int, path: str) -> Generation:
        """Activate generation ``number`` if it is ``system``'s, then point the profile at it."""
        stacks = profiles.wanted(self.layout, path)
        if profile == profiles.SYSTEM:
            profiles.activate(self.layout, self.kernel_modules, stacks, self.on_event)
        elif stacks:
            raise ActivationFailed(f"only the {profiles.SYSTEM} profile may assign stacks")
        generation_link = profiles.link_path(self.layout, profile, number)
        profiles.replace_link(
            profiles.link_path(self.layout, profile), os.path.basename(generation_link)
        )
        emit(self.on_event, ProfileSwitched(profile=profile, number=number, store_path=path))
        return Generation(profile=profile, number=number, store_path=path, current=True)

    def rollback(self, profile: str, generation: int | None = None) -> Generation:
        """Make an existing generation of ``profile`` current again. Phase 4.

        ``generation`` defaults to the newest one older than the current
        generation. Activation is as for switch. Raises StoreError if the
        generation does not exist.
        """
        profiles.check_profile_name(profile)
        if not self.layout.exists():
            raise StoreError(f"no store at {self.layout.root}")
        with lock.profiles(self.layout):
            if generation is None:
                now = profiles.current(self.layout, profile)
                earlier = [
                    n for n in profiles.numbers(self.layout, profile) if now is None or n < now
                ]
                if not earlier:
                    raise StoreError(f"profile {profile!r} has no earlier generation")
                generation = earlier[-1]
            path = profiles.store_path(self.layout, profile, generation)
            return self._make_current(profile, generation, path)

    def generations(self, profile: str) -> tuple[Generation, ...]:
        """Return every generation of ``profile``, oldest first. Phase 4."""
        profiles.check_profile_name(profile)
        return profiles.generations(self.layout, profile)

    def delete_generations(self, profile: str, keep: int) -> tuple[Generation, ...]:
        """Delete all but the newest ``keep`` generations and the current one. Phase 4.

        Their roots are removed, so the next collection can reclaim what
        only they used. Returns the deleted generations.
        """
        profiles.check_profile_name(profile)
        if keep < 0:
            raise StoreError("keep must not be negative")
        if not self.layout.exists():
            return ()
        connection = self._connect()
        try:
            with lock.profiles(self.layout):
                existing = profiles.generations(self.layout, profile)
                kept = (
                    {g.number for g in existing[max(len(existing) - keep, 0) :]} if keep else set()
                )
                deleted = tuple(g for g in existing if g.number not in kept and not g.current)
                for generation in deleted:
                    os.unlink(profiles.link_path(self.layout, profile, generation.number))
                    try:
                        database.remove_root(
                            connection, profiles.root_name(profile, generation.number)
                        )
                    except StoreError:
                        pass
                return deleted
        finally:
            connection.close()

    def slots(self) -> tuple[SlotState, ...]:
        """Return every slot that is active or that the current system generation wants. Phase 4."""
        return profiles.states(self.layout)

    def activate_boot(self) -> tuple[SlotState, ...]:
        """Load the kernel modules and create the slots of the current system generation. Phase 4.

        Runs once at boot, before any service starts. Library and command
        only; the daemon does not offer it. Raises ActivationFailed after
        setting every slot if a module did not load.
        """
        if self.layout.exists():
            with lock.profiles(self.layout):
                wanted = profiles.wanted(self.layout, profiles.system_environment(self.layout))
                profiles.boot(self.layout, self.kernel_modules, wanted, self.on_event)
        else:
            profiles.boot(self.layout, self.kernel_modules, {}, self.on_event)
        return profiles.states(self.layout)

    def substitute(self, path: str, shallow: bool = False) -> SubstitutionResult:
        """Fetch ``path`` and its missing references from the caches (section 14.1). Phase 5.

        If ``shallow`` and the cache is a CernVM-FS repository, ``path``
        becomes a link into it; its references are full copies. Raises
        SubstitutionFailed if no trusted cache has a valid entry for a
        path or a fetched path does not match its signed entry.
        """
        from .cache.substitute import substitute

        check_store_path(path)
        return substitute(self.layout, self.settings, path, shallow=shallow, on_event=self.on_event)

    def materialise(self, path: str) -> StorePathInfo:
        """Turn the shallow path ``path`` into a full copy (section 14.6). Phase 5.

        A process that opens ``path`` while its link is replaced gets
        ENOENT, so materialise a stack while its slot is not active.
        Raises StoreError if ``path`` is not registered or its copy does
        not match the registered hash.
        """
        from .cache.substitute import materialise

        check_store_path(path)
        return materialise(self.layout, path)

    def push(self, paths: Iterable[str], cache: str) -> PushResult:
        """Sign and publish store paths, their closures and provenance to ``cache``. Phase 5.

        Paths the cache already has are skipped. Raises SettingsError if
        no secret key is set, StoreError if a path was not built on this
        machine or has changed since it was registered, and
        SubstitutionFailed if the cache cannot be written (section 14.4).
        """
        from .cache.push import push

        paths = tuple(paths)
        for path in paths:
            check_store_path(path)
        return push(self.layout, self.settings, paths, cache, on_event=self.on_event)
