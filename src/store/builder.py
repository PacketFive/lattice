# SPDX-License-Identifier: GPL-2.0
"""Realising derivations in dependency order (RFC 0001 sections 10.1 and 10.2).

plan lists the derivations a build needs, inputs first, and says for
each one whether its outputs are already registered, will be fetched
from a binary cache, will be downloaded or will be built in the sandbox.
The plan is made from the requested derivation down: the inputs of a
derivation that comes from a cache are not needed and are left out
(section 14.1). Builder.build carries out a plan with
up to ``max_jobs`` steps at once; a step starts when every derivation it
names in ``env`` has finished. The first failure stops new steps from
starting, waits for the running ones and is raised.

The whole build holds the store lock shared, so collection cannot remove
an input while a step uses it, and each step holds the lock of its
derivation, so two processes never build the same derivation at once.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ..derivation import (
    BUILTIN_FETCH_URL,
    BUILTIN_PROFILE,
    BUILTIN_STACK,
    STORE_DIR,
    Derivation,
)
from ..errors import BuildFailed, StoreError
from ..events import BuildFinished, BuildLog, BuildStarted, Event, EventHandler, emit
from ..models import BuildResult, Model
from ..sandbox import BuildRequest, guest_files, host_path, run_build, select_backend
from ..settings import SandboxBackend, Settings, ShallowRules
from . import database, lock
from .combine import combine
from .fetch import fetch
from .layout import StoreLayout
from .realise import realise, remove_tree, scan
from .verify import diff_trees

if TYPE_CHECKING:
    from ..cache.substitute import Substituter
    from ..sandbox import Backend

Action = Literal["present", "substitute", "fetch", "build"]


class PlannedStep(Model):
    """One derivation of a plan, its output paths and what will happen to it."""

    name: str
    drv_hash: str
    outputs: dict[str, str]
    action: Action


class BuildPlan(Model):
    """The steps a build takes, each after the derivations it depends on."""

    steps: tuple[PlannedStep, ...]


def dependencies(drv: Derivation) -> tuple[Derivation, ...]:
    """Return the derivations ``drv`` names in ``env``, without repeats."""
    found: dict[str, Derivation] = {}
    for value in drv.env.values():
        if isinstance(value, Derivation):
            found.setdefault(value.hash, value)
    return tuple(found.values())


def ordered(drv: Derivation) -> tuple[Derivation, ...]:
    """Return ``drv`` and every derivation it depends on, each after its dependencies."""
    order: list[Derivation] = []
    seen: set[str] = set()
    stack: list[tuple[Derivation, bool]] = [(drv, False)]
    while stack:
        current, expanded = stack.pop()
        if expanded:
            order.append(current)
            continue
        if current.hash in seen:
            continue
        seen.add(current.hash)
        stack.append((current, True))
        stack.extend(
            (dep, False) for dep in reversed(dependencies(current)) if dep.hash not in seen
        )
    return tuple(order)


def direct_inputs(drv: Derivation) -> tuple[str, ...]:
    """Return every output path of every derivation ``drv`` names in ``env``."""
    return tuple(path for dep in dependencies(drv) for path in dep.output_paths.values())


def _action(drv: Derivation, present: bool) -> Action:
    if present:
        return "present"
    return "fetch" if drv.builder == BUILTIN_FETCH_URL else "build"


def shallow_paths(steps: tuple[Derivation, ...], rules: ShallowRules) -> frozenset[str]:
    """Return the output paths of ``steps`` that may be shallow paths (section 14.6).

    A derivation whose name matches a pattern in ``rules.packages`` may
    be shallow, and so may a stack for a slot in ``rules.slots`` with its
    contents. Kernel modules named by a stack never are.
    """
    allowed: set[str] = set()
    local: set[str] = set()
    for drv in steps:
        if any(fnmatch.fnmatchcase(drv.name, pattern) for pattern in rules.packages):
            allowed.update(drv.output_paths.values())
        if drv.builder != BUILTIN_STACK:
            continue
        shallow_slot = drv.env.get("slot") in rules.slots
        if shallow_slot:
            allowed.update(drv.output_paths.values())
        for key, value in drv.env.items():
            if not isinstance(value, Derivation):
                continue
            if str(key).startswith("kernel_module_"):
                local.update(value.output_paths.values())
            elif shallow_slot and str(key).startswith("content_"):
                allowed.update(value.output_paths.values())
    return frozenset(allowed - local)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Builder:
    """Builds derivations into the store of ``layout`` (section 10).

    ``backend`` is the sandbox backend; None chooses the one named in the
    settings when the first build step runs.
    """

    def __init__(
        self,
        layout: StoreLayout,
        settings: Settings,
        *,
        backend: Backend | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        """Store the layout, settings, backend and event handler."""
        self.layout = layout
        self.settings = settings
        self.backend = backend
        self.on_event = on_event
        self._emit_lock = threading.Lock()
        self._substituter: Substituter | None = None
        self._shallow: frozenset[str] = frozenset()

    def _emit(self, event: Event) -> None:
        with self._emit_lock:
            emit(self.on_event, event)

    def substituter(self) -> Substituter | None:
        """Return the Substituter of the configured caches, or None if there are none."""
        if self._substituter is None and self.settings.substituters:
            from ..cache.substitute import Substituter

            self._substituter = Substituter(self.layout, self.settings, on_event=self._emit)
        return self._substituter

    def _available(self, paths: set[str]) -> bool:
        substituter = self.substituter()
        return substituter is not None and all(substituter.query(p) is not None for p in paths)

    def _schedule(self, drv: Derivation) -> tuple[tuple[Derivation, ...], dict[str, Action]]:
        """Return the steps building ``drv`` needs, inputs first, and the action of each."""
        steps = ordered(drv)
        present: frozenset[str] = frozenset()
        if self.layout.exists():
            connection = database.connect(self.layout)
            try:
                present = database.registered(
                    connection, (p for step in steps for p in step.output_paths.values())
                )
            finally:
                connection.close()
        needed = {drv.hash}
        actions: dict[str, Action] = {}
        for step in reversed(steps):
            if step.hash not in needed:
                continue
            outputs = set(step.output_paths.values())
            if outputs <= present:
                actions[step.hash] = "present"
            elif self._available(outputs - present):
                actions[step.hash] = "substitute"
                continue
            else:
                actions[step.hash] = _action(step, False)
            needed.update(dep.hash for dep in dependencies(step))
        return tuple(step for step in steps if step.hash in needed), actions

    def _backend(self) -> Backend:
        if self.backend is None:
            self.backend = select_backend(
                self.settings.sandbox_backend, self.settings.sandbox, self.layout.store
            )
        return self.backend

    def plan(self, drv: Derivation) -> BuildPlan:
        """List the steps that building ``drv`` takes. Nothing is created.

        Without a store every step is fetched from a cache, downloaded or
        built. The configured caches are asked which paths they have.
        """
        steps, actions = self._schedule(drv)
        return BuildPlan(
            steps=tuple(
                PlannedStep(
                    name=step.name,
                    drv_hash=step.hash,
                    outputs=step.output_paths,
                    action=actions[step.hash],
                )
                for step in steps
            )
        )

    def build(self, drv: Derivation) -> BuildResult:
        """Realise ``drv`` and everything it depends on, then return its result.

        Raises BuildFailed if a step fails, BackendUnavailable if the
        sandbox cannot run here, SubstitutionFailed if a path the plan
        takes from a cache cannot be fetched and StoreError if the store
        is broken.
        """
        started = time.monotonic()
        database.connect(self.layout, create=True).close()
        built: dict[str, bool] = {}
        with lock.shared(self.layout):
            steps, actions = self._schedule(drv)
            self._shallow = shallow_paths(ordered(drv), self.settings.cache.shallow)
            self._run(steps, built, actions)
        return BuildResult(
            derivation=drv,
            store_path=drv.store_path,
            outputs=drv.output_paths,
            built=built[drv.hash],
            seconds=time.monotonic() - started,
        )

    def _run(
        self,
        steps: tuple[Derivation, ...],
        built: dict[str, bool],
        actions: dict[str, Action] | None = None,
    ) -> None:
        waiting = list(steps)
        scheduled = {step.hash for step in steps}
        actions = actions or {}
        running: dict[Future[bool], Derivation] = {}
        failure: BaseException | None = None
        with ThreadPoolExecutor(
            max_workers=self.settings.store.max_jobs, thread_name_prefix="lattice-build"
        ) as pool:
            while waiting or running:
                if failure is None:
                    for step in [s for s in waiting if _ready(s, built, scheduled)]:
                        waiting.remove(step)
                        action = actions.get(step.hash)
                        running[pool.submit(self._step, step, action)] = step
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    step = running.pop(future)
                    error = future.exception()
                    if error is None:
                        built[step.hash] = future.result()
                    elif failure is None:
                        failure = error
        if failure is not None:
            raise failure

    def _step(self, drv: Derivation, action: Action | None = None) -> bool:
        """Realise the outputs of ``drv``; return False unless they were built here."""
        with lock.derivation(self.layout, drv.hash):
            connection = database.connect(self.layout)
            try:
                if database.registered(connection, drv.output_paths.values()) == set(
                    drv.output_paths.values()
                ):
                    return False
                if action == "substitute":
                    substituter = self.substituter()
                    assert substituter is not None  # noqa: S101 - the plan used it
                    for path in drv.output_paths.values():
                        substituter.substitute(
                            connection, path, shallow=self._shallow, held=(drv.hash,)
                        )
                    return False
                inputs = self._inputs(connection, drv)
                started = time.monotonic()
                started_at = _timestamp()
                self._emit(BuildStarted(name=drv.name, store_path=drv.store_path))
                with self._work_dir(drv) as work:
                    outputs, boot = self._produce(drv, work, inputs)
                    entries = realise(self.layout, connection, drv, outputs, inputs)
                self._record(connection, drv, entries, inputs, started_at)
                self._emit(
                    BuildFinished(
                        name=drv.name,
                        store_path=drv.store_path,
                        built=True,
                        seconds=time.monotonic() - started,
                        boot_seconds=boot,
                    )
                )
                return True
            finally:
                connection.close()

    def _record(
        self,
        connection: sqlite3.Connection,
        drv: Derivation,
        entries: tuple[database.Registration, ...],
        inputs: tuple[str, ...],
        started_at: str,
    ) -> None:
        """Record how the outputs of ``drv`` were built, for their provenance (section 14.9).

        A build on the microVM backend also depends on the guest kernel and
        init, which are recorded as inputs: the store path that holds each,
        or the file itself with the SHA-256 of its bytes if it is not in
        the store.
        """
        backend = "builtin" if drv.builder.startswith("builtin:") else self._backend().name
        hashes = []
        for path in inputs:
            info = database.path_info(connection, path)
            if info is not None:
                hashes.append((path, info.content_sha256))
        if backend == "microvm":
            hashes.extend(self._guest_inputs(connection))
        record = database.BuildRecord(
            backend=backend,
            started_at=started_at,
            finished_at=_timestamp(),
            inputs=tuple(hashes),
        )
        for entry in entries:
            database.record_build(connection, entry.path, record)

    def _guest_inputs(self, connection: sqlite3.Connection) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for path in guest_files(self.settings.sandbox):
            if path.startswith(STORE_DIR + "/"):
                store_path = "/".join(path.split("/")[:4])
                info = database.path_info(connection, store_path)
                if info is not None:
                    found.append((store_path, info.content_sha256))
                    continue
            digest = hashlib.sha256()
            with open(host_path(path, self.layout.store), "rb") as file:
                while chunk := file.read(1 << 20):
                    digest.update(chunk)
            found.append((path, digest.hexdigest()))
        return found

    def _inputs(self, connection: sqlite3.Connection, drv: Derivation) -> tuple[str, ...]:
        direct = direct_inputs(drv)
        found = database.closure(connection, direct)
        missing = sorted(set(direct) - found)
        if missing:
            raise StoreError(f"{drv.name}: inputs {', '.join(missing)} are not registered")
        return tuple(sorted(found))

    def _work_dir(self, drv: Derivation) -> _WorkDir:
        return _WorkDir(self.layout.staging, drv.hash[:32])

    def _produce(
        self,
        drv: Derivation,
        work: str,
        inputs: tuple[str, ...],
        backend: Backend | None = None,
    ) -> tuple[dict[str, str], float | None]:
        """Download or build ``drv`` in ``work``.

        Returns the path of each output and the boot time of the virtual
        machine, or None. ``backend`` overrides the configured backend.
        """
        if drv.builder == BUILTIN_FETCH_URL:
            if drv.outputs != ("out",):
                raise BuildFailed(f"{drv.name}: fetch_url has the single output out")
            destination = os.path.join(work, "out")
            fetch(drv, destination)
            return {"out": destination}, None
        if drv.builder in (BUILTIN_STACK, BUILTIN_PROFILE):
            return combine(self.layout, drv, os.path.join(work, "out")), None
        name = drv.name

        def on_log(text: str) -> None:
            self._emit(BuildLog(name=name, text=text))

        outcome = run_build(
            BuildRequest(
                derivation=drv,
                work_dir=work,
                host_store=self.layout.store,
                sandbox=self.settings.sandbox,
                inputs=inputs,
                sources={
                    path: os.readlink(physical)
                    for path in inputs
                    if os.path.islink(physical := self.layout.physical(path))
                },
            ),
            backend or self._backend(),
            on_log,
        )
        return outcome.outputs, outcome.boot_seconds

    def check(self, drv: Derivation, backend: SandboxBackend | None = None) -> BuildResult:
        """Build ``drv`` again and compare the result with its registered outputs (section 16).

        Its inputs are built first if needed. The new outputs are thrown
        away, never registered. ``backend`` rebuilds ``drv`` on that
        backend instead of the configured one; its inputs are built on
        the configured one. Raises StoreError if ``drv`` is not
        registered and BuildFailed, naming the files that differ, if the
        outputs are not the same.
        """
        chosen: Backend | None = None
        if backend is not None:
            chosen = select_backend(backend, self.settings.sandbox, self.layout.store)
        started = time.monotonic()
        database.connect(self.layout, create=True).close()
        built: dict[str, bool] = {}
        with lock.shared(self.layout):
            self._run(ordered(drv)[:-1], built)
            connection = database.connect(self.layout)
            try:
                recorded = {
                    name: database.path_info(connection, path)
                    for name, path in drv.output_paths.items()
                }
                unregistered = sorted(n for n, info in recorded.items() if info is None)
                if unregistered:
                    raise StoreError(f"{drv.name}: output {', '.join(unregistered)} is not built")
                inputs = self._inputs(connection, drv)
            finally:
                connection.close()
            with self._work_dir(drv) as work:
                outputs, _ = self._produce(drv, work, inputs, chosen)
                entries = scan(drv, outputs, inputs)
                differences = [
                    f"{name}: {item}"
                    for name, entry in entries.items()
                    if (info := recorded[name]) is not None
                    and entry.content_sha256 != info.content_sha256
                    for item in diff_trees(self.layout.physical(entry.path), outputs[name])
                ]
        if differences:
            listed = "\n".join(differences)
            raise BuildFailed(f"{drv.name}: the rebuilt outputs differ:\n{listed}")
        return BuildResult(
            derivation=drv,
            store_path=drv.store_path,
            outputs=drv.output_paths,
            built=True,
            seconds=time.monotonic() - started,
        )


def _ready(drv: Derivation, finished: dict[str, bool], scheduled: set[str]) -> bool:
    return all(dep.hash in finished for dep in dependencies(drv) if dep.hash in scheduled)


class _WorkDir:
    """A new private directory in staging, removed with its contents on exit."""

    def __init__(self, staging: str, prefix: str) -> None:
        self.path = tempfile.mkdtemp(prefix=f"{prefix}-", dir=staging)

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *exc: object) -> None:
        remove_tree(self.path)
