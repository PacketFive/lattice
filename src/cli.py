# SPDX-License-Identifier: GPL-2.0
"""Command line entry point, installed as ``lattice``.

The subcommands are::

    lattice [--daemon | --remote URL] eval FILE.pyb
    lattice [--daemon | --remote URL] build [--check] FILE.pyb [NAME]
    lattice [--daemon | --remote URL] check FILE.pyb [NAME]
    lattice [--daemon | --remote URL] plan FILE.pyb [NAME]
    lattice [--daemon | --remote URL] sandbox-config FILE.pyb [NAME]
    lattice [--daemon | --remote URL] path-info PATH
    lattice [--daemon | --remote URL] verify PATH
    lattice [--daemon | --remote URL] roots
    lattice [--daemon | --remote URL] add-root NAME PATH
    lattice [--daemon | --remote URL] remove-root NAME
    lattice [--daemon | --remote URL] gc
    lattice [--daemon | --remote URL] switch [--profile P] FILE.pyb [NAME]
    lattice [--daemon | --remote URL] rollback [--profile P] [GENERATION]
    lattice [--daemon | --remote URL] generations [--profile P]
    lattice [--daemon | --remote URL] delete-generations [--profile P] --keep N
    lattice [--daemon | --remote URL] slots
    lattice [--daemon | --remote URL] backend-status
    lattice activate-boot
    lattice daemon

``eval`` evaluates the file and prints every top-level derivation with its
store path and canonical JSON. Nothing is built. The commands that take
FILE.pyb [NAME] act on derivation NAME of the file; NAME may be left out
when the file defines only one. ``build`` builds it and prints its output
paths, and ``build --check`` and ``check`` build it again and compare
(RFC 0001 section 16). ``plan`` prints what a build would reuse, download
or build, and ``sandbox-config`` the OCI runtime configuration of the
sandbox that would build it. ``path-info`` prints what the store database
records about PATH, and ``verify`` hashes PATH again and exits with 1 if
it changed. ``roots``, ``add-root`` and ``remove-root`` list and change
the roots collection keeps, and ``gc`` collects garbage.

``switch`` builds the derivation and makes it the next generation of
profile P, ``system`` by default, and ``rollback`` makes an earlier
generation current again (RFC 0001 section 13). ``generations`` lists the
generations of P, marking the current one, and ``delete-generations``
deletes all but the newest N and the current one. ``slots`` prints the
active and wanted stack of each slot. ``activate-boot`` loads the kernel
modules and creates the slots of the current system generation; it runs
at boot and always uses the library, never a daemon.

``backend-status`` prints whether each sandbox backend can run. ``daemon``
runs the daemon of RFC 0001 section 6.2 until interrupted. Later phases
add ``push`` and ``materialise``.

Build progress, logs and activation steps go to stderr.

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
from .derivation import Derivation, canonical_json
from .errors import LatticeError
from .events import (
    ActivationDeferred,
    BuildLog,
    BuildStarted,
    Event,
    KernelModuleLoaded,
    KernelModuleUnloaded,
    SlotChanged,
)
from .models import Generation, SlotState
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
    ``--remote`` are mutually exclusive. The arguments of each
    subcommand are those in the module docstring.
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
    build = commands.add_parser("build", help="build a derivation and print its outputs")
    _derivation_arguments(build)
    build.add_argument("--check", action="store_true", help="rebuild and compare instead")
    build.set_defaults(handler=cmd_build)
    check = commands.add_parser("check", help="rebuild a derivation and compare the outputs")
    _derivation_arguments(check)
    check.set_defaults(handler=cmd_check)
    plan = commands.add_parser("plan", help="show what building a derivation would do")
    _derivation_arguments(plan)
    plan.set_defaults(handler=cmd_plan)
    config = commands.add_parser(
        "sandbox-config", help="print the OCI configuration of a derivation's sandbox"
    )
    _derivation_arguments(config)
    config.set_defaults(handler=cmd_sandbox_config)
    info = commands.add_parser("path-info", help="show what the store records about a path")
    info.add_argument("path", help="store path")
    info.set_defaults(handler=cmd_path_info)
    verify = commands.add_parser("verify", help="check a store path against its hash")
    verify.add_argument("path", help="store path")
    verify.set_defaults(handler=cmd_verify)
    roots = commands.add_parser("roots", help="list the roots collection keeps")
    roots.set_defaults(handler=cmd_roots)
    add_root = commands.add_parser("add-root", help="keep a store path under a root")
    add_root.add_argument("name", help="root name, for example per-user/alice/hello")
    add_root.add_argument("path", help="store path")
    add_root.set_defaults(handler=cmd_add_root)
    remove_root = commands.add_parser("remove-root", help="remove a root")
    remove_root.add_argument("name", help="root name")
    remove_root.set_defaults(handler=cmd_remove_root)
    gc = commands.add_parser("gc", help="remove store paths that nothing keeps")
    gc.set_defaults(handler=cmd_gc)
    switch = commands.add_parser("switch", help="build a derivation and make it current")
    _profile_argument(switch)
    _derivation_arguments(switch)
    switch.set_defaults(handler=cmd_switch)
    rollback = commands.add_parser("rollback", help="make an earlier generation current")
    _profile_argument(rollback)
    rollback.add_argument("generation", nargs="?", type=int, help="generation number")
    rollback.set_defaults(handler=cmd_rollback)
    generations = commands.add_parser("generations", help="list the generations of a profile")
    _profile_argument(generations)
    generations.set_defaults(handler=cmd_generations)
    delete = commands.add_parser("delete-generations", help="delete old generations")
    _profile_argument(delete)
    delete.add_argument("--keep", type=int, required=True, metavar="N", help="newest to keep")
    delete.set_defaults(handler=cmd_delete_generations)
    slots = commands.add_parser("slots", help="show the active and wanted stack of each slot")
    slots.set_defaults(handler=cmd_slots)
    boot = commands.add_parser("activate-boot", help="load modules and create slots at boot")
    boot.set_defaults(handler=cmd_activate_boot)
    status = commands.add_parser("backend-status", help="show which sandbox backends can run")
    status.set_defaults(handler=cmd_backend_status)
    daemon = commands.add_parser("daemon", help="run the daemon")
    daemon.set_defaults(handler=cmd_daemon)
    return parser


def _derivation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="PyBonsai file to evaluate")
    parser.add_argument("name", nargs="?", help="derivation of the file")


def _profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default="system", metavar="P", help="profile (system)")


def progress(event: Event) -> None:
    """Print build starts, build logs and activation steps to stderr."""
    line = None
    if isinstance(event, BuildStarted):
        line = f"building {event.name} ({event.store_path})"
    elif isinstance(event, BuildLog):
        sys.stderr.write(event.text)
        sys.stderr.flush()
    elif isinstance(event, ActivationDeferred):
        line = f"slot {event.slot} changes at the next boot: {event.reason}"
    elif isinstance(event, SlotChanged):
        line = f"slot {event.slot}: {event.new}" if event.new else f"slot {event.slot}: removed"
    elif isinstance(event, KernelModuleLoaded):
        line = f"loaded kernel module {event.name}"
    elif isinstance(event, KernelModuleUnloaded):
        line = f"unloaded kernel module {event.name}"
    if line is not None:
        print(line, file=sys.stderr, flush=True)


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
        return Client(on_event=progress)
    if args.remote is not None:
        return Client(
            args.remote,
            ca_file=args.ca,
            cert_file=args.cert,
            key_file=args.key,
            token=os.environ.get(TOKEN_VARIABLE) or None,
            on_event=progress,
        )
    return Lattice(load_settings(args.settings), on_event=progress)


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


def select(api: LatticeAPI, file: str, name: str | None) -> Derivation:
    """Evaluate ``file`` and return its derivation ``name``.

    The name may be None when the file defines one derivation. An unknown
    or missing name is a LatticeError.
    """
    derivations = api.evaluate(file).derivations
    if name is None:
        if len(derivations) != 1:
            names = ", ".join(derivations) or "none"
            raise LatticeError(f"{file} defines several derivations ({names}); name one")
        return next(iter(derivations.values()))
    if name in derivations:
        return derivations[name]
    raise LatticeError(f"{file} defines no derivation named {name!r}")


def cmd_build(args: argparse.Namespace) -> int:
    """Build derivation ``args.name`` of ``args.file`` and print each output path.

    With ``--check`` run cmd_check instead. Return 0 on success.
    """
    if args.check:
        return cmd_check(args)
    api = make_api(args)
    result = api.build(select(api, args.file, args.name))
    for path in result.outputs.values():
        print(path)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Rebuild derivation ``args.name`` of ``args.file`` and compare. Return 0 if the same."""
    api = make_api(args)
    result = api.check(select(api, args.file, args.name))
    print(f"{result.store_path}: rebuilt with the same contents")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Print one line per step of the build: what happens to it and its store path."""
    api = make_api(args)
    for step in api.plan(select(api, args.file, args.name)).steps:
        print(f"{step.action:<8} {step.outputs[next(iter(step.outputs))]}")
    return 0


def cmd_sandbox_config(args: argparse.Namespace) -> int:
    """Print the OCI configuration of derivation ``args.name`` of ``args.file``. Return 0."""
    api = make_api(args)
    print(api.sandbox_config(select(api, args.file, args.name)).model_dump_json(indent=2))
    return 0


def cmd_path_info(args: argparse.Namespace) -> int:
    """Print what the store records about ``args.path`` as JSON. Return 0."""
    print(make_api(args).path_info(args.path).model_dump_json(indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Hash ``args.path`` again. Return 0 if it matches the database and 1 if not."""
    result = make_api(args).verify(args.path)
    if result.ok:
        print(f"{result.path}: ok")
        return 0
    print(
        f"{result.path}: changed: SHA-256 {result.actual_sha256}, {result.actual_size} bytes;"
        f" expected {result.expected_sha256}, {result.expected_size} bytes"
    )
    return 1


