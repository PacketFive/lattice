# SPDX-License-Identifier: GPL-2.0
"""Kernel modules: reading module files and changing the running kernel's modules.

======================  ===================================================
Module                  Contents
======================  ===================================================
``modinfo.py``          The ``.modinfo`` section of a module file, and the
                        order in which a set of modules loads
``system.py``           The KernelModules protocol and its Linux
                        implementation
``syscalls.py``         The module system calls, through ctypes
======================  ===================================================
"""

from __future__ import annotations

from .modinfo import ModuleInfo, find_modules, load_order, module_name, read_info
from .system import KernelModules, LinuxModules, LoadedModule

__all__ = [
    "KernelModules",
    "LinuxModules",
    "LoadedModule",
    "ModuleInfo",
    "find_modules",
    "load_order",
    "module_name",
    "read_info",
]
