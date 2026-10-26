# SPDX-License-Identifier: GPL-2.0
"""The store, its database and garbage collection (RFC 0001 sections 10 to 12).

layout.py names the files of a store and lock.py its locks. database.py
records which paths are registered and what they refer to. verify.py
hashes the contents of a path, references.py finds the store paths a
file names, realise.py publishes build outputs and fetch.py downloads
the files of ``fetch_url``. builder.py builds derivations in dependency
order and gc.py removes paths that nothing keeps.
"""

from __future__ import annotations

from .builder import Builder, BuildPlan, PlannedStep
from .database import GCRootInfo, Registration, StorePathInfo, check_root_name
from .fetch import FetchResult
from .gc import collect
from .layout import StoreLayout, check_store_path
from .verify import VerifyResult, content_hash, diff_trees

__all__ = [
    "BuildPlan",
    "Builder",
    "FetchResult",
    "GCRootInfo",
    "PlannedStep",
    "Registration",
    "StoreLayout",
    "StorePathInfo",
    "VerifyResult",
    "check_root_name",
    "check_store_path",
    "collect",
    "content_hash",
    "diff_trees",
]
