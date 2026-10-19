# SPDX-License-Identifier: GPL-2.0
"""Command line entry point, installed as ``lattice``.

Phase 1 has one subcommand::

    lattice eval FILE.pyb

It evaluates the file and prints every top-level derivation with its store
path and canonical JSON. Nothing is built. Later phases add ``build``,
``gc``, ``switch``, ``push`` and ``materialise``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with one subparser per subcommand.

    Phase 1 defines ``eval`` with a positional ``file`` argument.
    """
    raise NotImplementedError


def cmd_eval(args: argparse.Namespace) -> int:
    """Evaluate ``args.file`` with pybonsai.evaluate_file and stdlib.build_host().

    For each top-level name bound to a Derivation, in assignment order,
    print ``<store path> <canonical JSON>``. Return 0 on success. On a
    PyBonsaiError print the error to stderr and return 1.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the selected subcommand and return its exit code."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
