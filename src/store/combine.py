# SPDX-License-Identifier: GPL-2.0
"""The builders of stacks and profiles (RFC 0001 section 13).

A stack or profile is a derivation whose builder is ``builtin:stack`` or
``builtin:profile``. Lattice builds it itself, outside the sandbox, since
it only combines its inputs: the output is a tree of directories and
symlinks into the store, like a union of the input trees, together with a
manifest that records what went in. Two inputs that provide the same file
fail the build, so the result never depends on the order of the inputs.

The manifest is ``lattice-stack.json`` or ``lattice-profile.json`` at the
top of the output. It names store paths, so the reference scan of section
10.3 records every input it lists, and the garbage collector keeps a
stack's kernel modules for as long as it keeps the stack.
"""

from __future__ import annotations

import os
import re

from pydantic import field_validator

from ..derivation import BUILTIN_PROFILE, BUILTIN_STACK, Derivation
from ..errors import BuildFailed, StoreError
from ..models import Model
from ..stdlib import check_slot
from .layout import StoreLayout

STACK_MANIFEST = "lattice-stack.json"
PROFILE_MANIFEST = "lattice-profile.json"


class StackManifest(Model):
    """What a stack holds: ``lattice-stack.json``."""

    slot: str
    name: str
    contents: tuple[str, ...]
    kernel_modules: tuple[str, ...]
    devices: tuple[str, ...]

    @field_validator("slot")
    @classmethod
    def _check_slot(cls, value: str) -> str:
        return check_slot(value)


class ProfileManifest(Model):
    """What a profile generation holds: ``lattice-profile.json``.

    ``stacks`` maps each slot name to the store path of its stack.
    """

    name: str
    packages: tuple[str, ...]
    stacks: dict[str, str]

    @field_validator("stacks")
    @classmethod
    def _check_slots(cls, value: dict[str, str]) -> dict[str, str]:
        for slot in value:
            check_slot(slot)
        return value


def _numbered(drv: Derivation, prefix: str) -> list[object]:
    pattern = re.compile(re.escape(prefix) + r"_(0|[1-9][0-9]*)")
    found: dict[int, object] = {}
    for key, value in drv.env.items():
        match = pattern.fullmatch(str(key))
        if match:
            found[int(match.group(1))] = value
    if sorted(found) != list(range(len(found))):
        raise BuildFailed(f"{drv.name}: the {prefix} entries are not numbered from 0")
    return [found[index] for index in range(len(found))]


def _paths(drv: Derivation, prefix: str) -> tuple[str, ...]:
    values = _numbered(drv, prefix)
    if not all(isinstance(value, Derivation) for value in values):
        raise BuildFailed(f"{drv.name}: every {prefix} entry must be a derivation")
    return tuple(value.store_path for value in values if isinstance(value, Derivation))


def _check_keys(drv: Derivation, allowed: re.Pattern[str]) -> None:
    for key in drv.env:
        if not allowed.fullmatch(str(key)):
            raise BuildFailed(f"{drv.name}: unexpected env entry {key!r}")


_STACK_KEYS = re.compile(r"slot|(content|kernel_module|device)_[0-9]+")
_PROFILE_KEYS = re.compile(r"package_[0-9]+|stack_.+")


def stack_manifest(drv: Derivation) -> StackManifest:
    """Return the manifest of stack ``drv`` from its ``env``. Raises BuildFailed."""
    _check_keys(drv, _STACK_KEYS)
    slot = drv.env.get("slot")
    devices = _numbered(drv, "device")
    if not isinstance(slot, str) or not all(isinstance(d, str) for d in devices):
        raise BuildFailed(f"{drv.name}: a stack needs a slot name and device patterns")
    try:
        return StackManifest(
            slot=slot,
            name=drv.name,
            contents=_paths(drv, "content"),
            kernel_modules=_paths(drv, "kernel_module"),
            devices=tuple(str(d) for d in devices),
        )
    except ValueError as error:
        raise BuildFailed(f"{drv.name}: {error}") from None


