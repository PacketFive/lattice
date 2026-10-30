# SPDX-License-Identifier: GPL-2.0
"""The Trellis options a system description reads (RFC 0004 section 5.2).

When the directory of a description holds a ``config.pyb`` written by
Trellis (RFC 0003 section 9), its ``options`` mapping becomes the
read-only name ``config``. Lattice and Client read the file next to the
description; the daemon receives its text and never reads caller files.
"""

from __future__ import annotations

import os

import pybonsai
from pybonsai import FrozenMap, Host

from ..errors import EvaluationFailed
from ..stdlib import build_host

CONFIG_FILE = "config.pyb"


def read_config(description: str | os.PathLike[str]) -> str | None:
    """Return the text of the ``config.pyb`` next to ``description``, or None if there is none."""
    directory = os.path.dirname(os.path.abspath(os.fspath(description)))
    try:
        with open(os.path.join(directory, CONFIG_FILE), encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        return None


def config_host(config_source: str) -> Host:
    """Return the standard library host whose ``config`` is the options of ``config_source``.

    ``config.pyb`` is evaluated on its own with the PyBonsai default host
    and must define the dictionary ``options``. Raises PyBonsaiError if
    it is rejected or fails, and EvaluationFailed if it has no options.
    """
    names = pybonsai.evaluate_source(config_source, CONFIG_FILE)
    options = names.get("options")
    if not isinstance(options, FrozenMap):
        raise EvaluationFailed(f"{CONFIG_FILE}: options must be a dictionary")
    return build_host(options)
