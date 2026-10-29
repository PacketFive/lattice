# Changelog

All notable changes to Lattice are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) while the major version is 0. Lattice reaches 1.0.0 with
PyrixOS 1.0.

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