def profile_manifest(drv: Derivation) -> ProfileManifest:
    """Return the manifest of profile ``drv`` from its ``env``. Raises BuildFailed."""
    _check_keys(drv, _PROFILE_KEYS)
    stacks: dict[str, str] = {}
    for key in sorted(str(key) for key in drv.env):
        if key.startswith("stack_"):
            value = drv.env[key]
            if not isinstance(value, Derivation) or value.builder != BUILTIN_STACK:
                raise BuildFailed(f"{drv.name}: {key} must be a stack")
            if value.env.get("slot") != key.removeprefix("stack_"):
                raise BuildFailed(f"{drv.name}: {key} holds a stack for another slot")
            stacks[key.removeprefix("stack_")] = value.store_path
    try:
        return ProfileManifest(name=drv.name, packages=_paths(drv, "package"), stacks=stacks)
    except ValueError as error:
        raise BuildFailed(f"{drv.name}: {error}") from None


def _merge(
    physical: str, logical: str, destination: str, relative: str, owners: dict[str, str]
) -> None:
    """Add symlinks to every file under ``physical`` to the tree at ``destination``."""
    for entry in sorted(os.listdir(os.path.join(physical, relative))):
        path = os.path.join(relative, entry)
        source = os.path.join(physical, path)
        target = os.path.join(destination, path)
        is_directory = os.path.isdir(source) and not os.path.islink(source)
        if os.path.lexists(target):
            merged = is_directory and os.path.isdir(target) and not os.path.islink(target)
            if not merged:
                raise BuildFailed(f"{owners[path]} and {logical} both provide {path}")
        elif is_directory:
            os.mkdir(target, 0o755)
            owners[path] = logical
        else:
            os.symlink(os.path.join(logical, path), target)
            owners[path] = logical
        if is_directory:
            _merge(physical, logical, destination, path, owners)


def combine(layout: StoreLayout, drv: Derivation, destination: str) -> dict[str, str]:
    """Build the stack or profile ``drv`` at ``destination`` and return its outputs.

    Every input must be a registered directory. Raises BuildFailed if two
    inputs provide the same file or one is not a directory.
    """
    if drv.outputs != ("out",):
        raise BuildFailed(f"{drv.name}: a stack or profile has the single output out")
    manifest: StackManifest | ProfileManifest
    if drv.builder == BUILTIN_STACK:
        manifest = stack_manifest(drv)
        sources, name = manifest.contents, STACK_MANIFEST
    elif drv.builder == BUILTIN_PROFILE:
        manifest = profile_manifest(drv)
        sources, name = manifest.packages, PROFILE_MANIFEST
    else:
        raise BuildFailed(f"{drv.name}: {drv.builder} does not combine its inputs")
    os.mkdir(destination, 0o755)
    owners: dict[str, str] = {name: drv.name}
    with open(os.path.join(destination, name), "x", encoding="utf-8") as file:
        file.write(manifest.model_dump_json(indent=2) + "\n")
    for source in sources:
        physical = layout.physical(source)
        if not os.path.isdir(physical) or os.path.islink(physical):
            raise BuildFailed(f"{drv.name}: {source} is not a directory")
        _merge(physical, source, destination, "", owners)
    return {"out": destination}


def read_manifest[M: Model](layout: StoreLayout, path: str, model: type[M]) -> M | None:
    """Return the manifest of type ``model`` in store path ``path``, or None if it has none.

    Raises StoreError if the manifest is there but not valid.
    """
    name = STACK_MANIFEST if model is StackManifest else PROFILE_MANIFEST
    try:
        with open(os.path.join(layout.physical(path), name), "rb") as file:
            text = file.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return model.model_validate_json(text)
    except ValueError as error:
        raise StoreError(f"{path}/{name} is not valid: {error}") from None
