# SPDX-License-Identifier: GPL-2.0
"""The kernel module system calls, which the os module lacks (RFC 0001 section 13.3).

Each wrapper calls libc's ``syscall`` through ctypes and raises OSError
with the errno on failure. Only Linux on x86-64 and AArch64 is
supported.
"""

from __future__ import annotations

import ctypes
import os
import platform

_NUMBERS: dict[str, dict[str, int]] = {
    "x86_64": {"init_module": 175, "delete_module": 176, "finit_module": 313},
    "aarch64": {"init_module": 105, "delete_module": 106, "finit_module": 273},
}

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def machine() -> str:
    """Return the machine name as the kernel reports it, for example ``x86_64``."""
    return platform.machine()


def _syscall(name: str, *args: object) -> None:
    numbers = _NUMBERS.get(machine())
    if numbers is None:
        raise OSError(0, f"{name}: {machine()} is not supported")
    if _libc.syscall(ctypes.c_long(numbers[name]), *args) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{name}: {os.strerror(error)}")


def finit_module(fd: int, params: str = "") -> None:
    """Load the kernel module in the open file ``fd`` with ``params``."""
    _syscall("finit_module", ctypes.c_int(fd), os.fsencode(params), ctypes.c_int(0))


def init_module(image: bytes, params: str = "") -> None:
    """Load the kernel module whose uncompressed contents are ``image``."""
    buffer = ctypes.create_string_buffer(image, len(image))
    _syscall("init_module", buffer, ctypes.c_ulong(len(image)), os.fsencode(params))


def delete_module(name: str) -> None:
    """Unload the kernel module ``name``, failing at once if it is in use."""
    _syscall("delete_module", os.fsencode(name), ctypes.c_uint(os.O_NONBLOCK))
