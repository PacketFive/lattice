# SPDX-License-Identifier: GPL-2.0
"""Profiles, generations, slots and activation (RFC 0001 section 13).

generations.py manages the profile and generation symlinks, slots.py the
slot symlinks, and activation.py the kernel module steps of a slot change.
The Lattice class (api.py) combines them under the profiles lock.
"""

from __future__ import annotations

from .activation import activate, boot
from .generations import (
    check_profile_name,
    current,
    generations,
    link_path,
    numbers,
    prepare,
    replace_link,
    root_name,
    store_path,
)
from .slots import SYSTEM, set_slot, states, system_environment, wanted

__all__ = [
    "SYSTEM",
    "activate",
    "boot",
    "check_profile_name",
    "current",
    "generations",
    "link_path",
    "numbers",
    "prepare",
    "replace_link",
    "root_name",
    "set_slot",
    "states",
    "store_path",
    "system_environment",
    "wanted",
]
