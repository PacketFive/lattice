# SPDX-License-Identifier: GPL-2.0
"""Shared fixtures: stand-ins for the daemon backend and the sandbox.

FakeAPI satisfies LatticeAPI without evaluating or building anything, so
the daemon, protocol and client are tested apart from the operations
behind them. StoreBackend is a sandbox backend that runs a small Python
program instead of the sandbox helper, so the store is tested on any
machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import pytest

from lattice import (
    BuildResult,
    Derivation,
    Evaluation,
    EvaluationFailed,
    EvaluationStarted,
    EventHandler,
    GCResult,
    Generation,
    LatticeAPI,
    SandboxSettings,
)
from lattice.sandbox import BackendStatus, OciConfig, SandboxJob, make_spec, oci_config
from lattice.store import BuildPlan, GCRootInfo, StorePathInfo, VerifyResult

CREATED = datetime(2025, 1, 1, tzinfo=UTC)


class FakeAPI:
    def __init__(self, on_event: EventHandler | None) -> None:
        self.on_event = on_event

    def evaluate(self, path: str | os.PathLike[str]) -> Evaluation:
        raise AssertionError("the daemon never evaluates a path")

    def evaluate_source(self, source: str, filename: str = "<string>") -> Evaluation:
        if self.on_event is not None:
            self.on_event(EvaluationStarted(filename=filename))
        if source == "fail":
            raise EvaluationFailed(f"{filename}:1:1: rejected")
        if source == "crash":
            raise RuntimeError("secret detail of the daemon machine")
        return Evaluation(filename=filename, derivations={}, seconds=0.25)

    def build(self, drv: Derivation) -> BuildResult:
        raise NotImplementedError

    def check(self, drv: Derivation) -> BuildResult:
        raise NotImplementedError

    def plan(self, drv: Derivation) -> BuildPlan:
        raise NotImplementedError

    def path_info(self, path: str) -> StorePathInfo:
        raise NotImplementedError

    def verify(self, path: str) -> VerifyResult:
        raise NotImplementedError

    def sandbox_config(self, drv: Derivation) -> OciConfig:
        spec = make_spec(drv, SandboxSettings(), uid=1000, gid=1000, machine="x86_64")
        return oci_config(spec)

    def backend_status(self) -> tuple[BackendStatus, ...]:
        return (
            BackendStatus(name="namespaces", available=True),
            BackendStatus(name="oci", available=False, reason="not available in this release"),
        )

    def gc(self) -> GCResult:
        return GCResult(removed=("/lattice/store/a-x",), freed_bytes=3, seconds=0.5)

    def add_root(self, name: str, path: str) -> GCRootInfo:
        return GCRootInfo(name=name, path=path, created_at=CREATED)

    def remove_root(self, name: str) -> GCRootInfo:
        return GCRootInfo(name=name, path="/lattice/store/a-x", created_at=CREATED)

    def roots(self) -> tuple[GCRootInfo, ...]:
        return (GCRootInfo(name="system", path="/lattice/store/a-x", created_at=CREATED),)

    def switch(self, profile: str, drv: Derivation) -> Generation:
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> None:
        return None


@pytest.fixture
def factory() -> Callable[[EventHandler | None], LatticeAPI]:
    return FakeAPI


STORE_SCRIPT = """
import json, os, sys
fd, plan = int(sys.argv[1]), json.loads(sys.argv[2])
def report(**fields):
    os.write(fd, (json.dumps(fields) + "\\n").encode())
report(kind="started", pid=os.getpid())
print("building", plan["name"], flush=True)
if plan["args"][:1] == ["fail"]:
    report(kind="failed", error="the builder exited with status 2")
    sys.exit(0)
for name in plan["outputs"]:
    out = os.path.join(plan["out_dir"], name)
    os.mkdir(out)
    with open(os.path.join(out, "file"), "w") as file:
        file.write("\\n".join(plan["args"]) + plan["extra"])
report(kind="built")
"""


class StoreBackend:
    """A sandbox backend that writes ``args`` and ``extra`` to ``file`` in each output.

    A build whose first argument is ``fail`` fails. ``jobs`` records every
    job started.
    """

    name = "fake"

    def __init__(self, extra: str = "") -> None:
        self.extra = extra
        self.jobs: list[SandboxJob] = []
        self._lock = threading.Lock()

    def status(self) -> BackendStatus:
        return BackendStatus(name="fake", available=True)

    def start(self, job: SandboxJob, *, status_fd: int, log_fd: int) -> subprocess.Popen[bytes]:
        with self._lock:
            self.jobs.append(job)
        plan = {
            "name": job.spec.name,
            "args": list(job.spec.args),
            "out_dir": job.out_dir,
            "outputs": [path.rpartition("/")[2] for path in job.spec.outputs.values()],
            "extra": self.extra,
        }
        return subprocess.Popen(
            [sys.executable, "-c", STORE_SCRIPT, str(status_fd), json.dumps(plan)],
            stdout=log_fd,
            stderr=log_fd,
            pass_fds=(status_fd,),
        )


@pytest.fixture
def new_backend() -> Callable[..., StoreBackend]:
    return StoreBackend


@pytest.fixture
def store_backend() -> StoreBackend:
    return StoreBackend()
