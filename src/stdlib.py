# SPDX-License-Identifier: GPL-2.0
"""The Lattice standard library (RFC 0001 section 7.6).

A PyBonsai file evaluated by Lattice can call the pybonsai prelude (``len``,
``range``, ``sorted`` and the other pure helpers) and the functions defined
here. build_host() combines both into the pybonsai.Host that Lattice passes
to the evaluator, and registers Derivation as a host type with the
attributes listed in RFC 0001 section 7.5.

Each function returns the same result for the same arguments and reads or
changes nothing outside the evaluator.

``stack`` and ``profile`` record their lists of derivations as numbered
``env`` keys, such as ``content_0`` and ``package_1``, so they need nothing
beyond the derivation format of section 8. Lattice builds them itself
(store/combine.py).
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
from collections.abc import Callable
from functools import partial

from pybonsai import Builtin, FrozenMap, Host, make_host
from pydantic import ValidationError

from .derivation import (
    BUILTIN_FETCH_URL,
    BUILTIN_PROFILE,
    BUILTIN_STACK,
    BUILTIN_SYSTEM,
    BUILTIN_UNPACK,
    Derivation,
    validate_fields,
)
from .models import Model
from .system.spec import (
    STORE_REFERENCE,
    EtcFileSpec,
    FileSystemSpec,
    GroupSpec,
    ServiceSpec,
    SystemSpec,
    UserSpec,
    describe,
)

# RFC 0001 section 7.5, derivation row.
DERIVATION_ATTRIBUTES: frozenset[str] = frozenset(
    {"name", "builder", "args", "env", "outputs", "hash"}
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLOT = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")

# fetch_url runs outside the build sandbox, so it only reaches the network.
# A file:// URL would let a configuration test for files on the build host.
FETCH_SCHEMES: frozenset[str] = frozenset({"https", "http"})


def build_host(config: FrozenMap | None = None) -> Host:
    """Return the Host Lattice evaluates every file with.

    Calls pybonsai.make_host with the prelude included, the functions
    below as Builtins under their RFC names, ``config`` as the read-only
    name ``config`` (RFC 0004 section 5.2), and
    ``{Derivation: DERIVATION_ATTRIBUTES}`` as host types. ``machine``
    records ``config`` in the system it declares.
    """
    config = config if config is not None else FrozenMap()
    functions: dict[str, Callable[..., object]] = {
        "derivation": derivation,
        "fetch_url": fetch_url,
        "stack": stack,
        "profile": profile,
        "unpack": unpack,
        "machine": partial(machine, config=config),
        "user": user,
        "group": group,
        "file_system": file_system,
        "service": service,
        "etc_file": etc_file,
    }
    names: dict[str, object] = {name: Builtin(name, impl) for name, impl in functions.items()}
    names["config"] = config
    return make_host(names=names, types={Derivation: DERIVATION_ATTRIBUTES})


def derivation(
    name: str,
    builder: str,
    args: tuple[str, ...] = (),
    env: FrozenMap | None = None,
    outputs: tuple[str, ...] = ("out",),
) -> Derivation:
    """Validate the fields with derivation.validate_fields and return a Derivation."""
    env = env if env is not None else FrozenMap()
    validate_fields(name, builder, args, env, outputs)
    return Derivation(name=name, builder=builder, args=args, env=env, outputs=outputs)


def fetch_url(url: str, sha256: str) -> Derivation:
    """Declare a fixed-output source. No network access happens here.

    ``sha256`` must be 64 lowercase hex characters and ``url`` must use a
    scheme in FETCH_SCHEMES. Returns a Derivation whose builder is
    BUILTIN_FETCH_URL, whose ``env`` holds ``url`` and ``sha256``, and
    whose name is the last path segment of the URL, which must be a valid
    derivation name.
    """
    if type(url) is not str or type(sha256) is not str:
        raise TypeError("fetch_url needs a url and a sha256 string")
    if not _SHA256.fullmatch(sha256):
        raise ValueError("fetch_url sha256 must be 64 lowercase hex characters")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in FETCH_SCHEMES or not parts.hostname:
        raise ValueError(f"fetch_url needs an https or http URL, not {url!r}")
    name = parts.path.rsplit("/", 1)[-1]
    return derivation(
        name=name,
        builder=BUILTIN_FETCH_URL,
        env=FrozenMap({"url": url, "sha256": sha256}),
    )


ARCHIVE_ENDINGS: tuple[str, ...] = (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".txz", ".tar")
"""Endings ``unpack`` removes from the archive's name, longest first."""


