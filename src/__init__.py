# SPDX-License-Identifier: GPL-2.0
"""Lattice, the PyrixOS package manager.

Lattice evaluates PyBonsai files with the ``pybonsai`` package, passing
its own standard library as the host (RFC 0001 sections 7.6 and 8).

Phase 1 provides derivations and the standard library. Later phases add
the build sandbox, the store, the dependency database, the garbage
collector, profiles and binary caches as modules of this package.
"""

__version__ = "0.0.0"
