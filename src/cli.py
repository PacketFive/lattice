# SPDX-License-Identifier: GPL-2.0
"""Command line entry point, installed as ``lattice``.

The subcommands are::

    lattice [--daemon | --remote URL] eval FILE.pyb
    lattice [--daemon | --remote URL] sandbox-config FILE.pyb [NAME]
    lattice [--daemon | --remote URL] backend-status
    lattice daemon

``eval`` evaluates the file and prints every top-level derivation with its
store path and canonical JSON. Nothing is built. ``sandbox-config`` prints
the OCI runtime configuration of the sandbox that would build derivation
NAME of the file, which may be left out when the file defines only one.
``backend-status`` prints whether each sandbox backend can run. ``daemon``
runs the daemon of RFC 0001 section 6.2 until interrupted. Later phases
add ``build``, ``gc``, ``switch``, ``push`` and ``materialise``.

Every subcommand calls one method of a LatticeAPI and only formats its
result, so the command and the library always behave the same way. The
API is Lattice by default, Client on the local daemon with ``--daemon``,
and Client on a remote daemon with ``--remote URL``, using ``--ca``,
``--cert`` and ``--key`` for TLS. ``--settings FILE`` names the settings
file read by Lattice and by ``lattice daemon``, by default
DEFAULT_SETTINGS_PATH when that file exists. A bearer token is read from the
``LATTICE_TOKEN`` environment variable, never from the command line,
where other users could see it in the process list.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from .api import Lattice
from .client import DEFAULT_URL, Client, LatticeAPI
from .derivation import canonical_json
from .errors import LatticeError
from .settings import DEFAULT_SETTINGS_PATH, Settings
from .transport import Daemon

# The name of the variable that holds the token, not a token.
TOKEN_VARIABLE = "LATTICE_TOKEN"  # noqa: S105


class UsageError(Exception):
    """Options that do not go together. main reports it and exits with 2."""


def load_settings(path: str | None) -> Settings:
    """Return Settings.load(path), or for no path the default file if it exists.

    A missing default file means default settings. A file named with
    ``--settings`` must exist.
    """
    if path is None:
        if not os.path.exists(DEFAULT_SETTINGS_PATH):
            return Settings()
        path = DEFAULT_SETTINGS_PATH
    return Settings.load(path)


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with one subparser per subcommand.

    Global options are ``--settings FILE``, ``--daemon``, ``--remote URL``,
    ``--ca FILE``, ``--cert FILE`` and ``--key FILE``; ``--daemon`` and
    ``--remote`` are mutually exclusive. ``eval`` takes a positional
    ``file`` argument, ``sandbox-config`` a ``file`` and an optional
    ``name``, and ``backend-status`` and ``daemon`` take no arguments.
    """
    parser = argparse.ArgumentParser(prog="lattice", description="The PyrixOS package manager.")
    parser.add_argument("--settings", metavar="FILE", help="settings file to read")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--daemon", action="store_true", help="use the local daemon")
    where.add_argument("--remote", metavar="URL", help="use the daemon at an https:// URL")
    parser.add_argument("--ca", metavar="FILE", help="CA certificates for --remote")
    parser.add_argument("--cert", metavar="FILE", help="client certificate for --remote")
    parser.add_argument("--key", metavar="FILE", help="client key for --remote")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    evaluate = commands.add_parser("eval", help="evaluate a file and print its derivations")
    evaluate.add_argument("file", help="PyBonsai file to evaluate")
    evaluate.set_defaults(handler=cmd_eval)
    config = commands.add_parser(
        "sandbox-config", help="print the OCI configuration of a derivation's sandbox"
    )
    config.add_argument("file", help="PyBonsai file to evaluate")
    config.add_argument("name", nargs="?", help="derivation to describe")
    config.set_defaults(handler=cmd_sandbox_config)
    status = commands.add_parser("backend-status", help="show which sandbox backends can run")
    status.set_defaults(handler=cmd_backend_status)
    daemon = commands.add_parser("daemon", help="run the daemon")
    daemon.set_defaults(handler=cmd_daemon)
    return parser


def make_api(args: argparse.Namespace) -> LatticeAPI:
    """Return Lattice(), Client() or Client(args.remote, ...) for the global options.

    Settings for Lattice come from load_settings. Settings are refused
    with ``--daemon`` and ``--remote``, since the daemon uses its own, and
    TLS options are refused without ``--remote``.
    """
    if args.remote is None and (args.ca or args.cert or args.key):
        raise UsageError("--ca, --cert and --key need --remote")
    if (args.daemon or args.remote is not None) and args.settings is not None:
        raise UsageError("--settings is not used with --daemon or --remote")
    if args.daemon:
        return Client()
    if args.remote is not None:
        return Client(
            args.remote,
            ca_file=args.ca,
            cert_file=args.cert,
            key_file=args.key,
            token=os.environ.get(TOKEN_VARIABLE) or None,
        )
    return Lattice(load_settings(args.settings))


def cmd_eval(args: argparse.Namespace) -> int:
    """Evaluate ``args.file`` with make_api(args).evaluate.

    For each entry of Evaluation.derivations, in assignment order, print
    ``<store path> <canonical JSON>``. Return 0 on success. On a
    LatticeError or OSError main prints the error to stderr and returns 1.
    """
    evaluation = make_api(args).evaluate(args.file)
    for drv in evaluation.derivations.values():
        print(drv.store_path, canonical_json(drv).decode())
    return 0


def cmd_sandbox_config(args: argparse.Namespace) -> int:
    """Print the OCI configuration of derivation ``args.name`` of ``args.file``.

    The name may be left out when the file defines one derivation. Return
    0 on success. An unknown or missing name is a LatticeError.
    """
    api = make_api(args)
    derivations = api.evaluate(args.file).derivations
    if args.name is None:
        if len(derivations) != 1:
            names = ", ".join(derivations) or "none"
            raise LatticeError(f"{args.file} defines several derivations ({names}); name one")
        drv = next(iter(derivations.values()))
    elif args.name in derivations:
        drv = derivations[args.name]
    else:
        raise LatticeError(f"{args.file} defines no derivation named {args.name!r}")
    print(api.sandbox_config(drv).model_dump_json(indent=2))
    return 0


def cmd_backend_status(args: argparse.Namespace) -> int:
    """Print one line per sandbox backend: its name, then available or the reason. Return 0."""
    for status in make_api(args).backend_status():
        print(f"{status.name}: {'available' if status.available else status.reason}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run Daemon(settings).serve_forever and return 0 when it is interrupted.

    ``--daemon`` and ``--remote`` are refused here, since a daemon does not
    forward to another daemon.
    """
    if args.daemon or args.remote is not None or args.ca or args.cert or args.key:
        raise UsageError("lattice daemon takes no --daemon, --remote or TLS options")
    Daemon(load_settings(args.settings)).serve_forever()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the selected subcommand and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except UsageError as error:
        parser.error(str(error))
    except LatticeError as error:
        print(f"lattice: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        where = error.filename
        if where is None and args.command != "daemon" and (args.daemon or args.remote):
            where = args.remote or DEFAULT_URL
        reason = error.strerror or str(error)
        print(f"lattice: {where}: {reason}" if where else f"lattice: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