def unpacked_name(archive_name: str) -> str:
    """Return ``archive_name`` without the first matching ARCHIVE_ENDINGS entry."""
    for ending in ARCHIVE_ENDINGS:
        if archive_name.endswith(ending) and len(archive_name) > len(ending):
            return archive_name[: -len(ending)]
    return archive_name


def unpack(archive: Derivation) -> Derivation:
    """Declare the unpacked content of ``archive`` (RFC 0002 section 5.3).

    ``archive`` must be a derivation whose output is one tar file, such as
    a fetch_url. The name is the archive's name without its ending, as
    unpacked_name removes it. Lattice unpacks it itself, so
    this works before any program is in the store.
    """
    if type(archive) is not Derivation:
        raise TypeError("unpack needs a derivation")
    return derivation(
        name=unpacked_name(archive.name),
        builder=BUILTIN_UNPACK,
        env=FrozenMap({"archive": archive}),
    )


def check_slot(slot: object) -> str:
    """Return ``slot`` if it is a valid slot name, and raise otherwise.

    A slot name is a file name under ``/run/lattice/stacks``: letters,
    digits, ``.``, ``_``, ``+`` and ``-``, not starting with ``.``.
    """
    if type(slot) is not str:
        raise TypeError("a slot name must be a string")
    if not _SLOT.fullmatch(slot):
        raise ValueError(f"{slot!r} is not a valid slot name")
    return slot


def _derivations(what: str, values: object) -> tuple[Derivation, ...]:
    if type(values) is not tuple or not all(type(v) is Derivation for v in values):
        raise TypeError(f"{what} must be a list of derivations")
    assert isinstance(values, tuple)  # noqa: S101
    return values


def _devices(values: object) -> tuple[str, ...]:
    if type(values) is not tuple or not all(type(v) is str for v in values):
        raise TypeError("stack devices must be a list of strings")
    assert isinstance(values, tuple)  # noqa: S101
    for pattern in values:
        if not pattern.startswith("/dev/") or posixpath.normpath(pattern) != pattern:
            raise ValueError(f"stack device {pattern!r} must be a normalised path under /dev")
    return values


def stack(
    slot: str,
    name: str,
    contents: tuple[Derivation, ...],
    kernel_modules: tuple[Derivation, ...] = (),
    devices: tuple[str, ...] = (),
) -> Derivation:
    """Combine packages into one stack for ``slot`` (section 13.2).

    ``kernel_modules`` are derivations whose outputs hold the ``.ko``
    files the stack needs loaded, and ``devices`` are glob patterns of the
    device files those modules provide, such as ``/dev/nvidia*``. Lattice
    changes the modules only while no process has one of them open.
    """
    env: dict[str, object] = {"slot": check_slot(slot)}
    for index, drv in enumerate(_derivations("stack contents", contents)):
        env[f"content_{index}"] = drv
    for index, drv in enumerate(_derivations("stack kernel_modules", kernel_modules)):
        env[f"kernel_module_{index}"] = drv
    for index, pattern in enumerate(_devices(devices)):
        env[f"device_{index}"] = pattern
    return derivation(name=name, builder=BUILTIN_STACK, env=FrozenMap(env))


