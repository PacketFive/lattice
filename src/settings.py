# SPDX-License-Identifier: GPL-2.0
"""Machine settings for the Lattice library (RFC 0001 section 6.1).

Settings are passed to Lattice explicitly. Nothing is read from the
machine unless the program calls Settings.load. Evaluation limits are not
settings: section 7.7 fixes them per release so that a file evaluates the
same way on every machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, get_args

from .errors import SettingsError

SandboxBackend = Literal["namespaces", "oci", "microvm"]
SANDBOX_BACKENDS: tuple[str, ...] = get_args(SandboxBackend)


@dataclass(frozen=True)
class Settings:
    """What one machine uses to build and fetch store paths.

    ``sandbox_backend`` selects a backend from section 9.7.
    ``substituters`` lists binary cache URLs in the order they are tried and
    ``trusted_keys`` the public keys their signatures must come from
    (section 14.3). ``require_provenance`` rejects substituted paths without
    valid signed provenance (section 14.9).
    """

    sandbox_backend: SandboxBackend = "namespaces"
    substituters: tuple[str, ...] = ()
    trusted_keys: tuple[str, ...] = ()
    require_provenance: bool = False

    def __post_init__(self) -> None:
        """Raise SettingsError if a field has a value the RFC does not allow."""
        if self.sandbox_backend not in SANDBOX_BACKENDS:
            raise SettingsError(
                f"unknown sandbox backend {self.sandbox_backend!r}, "
                f"expected one of {', '.join(SANDBOX_BACKENDS)}"
            )
        for field in ("substituters", "trusted_keys"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
                raise SettingsError(f"{field} must be a tuple of strings")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Settings:
        """Read the settings of a machine from ``path``.

        The file format is open in RFC 0001 section 18 (Settings format)
        and is defined before Phase 5, which first needs caches and keys.
        """
        raise NotImplementedError
