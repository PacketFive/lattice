# SPDX-License-Identifier: GPL-2.0
"""The Lattice standard library (RFC 0001 section 7.6).

A PyBonsai file evaluated by Lattice can call the pybonsai prelude (``len``,
``range``, ``sorted`` and the other pure helpers) and the functions defined
here. build_host() combines both into the pybonsai.Host that Lattice passes
to the evaluator, and registers Derivation as a host type with the
attributes listed in RFC 0001 section 7.5.

Each function returns the same result for the same arguments and reads or
changes nothing outside the evaluator.

``stack`` and ``profile`` are declared because they are part of the
language, but they belong to Phase 4. They first need the RFC to say how a
tuple of derivations is recorded in a derivation's ``env``.
"""

from __future__ import annotations

import re
import urllib.parse

from pybonsai import Builtin, FrozenMap, Host, make_host

from .derivation import BUILTIN_FETCH_URL, Derivation, validate_fields

# RFC 0001 section 7.5, derivation row.
DERIVATION_ATTRIBUTES: frozenset[str] = frozenset(
    {"name", "builder", "args", "env", "outputs", "hash"}
)

_SHA256 = re.compile(r"[0-9a-f]{64}")

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


def stack(
    slot: str,
    name: str,
    contents: tuple[Derivation, ...],
    kernel_modules: tuple[Derivation, ...] = (),
) -> Derivation:
    """Combine packages into one stack for ``slot`` (section 13.2). Phase 4."""
    raise NotImplementedError


def profile(
    name: str,
    packages: tuple[Derivation, ...],
    stacks: FrozenMap | None = None,
) -> Derivation:
    """Build the environment of one profile generation (section 13.1). Phase 4."""
    raise NotImplementedError
