# SPDX-License-Identifier: GPL-2.0
"""Shared fixtures: stand-ins for the daemon backend and the sandbox.

FakeAPI satisfies LatticeAPI without evaluating or building anything, so
the daemon, protocol and client are tested apart from the operations
behind them. StoreBackend is a sandbox backend that runs a small Python
program instead of the sandbox helper, so the store is tested on any
machine. module_image builds a minimal kernel module file and FakeKernel
stands in for the running kernel, so activation is tested without
loading real modules.
"""

from __future__ import annotations

import fnmatch
import json
import os
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import pytest

from lattice import (
    ActivationFailed,
    BuildResult,
    Derivation,
    Evaluation,
    EvaluationFailed,
    EvaluationStarted,
    EventHandler,
    GCResult,
    Generation,
    LatticeAPI,
    ProfileSwitched,
    PushResult,
    SandboxSettings,
    SlotState,
    SubstitutionResult,
)
from lattice.kmod import LoadedModule, read_info
from lattice.procscan import ProcessUse
from lattice.sandbox import BackendStatus, OciConfig, SandboxJob, make_spec, oci_config
from lattice.settings import SandboxBackend
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

    def check(self, drv: Derivation, backend: SandboxBackend | None = None) -> BuildResult:
        """Return a result whose output names ``backend``."""
        return BuildResult(
            derivation=drv,
            store_path=drv.store_path,
            outputs={"out": str(backend)},
            built=True,
            seconds=0.0,
        )

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
        if self.on_event is not None:
            self.on_event(ProfileSwitched(profile=profile, number=2, store_path=drv.store_path))
        return Generation(profile=profile, number=2, store_path=drv.store_path, current=True)

    def rollback(self, profile: str, generation: int | None = None) -> Generation:
        number = 1 if generation is None else generation
        return Generation(
            profile=profile, number=number, store_path="/lattice/store/a-x", current=True
        )

    def generations(self, profile: str) -> tuple[Generation, ...]:
        return (
            Generation(profile=profile, number=1, store_path="/lattice/store/a-x"),
            Generation(profile=profile, number=2, store_path="/lattice/store/b-y", current=True),
        )

    def delete_generations(self, profile: str, keep: int) -> tuple[Generation, ...]:
        return (
            ()
            if keep
            else (Generation(profile=profile, number=1, store_path="/lattice/store/a-x"),)
        )

    def slots(self) -> tuple[SlotState, ...]:
        return (SlotState(slot="nvidia", active="/lattice/store/a-s", wanted="/lattice/store/b-s"),)

    def substitute(self, path: str, shallow: bool = False) -> SubstitutionResult:
        return SubstitutionResult(path=path, substituted=(path,) if shallow else (), seconds=0.0)

    def materialise(self, path: str) -> StorePathInfo:
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> PushResult:
        return PushResult(cache=cache, pushed=tuple(paths), skipped=(), seconds=0.0)


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
    report(kind="failed", error="the program exited with status 2")
    sys.exit(0)
for name in plan["outputs"]:
    out = os.path.join(plan["out_dir"], name)
    os.mkdir(out)
    with open(os.path.join(out, "file"), "w") as file:
        file.write("\\n".join(plan["args"]) + plan["extra"])
report(kind="finished")
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
            "outputs": list(job.spec.outputs),
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


def module_image(fields: dict[str, str], *, order: str = "<") -> bytes:
    """Return an ELF64 relocatable file whose ``.modinfo`` section holds ``fields``."""
    info = b"".join(f"{key}={value}".encode() + b"\0" for key, value in fields.items())
    names = b"\0.modinfo\0.shstrtab\0"
    info_offset = 64
    names_offset = info_offset + len(info)
    table = (names_offset + len(names) + 7) // 8 * 8
    ident = b"\x7fELF" + bytes([2, 1 if order == "<" else 2, 1]) + bytes(9)
    header = struct.pack(
        order + "16sHHIQQQIHHHHHH", ident, 1, 62, 1, 0, 0, table, 0, 64, 0, 0, 64, 3, 2
    )
    sections = [
        (0, 0, 0, 0),
        (1, 1, info_offset, len(info)),
        (10, 3, names_offset, len(names)),
    ]
    image = bytearray(header + info + names)
    image += bytes(table - len(image))
    for name, kind, offset, size in sections:
        image += struct.pack(order + "IIQQQQIIQQ", name, kind, 0, 0, offset, size, 0, 0, 1, 0)
    return bytes(image)


class FakeKernel:
    """KernelModules for a pretend kernel that records every change in ``log``.

    Loading a module file whose path contains a string in ``failing`` fails. ``devices`` are
    the device files processes have open.
    """

    def __init__(self, release: str = "6.12.0") -> None:
        self.kernel_release = release
        self.modules: dict[str, LoadedModule] = {}
        self.failing: set[str] = set()
        self.devices: list[ProcessUse] = []
        self.log: list[str] = []

    def release(self) -> str:
        return self.kernel_release

    def loaded(self) -> dict[str, LoadedModule]:
        return dict(self.modules)

    def load(self, path: str) -> None:
        info = read_info(path)
        if any(part in path for part in self.failing) or info.name in self.modules:
            raise ActivationFailed(f"cannot load {path}")
        for dependency in info.depends:
            used = self.modules[dependency]
            self.modules[dependency] = LoadedModule(
                name=dependency, refcount=used.refcount + 1, holders=(*used.holders, info.name)
            )
        self.modules[info.name] = LoadedModule(name=info.name, refcount=0, holders=())
        self.log.append(f"load {info.name}")

    def unload(self, name: str) -> None:
        module = self.modules.get(name)
        if module is None or module.refcount:
            raise ActivationFailed(f"cannot unload {name}")
        del self.modules[name]
        for other, used in list(self.modules.items()):
            if name in used.holders:
                holders = tuple(h for h in used.holders if h != name)
                self.modules[other] = LoadedModule(
                    name=other, refcount=used.refcount - 1, holders=holders
                )
        self.log.append(f"unload {name}")

    def open_devices(self, patterns: tuple[str, ...]) -> tuple[ProcessUse, ...]:
        return tuple(
            use
            for use in self.devices
            if any(fnmatch.fnmatchcase(use.path, pattern) for pattern in patterns)
        )


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


@pytest.fixture
def make_module() -> Callable[..., bytes]:
    return module_image
