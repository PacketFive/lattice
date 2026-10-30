# SPDX-License-Identifier: GPL-2.0
"""The build sandbox (RFC 0001 section 9), provided by Cloche.

Cloche runs a program in an unprivileged sandbox and knows nothing of
derivations or the store. This module turns a derivation into a Cloche
Command and adds the rules that belong to Lattice:

* a ``builtin:`` builder runs in Lattice, never in the sandbox;
* the builder is a program inside one of the build's inputs;
* the inputs are bound read-only at their store paths, from the host
  directory that holds the store;
* the store directory itself is an empty writable directory, so each
  output is created at its store path;
* each output path is passed in the environment under the output's name,
  which ``env`` may not set;
* an input that is a shallow path is bound from the CernVM-FS directory
  it links to, given in ``sources`` (RFC 0001 section 14.5).

Cloche's errors become Lattice's: a command that breaks a rule or a run
that fails raises BuildFailed, and a backend that cannot run here raises
BackendUnavailable.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable

import cloche
from cloche import Backend, BackendStatus, OciConfig, SandboxJob, SandboxSpec, oci_config
from pydantic import ValidationError

from .derivation import STORE_DIR, Derivation
from .errors import BackendUnavailable, BuildFailed
from .models import Model
from .settings import SandboxBackend, SandboxSettings


class BuildRequest(Model):
    """One build to run.

    ``work_dir`` is an empty directory that holds the sandbox root, the
    copied outputs and ``run.log``. ``host_store`` is the host directory
    that holds the derivation's inputs. ``inputs`` lists the store paths
    the build sees; None means the outputs of every derivation it depends
    on (see make_spec). ``sources`` gives the host directory of an input
    that is not in ``host_store``, such as a shallow path.
    """

    derivation: Derivation
    work_dir: str
    host_store: str = STORE_DIR
    sandbox: SandboxSettings = SandboxSettings()
    inputs: tuple[str, ...] | None = None
    sources: dict[str, str] = {}


class BuildOutcome(Model):
    """A finished build: the host path of each output, the log and the time taken.

    Each output is copied to ``<work_dir>/out/<output name>``.
    ``boot_seconds`` is the time the microVM backend took to boot the
    virtual machine, and None on the other backends.
    """

    outputs: dict[str, str]
    log: str
    seconds: float
    boot_seconds: float | None = None


def input_closure(drv: Derivation) -> tuple[Derivation, ...]:
    """Return every derivation reachable through ``env``, sorted by store path."""
    seen: dict[str, Derivation] = {}
    stack = [drv]
    while stack:
        for value in stack.pop().env.values():
            if isinstance(value, Derivation) and value.hash not in seen:
                seen[value.hash] = value
                stack.append(value)
    return tuple(sorted(seen.values(), key=lambda d: d.store_path))


def build_command(
    drv: Derivation,
    *,
    inputs: tuple[str, ...] | None = None,
    host_store: str = STORE_DIR,
    sources: dict[str, str] | None = None,
) -> cloche.Command:
    """Return the Cloche Command that builds ``drv``.

    ``inputs`` are the store paths bound in the sandbox. The store passes
    the outputs of the derivations ``drv`` depends on with everything
    they refer to (section 10.1); None binds the outputs of every
    derivation reachable through ``env`` instead. ``host_store`` is where
    the inputs are on the host, and ``sources`` names the host directory
    of any input that is elsewhere.

    Raises BuildFailed if the builder is built into Lattice or is not a
    program inside an input, if ``env`` names an output, or if Cloche
    rejects a field.
    """
    if drv.builder.startswith("builtin:"):
        raise BuildFailed(f"{drv.name}: {drv.builder} runs in Lattice, not in the sandbox")
    if inputs is None:
        inputs = tuple(path for dep in input_closure(drv) for path in dep.output_paths.values())
    builder = drv.builder
    inside = any(builder == path or builder.startswith(path + "/") for path in inputs)
    if posixpath.normpath(builder) != builder or not inside:
        raise BuildFailed(
            f"{drv.name}: builder {builder!r} must be a program inside one of the"
            " derivation's inputs, named in env"
        )
    outputs = drv.output_paths
    env: dict[str, str] = {}
    for key, value in drv.env.items():
        name = str(key)
        if name in outputs:
            raise BuildFailed(f"{drv.name}: env sets {name}, which names an output")
        env[name] = str(value)
    env.update(outputs)
    sources = sources or {}
    binds = tuple(
        cloche.Bind(source=sources.get(path, host_store + path[len(STORE_DIR) :]), destination=path)
        for path in inputs
    )
    try:
        return cloche.Command(
            name=drv.name,
            program=builder,
            args=drv.args,
            env=env,
            binds=binds,
            directories=(STORE_DIR,),
            outputs=outputs,
        )
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise BuildFailed(f"{drv.name}: {where}: {first['msg']}") from None


def _limits(settings: SandboxSettings) -> cloche.Limits:
    fields = settings.model_dump(exclude={"runtime", "microvm", "max_memory_mib"})
    if settings.max_memory_mib is not None:
        fields["max_memory"] = settings.max_memory_mib << 20
    return cloche.Limits.model_validate(fields)


def make_spec(
    drv: Derivation,
    settings: SandboxSettings,
    *,
    uid: int,
    gid: int,
    machine: str,
    inputs: tuple[str, ...] | None = None,
    host_store: str = STORE_DIR,
) -> SandboxSpec:
    """Describe the sandbox that builds ``drv`` (see build_command).

    Raises BuildFailed if the build breaks a rule of Lattice or Cloche,
    and BackendUnavailable if the sandbox does not support ``machine``.
    """
    command = build_command(drv, inputs=inputs, host_store=host_store)
    try:
        return cloche.make_spec(command, _limits(settings), uid=uid, gid=gid, machine=machine)
    except cloche.InvalidCommand as error:
        raise BuildFailed(str(error)) from None
    except cloche.SandboxUnavailable as error:
        raise BackendUnavailable(str(error)) from None


def run_build(
    request: BuildRequest,
    backend: Backend | None = None,
    on_log: Callable[[str], object] | None = None,
) -> BuildOutcome:
    """Build ``request.derivation`` in the sandbox.

    Raises BuildFailed, with the end of the log, when the build fails or
    runs out of time, and BackendUnavailable when the backend cannot run
    here. If anything else raises, the build is stopped first.
    """
    command = build_command(
        request.derivation,
        inputs=request.inputs,
        host_store=request.host_store,
        sources=request.sources,
    )
    try:
        outcome = cloche.run(
            command,
            request.work_dir,
            _limits(request.sandbox),
            backend=backend,
            on_log=on_log,
        )
    except (cloche.InvalidCommand, cloche.RunFailed) as error:
        raise BuildFailed(str(error)) from None
    except cloche.SandboxUnavailable as error:
        raise BackendUnavailable(str(error)) from None
    return BuildOutcome(
        outputs=outcome.outputs,
        log=outcome.log,
        seconds=outcome.seconds,
        boot_seconds=outcome.boot_seconds,
    )


def host_path(path: str, host_store: str = STORE_DIR) -> str:
    """Return where ``path`` is on the host: under ``host_store`` if it is in the store."""
    if path.startswith(STORE_DIR + "/"):
        return host_store + path[len(STORE_DIR) :]
    return path


def guest_files(sandbox: SandboxSettings) -> tuple[str, ...]:
    """Return the guest kernel and init of the microVM backend, as the settings name them."""
    return tuple(path for path in (sandbox.microvm.kernel, sandbox.microvm.init) if path)


def microvm_config(sandbox: SandboxSettings, host_store: str = STORE_DIR) -> cloche.MicroVMConfig:
    """Return the Cloche configuration of the microVM backend in ``sandbox``.

    A guest kernel or init in the store is read from ``host_store``.
    """
    settings = sandbox.microvm
    fields = settings.model_dump()
    for name in ("kernel", "init"):
        if fields[name] is not None:
            fields[name] = host_path(fields[name], host_store)
    return cloche.MicroVMConfig.model_validate(fields)


def select_backend(
    name: SandboxBackend,
    sandbox: SandboxSettings | None = None,
    host_store: str = STORE_DIR,
) -> Backend:
    """Return the backend called ``name``, configured by ``sandbox``.

    Raises BackendUnavailable when it cannot run here. Lattice never falls
    back to a weaker backend.
    """
    sandbox = sandbox or SandboxSettings()
    try:
        return cloche.select_backend(
            name, runtime=sandbox.runtime, microvm=microvm_config(sandbox, host_store)
        )
    except cloche.SandboxUnavailable as error:
        raise BackendUnavailable(str(error)) from None


def backend_status(
    sandbox: SandboxSettings | None = None, host_store: str = STORE_DIR
) -> tuple[BackendStatus, ...]:
    """Report which sandbox backends can run on this machine (section 9.7).

    A last entry named ``cgroups`` says whether builds get their own
    cgroup, which the memory, CPU and process limits need (section 9.4).
    """
    sandbox = sandbox or SandboxSettings()
    statuses = cloche.backend_status(
        runtime=sandbox.runtime, microvm=microvm_config(sandbox, host_store)
    )
    cgroup = cloche.cgroup_status()
    return (
        *statuses,
        BackendStatus(name="cgroups", available=cgroup.available, reason=cgroup.reason),
    )


__all__ = [
    "Backend",
    "BackendStatus",
    "BuildOutcome",
    "BuildRequest",
    "OciConfig",
    "SandboxJob",
    "SandboxSpec",
    "backend_status",
    "build_command",
    "guest_files",
    "host_path",
    "input_closure",
    "make_spec",
    "microvm_config",
    "oci_config",
    "run_build",
    "select_backend",
]
