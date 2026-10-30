# Changelog

All notable changes to Lattice are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) while the major version is 0. Lattice reaches 1.0.0 with
PyrixOS 1.0.

## [Unreleased]

## [0.11.0] - 2026-10-30

Phase 10 of the PyrixOS development plan, system configuration
(RFC 0004).

### Added

- The standard library functions `machine`, `user`, `group`,
  `file_system`, `service` and `etc_file`, and the builder
  `builtin:system`, which writes a whole system into one store path.
- `Lattice.switch_system`, `rollback_system` and `systems`, with
  `lattice switch [--boot | --test]`, `lattice systems` and
  `lattice rollback --profile system`. Activation links `/etc` through
  `/etc/static`, writes `passwd`, `group` and `shadow`, mounts new file
  systems, moves slots, restarts changed systemd units and writes a
  systemd-boot entry for each generation.
- `lattice activate-boot --system PATH`, run by the `init` of a system.
- `config.pyb` next to a system description is read as `config`.
- The `[system]` settings `description` and `root`, and
  `SystemManager`, `SystemdManager` and `OfflineManager`.
- The sandbox settings `max_memory_mib`, `max_cpu` and
  `max_processes`, applied through Cloche cgroups, and a `cgroups` line
  in `lattice backend-status`.

### Changed

- Requires PyBonsai 1.1 and Cloche 1.1.
- Deleting generations of `system` also removes their boot entries and
  unused kernel images.

## [0.10.0] - 2026-10-30

### Added

- The standard library function `unpack` and the builder
  `builtin:unpack`, which unpack a tar archive into the store outside
  the sandbox (RFC 0002 Bootstrap).

## [0.9.0] - 2026-10-29

First release, published on PyPI as `pyrixos-lattice`. It contains
Phases 1 to 7 of the PyrixOS development plan.

### Added

- Derivations from PyBonsai files, the Lattice standard library, the
  `Lattice` library interface, the settings file, and `lattice eval`.
- `lattice daemon`, with JSON-RPC and REST over its Unix socket and TLS,
  and the `Client` that calls it.
- Builds in the Cloche sandbox on the `namespaces`, `oci` and `microvm`
  backends, `lattice sandbox-config` and `lattice backend-status`.
- The store: atomic read-only publication, the SQLite database, parallel
  builds, `fetch_url`, `lattice check` and `lattice gc`.
- Profiles, generations, rollback, stacks and slots, with kernel module
  checks and deferral to the next boot.
- Binary caches over HTTP, directories and CernVM-FS, with signatures,
  SLSA provenance, shallow paths and `lattice materialise`.
- `lattice config`, which runs Trellis when the `config` extra is
  installed.
