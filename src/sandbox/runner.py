# SPDX-License-Identifier: GPL-2.0
"""Runs one build in the sandbox and supervises it (RFC 0001 sections 9.1 and 9.4).

run_build describes the sandbox, starts the backend and then waits. It
writes everything the builder prints to the build log, passes each chunk
to ``on_log``, and kills the build when the wall-clock limit or the
silence limit of the sandbox settings runs out. When the build succeeds
each output has been copied to ``<work_dir>/out``, named like its store
path; Phase 3 moves them into the store.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ..derivation import STORE_DIR, Derivation
from ..errors import BuildFailed
from ..models import Model
from ..settings import SandboxSettings
from . import libc
from .spec import HelperReport, SandboxJob, make_spec

if TYPE_CHECKING:
    from . import Backend

LOG_TAIL_LINES = 25


class BuildRequest(Model):
    """One build to run.

    ``work_dir`` is an empty directory that holds the sandbox root, the
    copied outputs and ``build.log``. ``host_store`` is the host directory
    that holds the derivation's inputs. ``inputs`` lists the store paths
    the build sees; None means the outputs of every derivation it depends
    on (see make_spec).
    """

    derivation: Derivation
    work_dir: str
    host_store: str = STORE_DIR
    sandbox: SandboxSettings = SandboxSettings()
    inputs: tuple[str, ...] | None = None


class BuildOutcome(Model):
    """A finished build: the host path of each output, the log and the time taken."""

    outputs: dict[str, str]
    log: str
    seconds: float


def _tail(log: Path) -> str:
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _supervise(
    process: subprocess.Popen[bytes],
    readers: set[int],
    log_read: int,
    log: Path,
    sandbox: SandboxSettings,
    on_log: Callable[[str], None] | None,
    started: float,
) -> tuple[list[HelperReport], str | None]:
    """Copy the log and read reports until the helper closes both pipes.

    Closes each pipe in ``readers`` at its end of file and removes it.
    Returns the reports and, if a time limit ended the build, the reason.
    """
    reports: list[HelperReport] = []
    timed_out: str | None = None
    status_buffer = b""
    last_output = started
    with selectors.DefaultSelector() as selector, log.open("wb") as log_file:
        for fd in readers:
            selector.register(fd, selectors.EVENT_READ)
        while selector.get_map():
            now = time.monotonic()
            limits = []
            if timed_out is None and sandbox.timeout is not None:
                limits.append((started + sandbox.timeout, f"timed out after {sandbox.timeout} s"))
            if timed_out is None and sandbox.max_silent_time is not None:
                limits.append(
                    (
                        last_output + sandbox.max_silent_time,
                        f"printed nothing for {sandbox.max_silent_time} s",
                    )
                )
            expired = [reason for deadline, reason in limits if deadline <= now]
            if expired:
                # The builder is PID 1 of its namespace and dies with the
                # helper, so killing the helper ends the whole build.
                timed_out = expired[0]
                process.kill()
                continue
            wait = min((deadline for deadline, _ in limits), default=None)
            for key, _ in selector.select(None if wait is None else max(wait - now, 0)):
                fd = int(key.fd)
                chunk = os.read(fd, 65536)
                if not chunk:
                    selector.unregister(fd)
                    readers.discard(fd)
                    os.close(fd)
                elif fd == log_read:
                    last_output = time.monotonic()
                    log_file.write(chunk)
                    log_file.flush()
                    if on_log is not None:
                        on_log(chunk.decode("utf-8", errors="replace"))
                else:
                    status_buffer += chunk
                    *lines, status_buffer = status_buffer.split(b"\n")
                    for line in lines:
                        try:
                            reports.append(HelperReport.model_validate_json(line))
                        except ValidationError:
                            continue
    return reports, timed_out


def run_build(
    request: BuildRequest,
    backend: Backend | None = None,
    on_log: Callable[[str], None] | None = None,
) -> BuildOutcome:
    """Build ``request.derivation`` in the sandbox.

    Raises BuildFailed, with the end of the build log, when the build
    fails or runs out of time, and BackendUnavailable when the backend
    cannot run here. If anything else raises, the build is stopped first.
    """
    if backend is None:
        from .namespaces import NamespaceBackend

        backend = NamespaceBackend()
    drv = request.derivation
    spec = make_spec(
        drv,
        request.sandbox,
        uid=os.getuid(),
        gid=os.getgid(),
        machine=libc.machine(),
        inputs=request.inputs,
    )
    work = Path(request.work_dir)
    root, out_dir, log = work / "root", work / "out", work / "build.log"
    root.mkdir(mode=0o700)
    out_dir.mkdir(mode=0o700)
    job = SandboxJob(
        spec=spec,
        host_store=request.host_store,
        root=str(root),
        out_dir=str(out_dir),
        parent_pid=os.getpid(),
    )
    started = time.monotonic()
    log_read, log_write = os.pipe2(os.O_CLOEXEC)
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    readers = {log_read, status_read}
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = backend.start(job, status_fd=status_write, log_fd=log_write)
        finally:
            os.close(log_write)
            os.close(status_write)
        reports, timed_out = _supervise(
            process, readers, log_read, log, request.sandbox, on_log, started
        )
        exit_status = process.wait()
    finally:
        for fd in readers:
            os.close(fd)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    seconds = time.monotonic() - started
    final = reports[-1] if reports and reports[-1].kind != "started" else None
    if timed_out is not None:
        reason = timed_out
    elif final is None:
        reason = f"the sandbox helper exited with status {exit_status} and no report"
    elif final.kind == "failed":
        reason = final.error or "the build failed"
    else:
        outputs = {name: str(out_dir / Path(path).name) for name, path in spec.outputs.items()}
        return BuildOutcome(outputs=outputs, log=str(log), seconds=seconds)
    tail = _tail(log)
    raise BuildFailed(
        f"{drv.name}: {reason}" + (f"\nlast lines of the build log:\n{tail}" if tail else "")
    )
