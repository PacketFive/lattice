# SPDX-License-Identifier: GPL-2.0
"""The sandbox description every backend consumes (RFC 0001 sections 9.2 to 9.6).

make_spec turns a derivation and the sandbox settings into a SandboxSpec:
the program to run, its environment, the inputs mounted read-only, the
mounts that make up the filesystem, the id mappings and the resource
limits. The namespace backend applies it directly, and oci_config writes
the same description as an OCI runtime configuration, which
``lattice sandbox-config`` prints.
"""

from __future__ import annotations

import posixpath
from typing import Literal

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..derivation import STORE_DIR, Derivation
from ..errors import BackendUnavailable, BuildFailed
from ..models import Model
from ..settings import SandboxSettings
from . import seccomp
from .environment import BUILD_DIR, build_environment

HOSTNAME = "localhost"
DEVICES: tuple[str, ...] = ("null", "zero", "full", "random", "urandom")


class Mount(Model):
    """One mount of the sandbox filesystem, applied in the order listed.

    ``source`` is set for a bind mount only. ``options`` are the flags and
    data of the mount, such as ``nosuid`` or ``mode=0755``.
    """

    destination: str
    kind: Literal["tmpfs", "bind", "proc"]
    source: str | None = None
    read_only: bool = False
    options: tuple[str, ...] = ()


class IdMapping(Model):
    """Maps ``size`` ids from ``inside`` the user namespace to ``outside`` it."""

    inside: int
    outside: int
    size: int = 1


class Rlimit(Model):
    """A resource limit set before the builder starts."""

    name: Literal["RLIMIT_CORE", "RLIMIT_NOFILE"]
    soft: int
    hard: int


class SandboxSpec(Model):
    """Everything a backend needs to run one build.

    ``outputs`` maps each output name to the store path the builder
    creates. ``inputs`` are the store paths mounted read-only, and the
    builder is a program inside one of them.
    """

    name: str
    machine: str
    builder: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: str = BUILD_DIR
    hostname: str = HOSTNAME
    outputs: dict[str, str]
    inputs: tuple[str, ...]
    mounts: tuple[Mount, ...]
    uid_map: IdMapping
    gid_map: IdMapping
    rlimits: tuple[Rlimit, ...]


class SandboxJob(Model):
    """A SandboxSpec with the host paths one build uses.

    ``host_store`` is the host directory that holds the input store paths,
    normally STORE_DIR. ``root`` is an empty host directory the sandbox
    root is mounted on, and the outputs are copied into ``out_dir``, one
    entry per output named by the last part of its store path.
    ``parent_pid`` is the process that supervises the build.
    """

    spec: SandboxSpec
    host_store: str
    root: str
    out_dir: str
    parent_pid: int


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


def make_spec(
    drv: Derivation, settings: SandboxSettings, *, uid: int, gid: int, machine: str
) -> SandboxSpec:
    """Describe the sandbox that builds ``drv``.

    Raises BuildFailed if the builder is built into Lattice, is not a
    normalised absolute path inside an input, or the environment breaks
    the rules of environment.py, and BackendUnavailable if no seccomp
    filter exists for ``machine``.
    """
    if machine not in seccomp.AUDIT_ARCH:
        raise BackendUnavailable(f"the build sandbox does not support {machine}")
    if drv.builder.startswith("builtin:"):
        raise BuildFailed(f"{drv.name}: {drv.builder} runs in Lattice, not in the sandbox")
    env = build_environment(drv)
    inputs = tuple(path for dep in input_closure(drv) for path in dep.output_paths.values())
    builder = drv.builder
    inside = any(builder == path or builder.startswith(path + "/") for path in inputs)
    if posixpath.normpath(builder) != builder or not inside:
        raise BuildFailed(
            f"{drv.name}: builder {builder!r} must be a program inside one of the"
            " derivation's inputs, named in env"
        )
    mounts = [
        Mount(destination=BUILD_DIR, kind="tmpfs", options=("nosuid", "nodev", "mode=0755")),
        Mount(destination=STORE_DIR, kind="tmpfs", options=("nosuid", "nodev", "mode=0755")),
        *(
            Mount(
                destination=path,
                kind="bind",
                source=path,
                read_only=True,
                options=("nosuid", "nodev"),
            )
            for path in inputs
        ),
        Mount(destination="/proc", kind="proc", options=("nosuid", "nodev", "noexec")),
        Mount(destination="/dev", kind="tmpfs", options=("nosuid", "noexec", "mode=0755")),
        *(
            Mount(
                destination=f"/dev/{name}",
                kind="bind",
                source=f"/dev/{name}",
                options=("nosuid", "noexec"),
            )
            for name in DEVICES
        ),
        Mount(
            destination="/dev/shm",  # noqa: S108
            kind="tmpfs",
            options=("nosuid", "nodev", "noexec", "mode=1777"),
        ),
    ]
    return SandboxSpec(
        name=drv.name,
        machine=machine,
        builder=builder,
        args=drv.args,
        env=env,
        outputs=drv.output_paths,
        inputs=inputs,
        mounts=tuple(mounts),
        uid_map=IdMapping(inside=0, outside=uid),
        gid_map=IdMapping(inside=0, outside=gid),
        rlimits=(
            Rlimit(name="RLIMIT_CORE", soft=0, hard=0),
            Rlimit(
                name="RLIMIT_NOFILE", soft=settings.max_open_files, hard=settings.max_open_files
            ),
        ),
    )


