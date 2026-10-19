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

from pybonsai import FrozenMap, Host

from .derivation import Derivation

# RFC 0001 section 7.5, derivation row.
DERIVATION_ATTRIBUTES: frozenset[str] = frozenset(
    {"name", "builder", "args", "env", "outputs", "hash"}
)


def build_host() -> Host:
    """Return the Host Lattice evaluates every file with.

    Calls pybonsai.make_host with the prelude included, the four functions
    below as Builtins under their RFC names, and
    ``{Derivation: DERIVATION_ATTRIBUTES}`` as host types.
    """
    raise NotImplementedError


def derivation(
    name: str,
    builder: str,
    args: tuple[str, ...] = (),
    env: FrozenMap | None = None,
    outputs: tuple[str, ...] = ("out",),
) -> Derivation:
    """Validate the fields with derivation.validate_fields and return a Derivation."""
    raise NotImplementedError


def fetch_url(url: str, sha256: str) -> Derivation:
    """Declare a fixed-output source. No network access happens here.

    ``sha256`` must be 64 lowercase hex characters. Returns a Derivation
    whose builder is BUILTIN_FETCH_URL, whose ``env`` holds ``url`` and
    ``sha256``, and whose name is the last path segment of the URL.
    """
    raise NotImplementedError


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
