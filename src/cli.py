# SPDX-License-Identifier: GPL-2.0
"""Command line entry point, installed as ``lattice``.

The subcommands are::

    lattice [--daemon | --remote URL] eval [--package-set DIR] FILE.pyb
    lattice [--daemon | --remote URL] build [--check [--backend B]] [--package-set DIR]
        FILE.pyb [NAME]
    lattice [--daemon | --remote URL] check [--backend B] [--package-set DIR] FILE.pyb [NAME]
    lattice [--daemon | --remote URL] plan [--package-set DIR] FILE.pyb [NAME]
    lattice [--daemon | --remote URL] sandbox-config [--package-set DIR] FILE.pyb [NAME]
    lattice [--daemon | --remote URL] path-info PATH
    lattice [--daemon | --remote URL] verify PATH
    lattice [--daemon | --remote URL] roots
    lattice [--daemon | --remote URL] add-root NAME PATH
    lattice [--daemon | --remote URL] remove-root NAME
    lattice [--daemon | --remote URL] gc
    lattice [--daemon | --remote URL] switch [--profile P] [--boot | --test] [--package-set DIR]
        [FILE.pyb [NAME]]
    lattice [--daemon | --remote URL] rollback [--profile P] [GENERATION]
    lattice [--daemon | --remote URL] generations [--profile P]
    lattice [--daemon | --remote URL] delete-generations [--profile P] --keep N
    lattice [--daemon | --remote URL] slots
    lattice [--daemon | --remote URL] systems
    lattice [--daemon | --remote URL] backend-status
    lattice [--daemon | --remote URL] substitute [--shallow] PATH
    lattice [--daemon | --remote URL] materialise PATH
    lattice [--daemon | --remote URL] push --to URL PATH...
    lattice generate-key NAME FILE
    lattice config [ARGUMENTS...]
    lattice activate-boot [--system PATH]
    lattice export-registration PATH...
    lattice daemon

``eval`` evaluates the file and prints every top-level derivation with its
store path and canonical JSON. Nothing is built. The commands that take
FILE.pyb [NAME] act on derivation NAME of the file; NAME may be left out
when the file defines only one. ``build`` builds it and prints its output
paths, and ``build --check`` and ``check`` build it again and compare
(RFC 0001 section 16), on sandbox backend B if ``--backend`` names one.
``plan`` prints what a build would reuse, download or build, and
``sandbox-config`` the OCI runtime configuration of the sandbox that
would build it. ``path-info`` prints what the store database
records about PATH, and ``verify`` hashes PATH again and exits with 1 if
it changed. ``roots``, ``add-root`` and ``remove-root`` list and change
the roots collection keeps, and ``gc`` collects garbage. With
``--package-set``, a file also sees the public names of the package set
in DIR (RFC 0005 section 5.2).

``switch`` builds the derivation and makes it the next generation of
profile P, ``system`` by default, and ``rollback`` makes an earlier
generation current again (RFC 0001 section 13). When the derivation is a
system declared with ``machine`` (RFC 0004), ``switch`` activates the whole
system: ``--boot`` makes it the default from the next boot only, and
``--test`` activates it now without a generation. Without FILE, ``switch``
uses derivation ``system`` of the system description named in the
settings, ``/etc/pyrixos/system.pyb`` by default. Without
``--package-set``, ``switch`` evaluates the file with the package set of
the settings, ``/etc/pyrixos/pkgs`` by default, when that directory
exists. ``systems`` lists the
system generations, marking the current and the booted one. ``generations`` lists the
generations of P, marking the current one, and ``delete-generations``
deletes all but the newest N and the current one. ``slots`` prints the
active and wanted stack of each slot. ``activate-boot`` loads the kernel
modules and creates the slots of the current system generation; it runs
at boot and always uses the library, never a daemon. With ``--system``,
as the ``init`` of a system runs it, it first links ``/run/booted-system``,
``/run/current-system`` and ``/etc`` to that system and writes its
accounts. Before that, on the first boot of a disk image, it registers
the store paths listed in ``registration.json`` (RFC 0005 section 8.2).
``export-registration`` prints that file for the closure of PATH, using
the library like ``activate-boot``.

``backend-status`` prints whether each sandbox backend can run. ``daemon``
runs the daemon of RFC 0001 section 6.2 until interrupted.

``substitute`` fetches PATH and its missing references from the
configured binary caches and prints each path it registered; with
``--shallow`` PATH may become a link into a CernVM-FS cache.
``materialise`` turns a shallow path into a full copy. ``push`` signs and
publishes each PATH with its closure and provenance to the cache at URL
and prints each path it published. ``generate-key`` writes a new secret
signing key called NAME to FILE, readable by its owner only, and prints
the public key to list in ``trusted_keys``; it always runs locally
(RFC 0001 section 14).

``config`` runs the Trellis configuration command with ARGUMENTS, so
``lattice config menu --kconfig Kconfig`` is ``trellis menu --kconfig
Kconfig``. It always runs locally and needs the trellis package.

Build progress, logs, substitutions and activation steps go to stderr.

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
import importlib
import os
import sys
from collections.abc import Sequence

from .api import Lattice
from .client import DEFAULT_URL, Client, LatticeAPI
from .derivation import BUILTIN_SYSTEM, Derivation, canonical_json
from .errors import LatticeError
from .events import (
    ActivationDeferred,
    ActivationNotice,
    ActivationStep,
    BuildLog,
    BuildStarted,
    CacheSkipped,
    Event,
    KernelModuleLoaded,
    KernelModuleUnloaded,
    PathPushed,
    SlotChanged,
    SubstitutionStarted,
)
from .models import Generation, SlotState, SystemGeneration, SystemMode
from .settings import DEFAULT_SETTINGS_PATH, SANDBOX_BACKENDS, Settings, SystemSettings
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
    _package_set_argument(evaluate)
    evaluate.set_defaults(handler=cmd_eval)
    build = commands.add_parser("build", help="build a derivation and print its outputs")
    _derivation_arguments(build)
    build.add_argument("--check", action="store_true", help="rebuild and compare instead")
    _backend_argument(build)
    build.set_defaults(handler=cmd_build)
    check = commands.add_parser("check", help="rebuild a derivation and compare the outputs")
    _derivation_arguments(check)
    _backend_argument(check)
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
    modes = switch.add_mutually_exclusive_group()
    modes.add_argument("--boot", action="store_true", help="activate a system at the next boot")
    modes.add_argument("--test", action="store_true", help="activate a system until reboot")
    switch.add_argument("file", nargs="?", help="PyBonsai file (the system description)")
    switch.add_argument("name", nargs="?", help="derivation of the file")
    _package_set_argument(switch)
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
    systems = commands.add_parser("systems", help="list the system generations")
    systems.set_defaults(handler=cmd_systems)
    boot = commands.add_parser("activate-boot", help="load modules and create slots at boot")
    boot.add_argument("--system", metavar="PATH", help="the system the machine booted")
    boot.set_defaults(handler=cmd_activate_boot)
    export = commands.add_parser(
        "export-registration", help="print registration.json for the closure of store paths"
    )
    export.add_argument("paths", nargs="+", metavar="PATH", help="store path")
    export.set_defaults(handler=cmd_export_registration)
    status = commands.add_parser("backend-status", help="show which sandbox backends can run")
    status.set_defaults(handler=cmd_backend_status)
    substitute = commands.add_parser("substitute", help="fetch a store path from the caches")
    substitute.add_argument("--shallow", action="store_true", help="allow a shallow path")
    substitute.add_argument("path", help="store path")
    substitute.set_defaults(handler=cmd_substitute)
    materialise = commands.add_parser("materialise", help="turn a shallow path into a copy")
    materialise.add_argument("path", help="store path")
    materialise.set_defaults(handler=cmd_materialise)
    push = commands.add_parser("push", help="publish store paths to a binary cache")
    push.add_argument("--to", required=True, metavar="URL", help="cache to publish to")
    push.add_argument("paths", nargs="+", metavar="PATH", help="store path")
    push.set_defaults(handler=cmd_push)
    key = commands.add_parser("generate-key", help="create a cache signing key")
    key.add_argument("name", help="key name, for example cache.example.org-1")
    key.add_argument("file", help="new file for the secret key")
    key.set_defaults(handler=cmd_generate_key)
    configure = commands.add_parser(
        "config", help="run the Trellis configuration command", add_help=False
    )
    configure.add_argument("arguments", nargs=argparse.REMAINDER, help="Trellis arguments")
    configure.set_defaults(handler=cmd_config)
    daemon = commands.add_parser("daemon", help="run the daemon")
    daemon.set_defaults(handler=cmd_daemon)
    return parser


def _backend_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        choices=SANDBOX_BACKENDS,
        help="rebuild on this sandbox backend instead of the configured one",
    )


def _derivation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="PyBonsai file to evaluate")
    parser.add_argument("name", nargs="?", help="derivation of the file")
    _package_set_argument(parser)


def _package_set_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--package-set", metavar="DIR", help="evaluate with the package set in this directory"
    )


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
    elif isinstance(event, ActivationStep):
        line = f"activating: {event.step}"
    elif isinstance(event, ActivationNotice):
        line = f"note: {event.text}"
    elif isinstance(event, ActivationDeferred):
        line = f"slot {event.slot} changes at the next boot: {event.reason}"
    elif isinstance(event, SlotChanged):
        line = f"slot {event.slot}: {event.new}" if event.new else f"slot {event.slot}: removed"
    elif isinstance(event, KernelModuleLoaded):
        line = f"loaded kernel module {event.name}"
    elif isinstance(event, KernelModuleUnloaded):
        line = f"unloaded kernel module {event.name}"
    elif isinstance(event, SubstitutionStarted):
        line = f"fetching {event.path} from {event.cache}"
    elif isinstance(event, CacheSkipped):
        line = f"skipped {event.cache}: {event.reason}"
    elif isinstance(event, PathPushed):
        line = f"pushed {event.path} to {event.cache}"
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
    evaluation = make_api(args).evaluate(args.file, args.package_set)
    for drv in evaluation.derivations.values():
        print(drv.store_path, canonical_json(drv).decode())
    return 0


def select(
    api: LatticeAPI, file: str, name: str | None, package_set: str | None = None
) -> Derivation:
    """Evaluate ``file`` with ``package_set`` and return its derivation ``name``.

    The name may be None when the file defines one derivation. An unknown
    or missing name is a LatticeError.
    """
    derivations = api.evaluate(file, package_set).derivations
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
    if args.backend is not None:
        raise LatticeError("--backend applies only with --check")
    api = make_api(args)
    result = api.build(select(api, args.file, args.name, args.package_set))
    for path in result.outputs.values():
        print(path)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Rebuild derivation ``args.name`` of ``args.file`` and compare. Return 0 if the same."""
    api = make_api(args)
    result = api.check(select(api, args.file, args.name, args.package_set), args.backend)
    print(f"{result.store_path}: rebuilt with the same contents")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Print one line per step of the build: what happens to it and its store path."""
    api = make_api(args)
    for step in api.plan(select(api, args.file, args.name, args.package_set)).steps:
        print(f"{step.action:<8} {step.outputs[next(iter(step.outputs))]}")
    return 0


def cmd_sandbox_config(args: argparse.Namespace) -> int:
    """Print the OCI configuration of derivation ``args.name`` of ``args.file``. Return 0."""
    api = make_api(args)
    print(
        api.sandbox_config(select(api, args.file, args.name, args.package_set)).model_dump_json(
            indent=2
        )
    )
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


def _print_system(system: SystemGeneration) -> None:
    marks = [m for m, on in (("current", system.current), ("booted", system.booted)) if on]
    mark = f" ({', '.join(marks)})" if marks else ""
    print(f"{system.number} {system.name} {system.store_path}{mark}")


def cmd_switch(args: argparse.Namespace) -> int:
    """Build derivation ``args.name`` of ``args.file`` and make it current. Return 0.

    Without a file, the system description of the settings and its
    derivation ``system`` are used. A system goes to switch_system in the
    mode ``--boot`` or ``--test`` choose.
    """
    api = make_api(args)
    file, name, package_set = args.file, args.name, args.package_set
    local = not args.daemon and args.remote is None
    system = load_settings(args.settings).system if local else SystemSettings()
    if file is None:
        file, name = system.description, "system"
    if package_set is None and os.path.isdir(system.package_set):
        package_set = system.package_set
    drv = select(api, file, name, package_set)
    mode: SystemMode = "boot" if args.boot else "test" if args.test else "switch"
    if drv.builder == BUILTIN_SYSTEM:
        if args.profile != "system":
            raise LatticeError("a system can only be switched in the system profile")
        _print_system(api.switch_system(drv, mode))
    elif mode != "switch":
        raise LatticeError("--boot and --test apply only to a system")
    else:
        _print_generation(api.switch(args.profile, drv))
    return 0


def cmd_systems(args: argparse.Namespace) -> int:
    """Print each system generation, marking the current and the booted one. Return 0."""
    for system in make_api(args).systems():
        _print_system(system)
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
    lattice = Lattice(load_settings(args.settings), on_event=progress)
    _print_slots(lattice.activate_boot(args.system))
    return 0


def cmd_export_registration(args: argparse.Namespace) -> int:
    """Print Lattice.export_registration for ``args.paths``. Return 0.

    The library is always used, like ``activate-boot``.
    """
    if args.daemon or args.remote is not None or args.ca or args.cert or args.key:
        raise UsageError("lattice export-registration takes no --daemon, --remote or TLS options")
    lattice = Lattice(load_settings(args.settings), on_event=progress)
    sys.stdout.buffer.write(lattice.export_registration(args.paths))
    sys.stdout.flush()
    return 0


def cmd_backend_status(args: argparse.Namespace) -> int:
    """Print one line per sandbox backend: its name, then available or the reason. Return 0."""
    for status in make_api(args).backend_status():
        print(f"{status.name}: {'available' if status.available else status.reason}")
    return 0


def cmd_substitute(args: argparse.Namespace) -> int:
    """Fetch ``args.path`` and print each path registered. Return 0."""
    for path in make_api(args).substitute(args.path, args.shallow).substituted:
        print(path)
    return 0


def cmd_materialise(args: argparse.Namespace) -> int:
    """Turn the shallow path ``args.path`` into a full copy. Return 0."""
    info = make_api(args).materialise(args.path)
    print(f"{info.path}: {info.location}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Publish ``args.paths`` to ``args.to`` and print each path published. Return 0."""
    result = make_api(args).push(args.paths, args.to)
    for path in result.pushed:
        print(path)
    print(f"pushed {len(result.pushed)} store paths, {len(result.skipped)} already in the cache")
    return 0


def cmd_generate_key(args: argparse.Namespace) -> int:
    """Write a new secret key to ``args.file`` and print its public key. Return 0."""
    if args.daemon or args.remote is not None or args.ca or args.cert or args.key:
        raise UsageError("lattice generate-key always runs locally")
    from .cache.signing import generate_key, write_secret_key

    try:
        key = generate_key(args.name)
    except ValueError as error:
        raise UsageError(str(error)) from None
    write_secret_key(args.file, key)
    print(key.public())
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


def cmd_config(args: argparse.Namespace) -> int:
    """Run ``trellis`` with ``args.arguments`` and return its exit status."""
    if args.daemon or args.remote is not None or args.ca or args.cert or args.key:
        raise UsageError("lattice config always runs locally")
    try:
        trellis = importlib.import_module("trellis.cli")
    except ImportError:
        raise UsageError("Trellis is not installed; install the trellis package") from None
    status: int = trellis.main(args.arguments)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the selected subcommand and return its exit code."""
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "config":
        args.arguments = [*extra, *args.arguments]
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
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
