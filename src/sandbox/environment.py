# SPDX-License-Identifier: GPL-2.0
"""The builder's environment (RFC 0001 section 9.5).

The builder sees the derivation's ``env``, one variable per output naming
its store path, and FIXED_VARIABLES. Nothing from Lattice's own
environment reaches it. A derivation that sets a fixed variable or a
variable named after one of its outputs is refused, so a reader of the
derivation sees exactly what the build sees.
"""

from __future__ import annotations

from types import MappingProxyType

from ..derivation import Derivation
from ..errors import BuildFailed

BUILD_DIR = "/build"

FIXED_VARIABLES = MappingProxyType(
    {
        "SOURCE_DATE_EPOCH": "315532800",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "HOME": "/homeless",
        "TMPDIR": BUILD_DIR,
        "PATH": "/path-not-set",
    }
)


def build_environment(drv: Derivation) -> dict[str, str]:
    """Return the builder's environment for ``drv``, sorted by name.

    A derivation in ``env`` is replaced by the store path of its first
    output. Raises BuildFailed if ``env`` sets a fixed variable or an
    output variable, or holds a name or value that execve cannot pass.
    """
    outputs = drv.output_paths
    result: dict[str, str] = {}
    for key, value in drv.env.items():
        name = str(key)
        if name in FIXED_VARIABLES:
            raise BuildFailed(f"{drv.name}: env sets {name}, which the sandbox fixes")
        if name in outputs:
            raise BuildFailed(f"{drv.name}: env sets {name}, which names an output")
        text = str(value)
        if "=" in name or "\0" in name or "\0" in text:
            raise BuildFailed(f"{drv.name}: env variable {name!r} cannot be passed to a program")
        result[name] = text
    result.update(outputs)
    result.update(FIXED_VARIABLES)
    return dict(sorted(result.items()))