class _Oci(Model):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class OciUser(_Oci):
    """``process.user``."""

    uid: int = 0
    gid: int = 0


class OciRlimit(_Oci):
    """One entry of ``process.rlimits``."""

    type: str
    hard: int
    soft: int


class OciCapabilities(_Oci):
    """``process.capabilities``: every set is empty in the sandbox."""

    bounding: tuple[str, ...] = ()
    effective: tuple[str, ...] = ()
    permitted: tuple[str, ...] = ()
    inheritable: tuple[str, ...] = ()
    ambient: tuple[str, ...] = ()


class OciProcess(_Oci):
    """``process``."""

    terminal: bool = False
    user: OciUser = OciUser()
    args: tuple[str, ...]
    env: tuple[str, ...]
    cwd: str
    no_new_privileges: bool = True
    capabilities: OciCapabilities = OciCapabilities()
    rlimits: tuple[OciRlimit, ...]


class OciRoot(_Oci):
    """``root``: an empty directory the runtime provides."""

    path: str = "rootfs"
    readonly: bool = False


class OciMount(_Oci):
    """One entry of ``mounts``."""

    destination: str
    type: str
    source: str
    options: tuple[str, ...]


class OciNamespace(_Oci):
    """One entry of ``linux.namespaces``."""

    type: str


class OciIdMapping(_Oci):
    """One entry of ``linux.uidMappings`` or ``linux.gidMappings``."""

    container_id: int = Field(alias="containerID")
    host_id: int = Field(alias="hostID")
    size: int


class OciSeccompArg(_Oci):
    """One condition of a seccomp rule."""

    index: int
    value: int
    value_two: int
    op: str


class OciSyscall(_Oci):
    """One entry of ``linux.seccomp.syscalls``."""

    names: tuple[str, ...]
    action: str
    errno_ret: int
    args: tuple[OciSeccompArg, ...] = ()


class OciSeccomp(_Oci):
    """``linux.seccomp``."""

    default_action: str = "SCMP_ACT_ALLOW"
    architectures: tuple[str, ...]
    syscalls: tuple[OciSyscall, ...]


class OciLinux(_Oci):
    """``linux``."""

    namespaces: tuple[OciNamespace, ...]
    uid_mappings: tuple[OciIdMapping, ...]
    gid_mappings: tuple[OciIdMapping, ...]
    seccomp: OciSeccomp


class OciConfig(_Oci):
    """An OCI runtime configuration, ``config.json`` (section 9.6)."""

    oci_version: str = "1.2.0"
    process: OciProcess
    root: OciRoot = OciRoot()
    hostname: str
    mounts: tuple[OciMount, ...]
    linux: OciLinux


NAMESPACES: tuple[str, ...] = ("user", "mount", "network", "pid", "uts", "ipc")


def _oci_mount(mount: Mount) -> OciMount:
    options = list(mount.options)
    if mount.kind == "bind":
        options = ["rbind", *options]
    if mount.read_only:
        options.append("ro")
    source = mount.source if mount.source is not None else mount.kind
    return OciMount(
        destination=mount.destination, type=mount.kind, source=source, options=tuple(options)
    )


def _oci_mapping(mapping: IdMapping) -> OciIdMapping:
    return OciIdMapping(containerID=mapping.inside, hostID=mapping.outside, size=mapping.size)


def oci_config(spec: SandboxSpec) -> OciConfig:
    """Write ``spec`` as an OCI runtime configuration."""
    return OciConfig(
        process=OciProcess(
            args=(spec.builder, *spec.args),
            env=tuple(f"{k}={v}" for k, v in spec.env.items()),
            cwd=spec.cwd,
            rlimits=tuple(OciRlimit(type=r.name, hard=r.hard, soft=r.soft) for r in spec.rlimits),
        ),
        hostname=spec.hostname,
        mounts=tuple(_oci_mount(m) for m in spec.mounts),
        linux=OciLinux(
            namespaces=tuple(OciNamespace(type=n) for n in NAMESPACES),
            uid_mappings=(_oci_mapping(spec.uid_map),),
            gid_mappings=(_oci_mapping(spec.gid_map),),
            seccomp=OciSeccomp(
                architectures=(seccomp.OCI_ARCH[spec.machine],),
                syscalls=tuple(
                    OciSyscall.model_validate(entry) for entry in seccomp.oci_syscalls(spec.machine)
                ),
            ),
        ),
    )


class HelperReport(Model):
    """One line the sandbox helper writes on its status pipe.

    ``started`` carries the host pid of the builder's PID 1, so the runner
    can kill the build. ``built`` or ``failed`` ends the build; ``failed``
    carries the reason.
    """

    kind: Literal["started", "built", "failed"]
    pid: int | None = None
    error: str | None = None
