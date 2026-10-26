# SPDX-License-Identifier: GPL-2.0
"""Build supervision tests with a stand-in backend: RFC 0001 sections 9.1 and 9.4.

FakeBackend runs a small Python program instead of the sandbox helper,
so log capture, reports and time limits are tested on any machine.
"""

import os
import resource
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation, SandboxSettings
from lattice.sandbox import BuildRequest, SandboxJob, run_build
from lattice.sandbox.namespaces import copy_out

SCRIPT = """
import json, os, sys, time
fd, mode = int(sys.argv[1]), sys.argv[2]
def report(**fields):
    os.write(fd, (json.dumps(fields) + "\\n").encode())
report(kind="started", pid=os.getpid())
os.write(fd, b"not json\\n")
print("building", flush=True)
if mode == "built":
    report(kind="built")
elif mode == "failed":
    report(kind="failed", error="the builder exited with status 2")
elif mode == "silent":
    time.sleep(30)
elif mode == "chatty":
    while True:
        print("tick", flush=True)
        time.sleep(0.05)
else:
    sys.exit(3)
"""

TOOLS = Derivation(name="tools", builder="/bin/sh")
DRV = Derivation(name="hello", builder=f"{TOOLS.store_path}/bin/sh", env=FrozenMap({"t": TOOLS}))


class FakeBackend:
    name = "fake"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.job: SandboxJob | None = None

    def status(self) -> object:
        raise AssertionError("not called")

    def start(self, job: SandboxJob, *, status_fd: int, log_fd: int) -> subprocess.Popen[bytes]:
        self.job = job
        return subprocess.Popen(
            [sys.executable, "-c", SCRIPT, str(status_fd), self.mode],
            stdout=log_fd,
            stderr=log_fd,
            pass_fds=(status_fd,),
        )


def run(tmp_path: Path, mode: str, **limits: int) -> tuple[FakeBackend, object]:
    backend = FakeBackend(mode)
    request = BuildRequest(
        derivation=DRV, work_dir=str(tmp_path), sandbox=SandboxSettings(**limits)
    )
    seen: list[str] = []
    try:
        outcome: object = run_build(request, backend, seen.append)  # type: ignore[arg-type]
    except BuildFailed as error:
        outcome = error
    assert "".join(seen).startswith("building\n")
    return backend, outcome


def test_exception_stops_the_build(tmp_path: Path) -> None:
    backend = FakeBackend("chatty")
    request = BuildRequest(derivation=DRV, work_dir=str(tmp_path))

    def on_log(text: str) -> None:
        raise RuntimeError("client went away")

    processes: list[subprocess.Popen[bytes]] = []
    start = backend.start

    def recording_start(job: SandboxJob, **fds: int) -> subprocess.Popen[bytes]:
        processes.append(start(job, **fds))
        return processes[-1]

    backend.start = recording_start  # type: ignore[method-assign]
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(RuntimeError, match="client went away"):
        run_build(request, backend, on_log)  # type: ignore[arg-type]
    assert processes[0].poll() is not None
    assert set(os.listdir("/proc/self/fd")) == before


def test_built(tmp_path: Path) -> None:
    backend, outcome = run(tmp_path, "built")
    assert not isinstance(outcome, BuildFailed)
    assert outcome.outputs == {"out": str(tmp_path / "out" / Path(DRV.store_path).name)}  # type: ignore[attr-defined]
    assert Path(outcome.log).read_text(encoding="utf-8") == "building\n"  # type: ignore[attr-defined]
    assert backend.job is not None
    assert backend.job.root == str(tmp_path / "root")
    assert backend.job.host_store == "/lattice/store"


def test_failed_report(tmp_path: Path) -> None:
    _, outcome = run(tmp_path, "failed")
    assert isinstance(outcome, BuildFailed)
    assert str(outcome) == (
        "hello: the builder exited with status 2\nlast lines of the build log:\nbuilding"
    )


def test_no_report(tmp_path: Path) -> None:
    _, outcome = run(tmp_path, "crash")
    assert "exited with status 3 and no report" in str(outcome)


def test_timeout(tmp_path: Path) -> None:
    _, outcome = run(tmp_path, "chatty", timeout=1)
    assert "timed out after 1 s" in str(outcome)


def test_silence(tmp_path: Path) -> None:
    _, outcome = run(tmp_path, "silent", max_silent_time=1, timeout=60)
    assert "printed nothing for 1 s" in str(outcome)


def test_work_dir_must_be_empty(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    with pytest.raises(FileExistsError):
        run_build(BuildRequest(derivation=DRV, work_dir=str(tmp_path)), FakeBackend("built"))  # type: ignore[arg-type]


def test_copy_out_holds_few_descriptors(tmp_path: Path) -> None:
    output = tmp_path / "root/lattice/store/abc-many"
    for index in range(300):
        leaf = output / f"d{index}" / "sub"
        leaf.mkdir(parents=True)
        (leaf / "file").write_text(str(index), encoding="utf-8")
    deep = output / "deep"
    for _ in range(20):
        deep /= "x"
    deep.mkdir(parents=True)
    (output / "d0").chmod(0o555)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (len(os.listdir("/proc/self/fd")) + 64, hard))
    try:
        copy_out(str(tmp_path / "root"), "/lattice/store/abc-many", str(tmp_path / "copy"))
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    copy = tmp_path / "copy"
    assert (copy / "d299/sub/file").read_text(encoding="utf-8") == "299"
    assert (copy / "deep").joinpath(*["x"] * 20).is_dir()
    assert stat.S_IMODE((copy / "d0").stat().st_mode) == 0o555