def cmd_roots(args: argparse.Namespace) -> int:
    """Print each root as ``<name> <store path>``. Return 0."""
    for root in make_api(args).roots():
        print(root.name, root.path)
    return 0


def cmd_add_root(args: argparse.Namespace) -> int:
    """Make ``args.path`` the root ``args.name``. Return 0."""
    root = make_api(args).add_root(args.name, args.path)
    print(root.name, root.path)
    return 0


def cmd_remove_root(args: argparse.Namespace) -> int:
    """Remove the root ``args.name``. Return 0."""
    make_api(args).remove_root(args.name)
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """Collect garbage, print each removed path and the bytes freed. Return 0."""
    result = make_api(args).gc()
    for path in result.removed:
        print(path)
    print(f"removed {len(result.removed)} store paths, freed {result.freed_bytes} bytes")
    return 0


def _print_generation(generation: Generation) -> None:
    mark = " (current)" if generation.current else ""
    print(f"{generation.number} {generation.store_path}{mark}")


def cmd_switch(args: argparse.Namespace) -> int:
    """Build derivation ``args.name`` of ``args.file`` and make it current. Return 0."""
    api = make_api(args)
    _print_generation(api.switch(args.profile, select(api, args.file, args.name)))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Make generation ``args.generation``, or the previous one, current. Return 0."""
    _print_generation(make_api(args).rollback(args.profile, args.generation))
    return 0


def cmd_generations(args: argparse.Namespace) -> int:
    """Print each generation as ``<number> <store path>``, marking the current one. Return 0."""
    for generation in make_api(args).generations(args.profile):
        _print_generation(generation)
    return 0


def cmd_delete_generations(args: argparse.Namespace) -> int:
    """Delete old generations and print each one deleted. Return 0."""
    for generation in make_api(args).delete_generations(args.profile, args.keep):
        _print_generation(generation)
    return 0


def _print_slots(api_slots: tuple[SlotState, ...]) -> None:
    for state in api_slots:
        line = f"{state.slot}: active {state.active or 'none'}"
        if state.wanted != state.active:
            line += f", next boot {state.wanted or 'none'}"
        print(line)


def cmd_slots(args: argparse.Namespace) -> int:
    """Print each slot with its active stack, and its wanted stack if that differs. Return 0."""
    _print_slots(make_api(args).slots())
    return 0


def cmd_activate_boot(args: argparse.Namespace) -> int:
    """Run Lattice.activate_boot and print the slots. Return 0.

    The library is always used, since boot runs before the daemon starts.
    """
    if args.daemon or args.remote is not None or args.ca or args.cert or args.key:
        raise UsageError("lattice activate-boot takes no --daemon, --remote or TLS options")
    _print_slots(Lattice(load_settings(args.settings), on_event=progress).activate_boot())
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
