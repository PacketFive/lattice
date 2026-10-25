# SPDX-License-Identifier: GPL-2.0
"""Command line entry point, installed as ``lattice``.

Phase 1 has two subcommands::

    lattice [--daemon | --remote URL] eval FILE.pyb
    lattice daemon

``eval`` evaluates the file and prints every top-level derivation with its
store path and canonical JSON. Nothing is built. ``daemon`` runs the
daemon of RFC 0001 section 6.2 until interrupted. Later phases add
``build``, ``gc``, ``switch``, ``push`` and ``materialise``.

Every subcommand calls one method of a LatticeAPI and only formats its
result, so the command and the library always behave the same way. The
API is Lattice by default, Client on the local daemon with ``--daemon``,
and Client on a remote daemon with ``--remote URL``, using ``--ca``,
``--cert`` and ``--key`` for TLS. A bearer token is read from the
``LATTICE_TOKEN`` environment variable, never from the command line,
where other users could see it in the process list.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .client import LatticeAPI


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with one subparser per subcommand.

    Global options are ``--daemon``, ``--remote URL``, ``--ca FILE``,
    ``--cert FILE`` and ``--key FILE``; ``--daemon`` and ``--remote`` are
    mutually exclusive. Phase 1 defines ``eval`` with a positional
    ``file`` argument, and ``daemon`` with no arguments.
    """
    raise NotImplementedError


def make_api(args: argparse.Namespace) -> LatticeAPI:
    """Return Lattice(), Client() or Client(args.remote, ...) for the global options.

    Settings for Lattice come from Settings.load once its format is
    defined, and are the defaults until then.
    """
    raise NotImplementedError


def cmd_eval(args: argparse.Namespace) -> int:
    """Evaluate ``args.file`` with make_api(args).evaluate.

    For each entry of Evaluation.derivations, in assignment order, print
    ``<store path> <canonical JSON>``. Return 0 on success. On a
    LatticeError print the error to stderr and return 1.
    """
    raise NotImplementedError


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run Daemon(settings).serve_forever and return 0 when it is interrupted.

    ``--daemon`` and ``--remote`` are refused here, since a daemon does not
    forward to another daemon.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the selected subcommand and return its exit code."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
