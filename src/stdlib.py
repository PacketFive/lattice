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

from pybonsai import Builtin, FrozenMap, Host, make_host

from .derivation import (
    BUILTIN_FETCH_URL,
    BUILTIN_PROFILE,
    BUILTIN_STACK,
    Derivation,
    validate_fields,
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


def build_host() -> Host:
    """Return the Host Lattice evaluates every file with.

    Calls pybonsai.make_host with the prelude included, the four functions
    below as Builtins under their RFC names, and
    ``{Derivation: DERIVATION_ATTRIBUTES}`` as host types.
    """
    functions = {
        "derivation": derivation,
        "fetch_url": fetch_url,
        "stack": stack,
        "profile": profile,
    }
    return make_host(
        names={name: Builtin(name, impl) for name, impl in functions.items()},
        types={Derivation: DERIVATION_ATTRIBUTES},
    )


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
