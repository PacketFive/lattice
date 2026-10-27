# SPDX-License-Identifier: GPL-2.0
"""Profile and generation symlinks (RFC 0001 section 13.1).

A profile ``p`` is a set of symlinks in ``<root>/profiles``:

====================  ===================================================
Symlink               Target
====================  ===================================================
``p-<n>-link``        The store path of generation ``n``
``p``                 ``p-<n>-link`` of the current generation
====================  ===================================================

A profile name follows the root name syntax, so ``user/alice`` is the
profile ``alice`` in the directory ``profiles/user``. No segment may start
with ``.`` or end in ``-<n>-link``, so a profile cannot be mistaken for a
generation, and a profile cannot sit below the symlink of another.
Generation ``n`` is also the GC root ``profile/<p>/<n>``.

Every symlink changes by creating a temporary symlink and renaming it
over the old one, so a reader always sees a complete profile. Callers
hold the profiles lock (store/lock.py) while they change a profile.
"""

from __future__ import annotations

import os
import re
import uuid

from ..errors import StoreError
from ..models import Generation
from ..store import StoreLayout, check_root_name

_GENERATION_LINK = re.compile(r".*-(?:0|[1-9][0-9]*)-link")


def check_profile_name(name: str) -> str:
    """Return ``name`` if it is a valid profile name, and raise StoreError if not."""
    check_root_name(name)
    for part in name.split("/"):
        if part.startswith(".") or _GENERATION_LINK.fullmatch(part):
            raise StoreError(f"{name!r} is not a valid profile name")
    return name


def root_name(profile: str, number: int) -> str:
    """Return the name of the GC root that keeps generation ``number`` of ``profile``."""
    return f"profile/{profile}/{number}"


def _paths(layout: StoreLayout, profile: str) -> tuple[str, str]:
    """Return the directory that holds the symlinks of ``profile`` and its last segment."""
    directory, _, last = os.path.join(layout.profiles, profile).rpartition("/")
    return directory, last


def link_path(layout: StoreLayout, profile: str, number: int | None = None) -> str:
    """Return the path of the current link, or of generation ``number``'s link."""
    directory, last = _paths(layout, profile)
    return os.path.join(directory, last if number is None else f"{last}-{number}-link")


def numbers(layout: StoreLayout, profile: str) -> list[int]:
    """Return the generation numbers of ``profile``, in increasing order."""
    directory, last = _paths(layout, profile)
    pattern = re.compile(re.escape(last) + r"-(0|[1-9][0-9]*)-link")
    try:
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = []
    for name in names:
        match = pattern.fullmatch(name)
        if match and os.path.islink(os.path.join(directory, name)):
            found.append(int(match.group(1)))
    return sorted(found)


def current(layout: StoreLayout, profile: str) -> int | None:
    """Return the number of the current generation of ``profile``, or None if it has none."""
    directory, last = _paths(layout, profile)
    try:
        target = os.readlink(os.path.join(directory, last))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    match = re.fullmatch(re.escape(last) + r"-(0|[1-9][0-9]*)-link", target)
    return int(match.group(1)) if match else None


def store_path(layout: StoreLayout, profile: str, number: int) -> str:
    """Return the store path of generation ``number``, and raise StoreError if there is none."""
    try:
        return os.readlink(link_path(layout, profile, number))
    except OSError:
        raise StoreError(f"profile {profile!r} has no generation {number}") from None


def generations(layout: StoreLayout, profile: str) -> tuple[Generation, ...]:
    """Return every generation of ``profile``, oldest first."""
    now = current(layout, profile)
    return tuple(
        Generation(
            profile=profile,
            number=number,
            store_path=store_path(layout, profile, number),
            current=number == now,
        )
        for number in numbers(layout, profile)
    )


def replace_link(path: str, target: str) -> None:
    """Point the symlink ``path`` at ``target`` with a temporary symlink and a rename."""
    temporary = os.path.join(os.path.dirname(path), f".{uuid.uuid4().hex}.tmp")
    os.symlink(target, temporary)
    try:
        os.rename(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def prepare(layout: StoreLayout, profile: str) -> None:
    """Create the directory of ``profile`` and check that the profile can be there.

    Raises StoreError if a directory on the way is the symlink of another
    profile, or if the profile's own name is a directory of profiles.
    """
    check_profile_name(profile)
    directory = layout.profiles
    for part in profile.split("/")[:-1]:
        directory = os.path.join(directory, part)
        if os.path.islink(directory):
            raise StoreError(f"profile {profile!r} is below the profile link {directory}")
    os.makedirs(os.path.dirname(link_path(layout, profile)), exist_ok=True)
    path = link_path(layout, profile)
    if os.path.lexists(path) and not os.path.islink(path):
        raise StoreError(f"profile {profile!r} is a directory of other profiles")
