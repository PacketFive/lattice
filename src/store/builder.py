# SPDX-License-Identifier: GPL-2.0
"""Realising derivations in dependency order (RFC 0001 sections 10.1 and 10.2).

plan lists the derivations a build needs, inputs first, and says for
each one whether its outputs are already registered, will be downloaded
or will be built in the sandbox. Builder.build carries out a plan with
up to ``max_jobs`` steps at once; a step starts when every derivation it
names in ``env`` has finished. The first failure stops new steps from
starting, waits for the running ones and is raised.

The whole build holds the store lock shared, so collection cannot remove
an input while a step uses it, and each step holds the lock of its
derivation, so two processes never build the same derivation at once.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Literal

from ..derivation import BUILTIN_FETCH_URL, Derivation
from ..errors import BuildFailed, StoreError
from ..events import BuildFinished, BuildLog, BuildStarted, EventHandler, emit
from ..models import BuildResult, Model
from ..sandbox.runner import BuildRequest, run_build
from ..settings import Settings
from . import database, lock
from .fetch import fetch
from .layout import StoreLayout
from .realise import realise, remove_tree, scan
from .verify import diff_trees

if TYPE_CHECKING:
    from ..sandbox import Backend

Action = Literal["present", "fetch", "build"]


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

    def _emit(self, event: BuildStarted | BuildLog | BuildFinished) -> None:
        with self._emit_lock:
            emit(self.on_event, event)

    def _backend(self) -> Backend:
        if self.backend is None:
            from ..sandbox import select_backend

            self.backend = select_backend(self.settings.sandbox_backend)
        return self.backend

    def plan(self, drv: Derivation) -> BuildPlan:
        """List the steps that building ``drv`` takes. Nothing is created.

        Without a store every step is a download or a build.
        """
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
        return BuildPlan(
            steps=tuple(
                PlannedStep(
                    name=step.name,
                    drv_hash=step.hash,
                    outputs=step.output_paths,
                    action=_action(step, set(step.output_paths.values()) <= present),
                )
                for step in steps
            )
        )

    def build(self, drv: Derivation) -> BuildResult:
        """Realise ``drv`` and everything it depends on, then return its result.

        Raises BuildFailed if a step fails, BackendUnavailable if the
        sandbox cannot run here and StoreError if the store is broken.
        """
        started = time.monotonic()
        database.connect(self.layout, create=True).close()
        built: dict[str, bool] = {}
        with lock.shared(self.layout):
            self._run(ordered(drv), built)
        return BuildResult(
            derivation=drv,
            store_path=drv.store_path,
            outputs=drv.output_paths,
            built=built[drv.hash],
            seconds=time.monotonic() - started,
        )

    def _run(self, steps: tuple[Derivation, ...], built: dict[str, bool]) -> None:
        waiting = list(steps)
        running: dict[Future[bool], Derivation] = {}
        failure: BaseException | None = None
        with ThreadPoolExecutor(
            max_workers=self.settings.store.max_jobs, thread_name_prefix="lattice-build"
        ) as pool:
            while waiting or running:
                if failure is None:
                    for step in [s for s in waiting if _ready(s, built)]:
                        waiting.remove(step)
                        running[pool.submit(self._step, step)] = step
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

    def _step(self, drv: Derivation) -> bool:
        """Realise the outputs of ``drv``; return False if they were already registered."""
        with lock.derivation(self.layout, drv.hash):
            connection = database.connect(self.layout)
            try:
                if database.registered(connection, drv.output_paths.values()) == set(
                    drv.output_paths.values()
                ):
                    return False
                inputs = self._inputs(connection, drv)
                started = time.monotonic()
                self._emit(BuildStarted(name=drv.name, store_path=drv.store_path))
                with self._work_dir(drv) as work:
                    outputs = self._produce(drv, work, inputs)
                    realise(self.layout, connection, drv, outputs, inputs)
                self._emit(
                    BuildFinished(
                        name=drv.name,
                        store_path=drv.store_path,
                        built=True,
                        seconds=time.monotonic() - started,
                    )
                )
                return True
            finally:
                connection.close()

    def _inputs(self, connection: sqlite3.Connection, drv: Derivation) -> tuple[str, ...]:
        direct = direct_inputs(drv)
        found = database.closure(connection, direct)
        missing = sorted(set(direct) - found)
        if missing:
            raise StoreError(f"{drv.name}: inputs {', '.join(missing)} are not registered")
        return tuple(sorted(found))

    def _work_dir(self, drv: Derivation) -> _WorkDir:
        return _WorkDir(self.layout.staging, drv.hash[:32])

    def _produce(self, drv: Derivation, work: str, inputs: tuple[str, ...]) -> dict[str, str]:
        """Download or build ``drv`` in ``work`` and return the path of each output."""
        if drv.builder == BUILTIN_FETCH_URL:
            if drv.outputs != ("out",):
                raise BuildFailed(f"{drv.name}: fetch_url has the single output out")
            destination = os.path.join(work, "out")
            fetch(drv, destination)
            return {"out": destination}
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
            ),
            self._backend(),
            on_log,
        )
        return outcome.outputs

    def check(self, drv: Derivation) -> BuildResult:
        """Build ``drv`` again and compare the result with its registered outputs (section 16).

        Its inputs are built first if needed. The new outputs are thrown
        away, never registered. Raises StoreError if ``drv`` is not
        registered and BuildFailed, naming the files that differ, if the
        outputs are not the same.
        """
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
                outputs = self._produce(drv, work, inputs)
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


def _ready(drv: Derivation, finished: dict[str, bool]) -> bool:
    return all(dep.hash in finished for dep in dependencies(drv))


class _WorkDir:
    """A new private directory in staging, removed with its contents on exit."""

    def __init__(self, staging: str, prefix: str) -> None:
        self.path = tempfile.mkdtemp(prefix=f"{prefix}-", dir=staging)

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *exc: object) -> None:
        remove_tree(self.path)