def profile(
    name: str,
    packages: tuple[Derivation, ...],
    stacks: FrozenMap | None = None,
) -> Derivation:
    """Build the environment of one profile generation (section 13.1).

    ``stacks`` maps each slot name to the stack active in it. Every value
    must be a stack made for that slot.
    """
    env: dict[str, object] = {}
    for index, drv in enumerate(_derivations("profile packages", packages)):
        env[f"package_{index}"] = drv
    stacks = stacks if stacks is not None else FrozenMap()
    if type(stacks) is not FrozenMap:
        raise TypeError("profile stacks must be a mapping from slot names to stacks")
    for slot, drv in stacks.items():
        check_slot(slot)
        if type(drv) is not Derivation or drv.builder != BUILTIN_STACK:
            raise TypeError(f"profile stack {slot!r} must be a stack")
        if drv.env["slot"] != slot:
            raise ValueError(f"stack {drv.name} is for slot {drv.env['slot']!r}, not {slot!r}")
        env[f"stack_{slot}"] = drv
    return derivation(name=name, builder=BUILTIN_PROFILE, env=FrozenMap(env))


def plain(value: object) -> object:
    """Return ``value`` with every FrozenMap turned into a dict, for the models.

    Derivations become their store paths, so a record names what it uses.
    """
    if isinstance(value, FrozenMap):
        return {plain(k): plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(plain(item) for item in value)
    if isinstance(value, Derivation):
        return value.store_path
    return value


def _record(model: type[Model], what: str, **fields: object) -> FrozenMap:
    values = {key: plain(value) for key, value in fields.items()}
    try:
        record = model.model_validate(values)
    except ValidationError as error:
        label = values.get("name", values.get("mount_point", values.get("path", "")))
        raise ValueError(f"{what} {label!r}: {describe(error)}") from None
    return FrozenMap(record.model_dump())


def user(
    name: str,
    uid: int | None = None,
    group: str = "users",
    groups: tuple[str, ...] = (),
    home: str | None = None,
    shell: str = "/run/current-system/sw/bin/sh",
    password_file: str | None = None,
    system_user: bool = False,
) -> FrozenMap:
    """Return a user account record for ``machine`` (RFC 0004 section 5.1).

    ``password_file`` names a file outside the store holding the password
    hash, which activation reads; without one the password is locked.
    """
    return _record(
        UserSpec,
        "user",
        name=name,
        uid=uid,
        group=group,
        groups=groups,
        home=home,
        shell=shell,
        password_file=password_file,
        system_user=system_user,
    )


def group(name: str, gid: int | None = None) -> FrozenMap:
    """Return a group record for ``machine``."""
    return _record(GroupSpec, "group", name=name, gid=gid)


def file_system(
    mount_point: str,
    device: str,
    fs_type: str = "auto",
    options: tuple[str, ...] = ("defaults",),
    needed_for_boot: bool = False,
) -> FrozenMap:
    """Return a mount record for ``machine``. It becomes a line of ``/etc/fstab``."""
    return _record(
        FileSystemSpec,
        "file_system",
        mount_point=mount_point,
        device=device,
        fs_type=fs_type,
        options=options,
        needed_for_boot=needed_for_boot,
    )


def service(
    name: str,
    exec_start: str,
    user: str = "root",
    after: tuple[str, ...] = (),
    wants: tuple[str, ...] = (),
    environment: FrozenMap | None = None,
    restart: str = "on-failure",
    unit: FrozenMap | None = None,
) -> FrozenMap:
    """Return a systemd service record for ``machine``.

    ``exec_start`` must start with a store path, and that path must be the
    output of a derivation the system includes.
    """
    return _record(
        ServiceSpec,
        "service",
        name=name,
        exec_start=exec_start,
        user=user,
        after=after,
        wants=wants,
        environment=environment if environment is not None else FrozenMap(),
        restart=restart,
        unit=unit if unit is not None else FrozenMap(),
    )


def etc_file(
    path: str,
    text: str | None = None,
    source: Derivation | None = None,
    mode: str = "0444",
) -> FrozenMap:
    """Return a record of one file under ``/etc``, given as ``text`` or a derivation.

    The record keeps ``source`` as a derivation, so ``machine`` can make
    it an input of the system.
    """
    if source is not None and type(source) is not Derivation:
        raise TypeError(f"etc_file {path!r} source must be a derivation")
    record = _record(EtcFileSpec, "etc_file", path=path, text=text, source=source, mode=mode)
    fields = dict(record.items())
    fields["source"] = source
    return FrozenMap(fields)


def _records(what: str, values: object, kind: str) -> tuple[FrozenMap, ...]:
    if not isinstance(values, tuple) or not all(
        isinstance(value, FrozenMap) and value.get("kind") == kind for value in values
    ):
        raise TypeError(f"machine {what} must be a list of {kind} records")
    return values


def _optional(what: str, value: object) -> Derivation | None:
    if value is not None and not isinstance(value, Derivation):
        raise TypeError(f"machine {what} must be a derivation")
    return value


def _strings(what: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not all(isinstance(value, str) for value in values):
        raise TypeError(f"machine {what} must be a list of strings")
    return values


def _provided(drvs: list[Derivation]) -> set[str]:
    return {path for drv in drvs for path in drv.output_paths.values()}


def machine(
    name: str,
    kernel: Derivation | None = None,
    initrd: Derivation | None = None,
    packages: tuple[Derivation, ...] = (),
    stacks: FrozenMap | None = None,
    users: tuple[FrozenMap, ...] = (),
    groups: tuple[FrozenMap, ...] = (),
    file_systems: tuple[FrozenMap, ...] = (),
    services: tuple[FrozenMap, ...] = (),
    etc: tuple[FrozenMap, ...] = (),
    host_name: str = "pyrixos",
    time_zone: str = "UTC",
    kernel_params: tuple[str, ...] = (),
    *,
    config: FrozenMap,
) -> Derivation:
    """Declare a whole system (RFC 0004 section 5.1).

    ``packages`` and ``stacks`` become the profile ``<name>-sw``. The
    records are validated together as a SystemSpec and stored as canonical
    JSON in ``env["system"]``; the kernel, the initial RAM disk, the
    profile and every ``etc_file`` source are inputs in ``env``. Every
    store path that a service or a user's shell names must be an output
    of a package or of a stack's contents, so the system holds what it
    runs.
    """
    if type(name) is not str:
        raise TypeError("machine name must be a string")
    kernel = _optional("kernel", kernel)
    initrd = _optional("initrd", initrd)
    sw = profile(f"{name}-sw", packages, stacks)
    etc_records = _records("etc", etc, "etc_file")
    sources = [record["source"] for record in etc_records if record["source"] is not None]
    try:
        spec = SystemSpec.model_validate(
            {
                "name": name,
                "host_name": host_name,
                "time_zone": time_zone,
                "kernel": kernel.store_path if kernel is not None else None,
                "initrd": initrd.store_path if initrd is not None else None,
                "kernel_params": _strings("kernel_params", kernel_params),
                "sw": sw.store_path,
                "users": plain(_records("users", users, "user")),
                "groups": plain(_records("groups", groups, "group")),
                "file_systems": plain(_records("file_systems", file_systems, "file_system")),
                "services": plain(_records("services", services, "service")),
                "etc": plain(etc_records),
                "config": plain(config),
            }
        )
    except ValidationError as error:
        raise ValueError(f"machine {name!r}: {describe(error)}") from None
    contents = [
        value
        for stack_drv in (stacks or FrozenMap()).values()
        if isinstance(stack_drv, Derivation)
        for value in stack_drv.env.values()
        if isinstance(value, Derivation)
    ]
    provided = _provided([*packages, *contents])
    used = [(f"service {s.name!r}", s.exec_start) for s in spec.services]
    used += [(f"user {u.name!r}", u.shell) for u in spec.users]
    for who, command in used:
        for path in STORE_REFERENCE.findall(command):
            if path not in provided:
                raise ValueError(
                    f"machine {name!r}: {who} runs {path}, which is not in the system's packages"
                )
    env: dict[str, object] = {"system": spec.canonical_json(), "sw": sw}
    if kernel is not None:
        env["kernel"] = kernel
    if initrd is not None:
        env["initrd"] = initrd
    for index, drv in enumerate(sources):
        env[f"etc_source_{index}"] = drv
    return derivation(name=name, builder=BUILTIN_SYSTEM, env=FrozenMap(env))
