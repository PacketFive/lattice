# SPDX-License-Identifier: GPL-2.0
"""Changing slots and the kernel modules of their stacks (RFC 0001 section 13.3).

activate() moves every slot from the stack it has to the stack a new
``system`` generation wants. It first checks every slot whose kernel
modules change:

* the new modules were built for the running kernel,
* no process holds a device file of the old stack open, and
* no module of the old stack is in use, which means its reference count
  is more than the number of holders that are themselves modules of the
  old stack.

A module that a wanted stack also provides, with the same name and file,
is kept and not checked for use.

If any check fails, nothing changes and every such slot is reported with
an ActivationDeferred event, so the change happens at the next boot.
Otherwise the old modules of every changing slot are unloaded in reverse
load order, except those kept, then the new ones are loaded in load order
(skipping any already loaded), and the slot symlinks are replaced. If a
module step or a slot symlink fails, the slots already replaced are put
back, the module steps are undone in reverse and ActivationFailed is
raised.

boot() is the step that runs at boot: it loads the modules of every
stack the current generation wants and sets the slots to match.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import ActivationFailed, StoreError
from ..events import (
    ActivationDeferred,
    EventHandler,
    KernelModuleLoaded,
    KernelModuleUnloaded,
    SlotChanged,
    emit,
)
from ..kmod import KernelModules, ModuleInfo, find_modules, load_order, read_info
from ..models import Model
from ..store import StoreLayout
from ..store.combine import StackManifest, read_manifest
from .slots import set_slot


class _Stack(Model):
    """A stack with its kernel modules in load order."""

    path: str
    manifest: StackManifest
    modules: tuple[ModuleInfo, ...]


def _stack(layout: StoreLayout, path: str | None) -> _Stack | None:
    if path is None:
        return None
    manifest = read_manifest(layout, path, StackManifest)
    if manifest is None:
        raise ActivationFailed(f"{path} is not a stack")
    files = [
        file
        for module_path in manifest.kernel_modules
        for file in find_modules(layout.physical(module_path))
    ]
    modules = load_order(tuple(read_info(file) for file in files))
    return _Stack(path=path, manifest=manifest, modules=modules)


def _module_paths(stack: _Stack | None) -> frozenset[str]:
    return frozenset(stack.manifest.kernel_modules) if stack is not None else frozenset()


class _Change(Model):
    """One slot that changes from ``old`` to ``new``."""

    slot: str
    old: _Stack | None
    new: _Stack | None

    @property
    def modules_change(self) -> bool:
        return _module_paths(self.old) != _module_paths(self.new)


def _module_key(module: ModuleInfo) -> tuple[str, str]:
    return (module.name, module.file)


def _blockers(
    change: _Change, kernel: KernelModules, kept: frozenset[tuple[str, str]]
) -> list[str]:
    """Return why the modules of ``change`` cannot change now, or nothing if they can."""
    reasons: list[str] = []
    release = kernel.release()
    if change.new is not None:
        for module in change.new.modules:
            if module.release != release:
                reasons.append(
                    f"module {module.name} is built for kernel {module.release or 'unknown'}, "
                    f"not {release}"
                )
    if change.old is not None:
        for use in kernel.open_devices(change.old.manifest.devices):
            reasons.append(f"process {use.pid} has {use.path} open")
        names = {module.name for module in change.old.modules}
        leaving = {m.name for m in change.old.modules if _module_key(m) not in kept}
        loaded = kernel.loaded()
        for name in sorted(leaving & set(loaded)):
            module = loaded[name]
            if module.refcount - len(names & set(module.holders)) > 0:
                reasons.append(f"module {name} is in use")
    return reasons


class _Steps:
    """The module steps done so far, so they can be undone."""

    def __init__(self, kernel: KernelModules, on_event: EventHandler | None) -> None:
        self.kernel = kernel
        self.on_event = on_event
        self.undo: list[Callable[[], None]] = []

    def unload(self, module: ModuleInfo) -> None:
        self.kernel.unload(module.name)
        emit(self.on_event, KernelModuleUnloaded(name=module.name))
        self.undo.append(lambda: self.kernel.load(module.file))

    def load(self, module: ModuleInfo) -> None:
        self.kernel.load(module.file)
        emit(self.on_event, KernelModuleLoaded(name=module.name, file=module.file))
        self.undo.append(lambda: self.kernel.unload(module.name))

    def roll_back(self) -> list[str]:
        """Undo every step in reverse, and return the ones that failed."""
        failures = []
        for step in reversed(self.undo):
            try:
                step()
            except ActivationFailed as error:
                failures.append(str(error))
        self.undo.clear()
        return failures


def activate(
    layout: StoreLayout,
    kernel: KernelModules,
    wanted: dict[str, str],
    on_event: EventHandler | None = None,
) -> bool:
    """Move the slots to the stacks in ``wanted``, by slot name.

    Returns False if the change is deferred to the next boot, after
    reporting each blocked slot. Raises ActivationFailed if a module step
    or slot change fails; the steps already done are then undone.
    """
    active = layout.active_slots()
    changes = [
        _Change(
            slot=slot,
            old=_stack(layout, active.get(slot)),
            new=_stack(layout, wanted.get(slot)),
        )
        for slot in sorted(set(active) | set(wanted))
        if active.get(slot) != wanted.get(slot)
    ]
    new_stacks = {change.slot: change.new for change in changes}
    kept = frozenset(
        _module_key(module)
        for slot, path in wanted.items()
        if (stack := new_stacks[slot] if slot in new_stacks else _stack(layout, path)) is not None
        for module in stack.modules
    )
    moving = [change for change in changes if change.modules_change]
    blocked = {
        change.slot: reasons for change in moving if (reasons := _blockers(change, kernel, kept))
    }
    if blocked:
        for slot, reasons in blocked.items():
            emit(on_event, ActivationDeferred(slot=slot, reason="; ".join(reasons)))
        return False
    steps = _Steps(kernel, on_event)
    replaced: list[str] = []
    try:
        for change in moving:
            if change.old is not None:
                loaded = kernel.loaded()
                for module in reversed(change.old.modules):
                    if module.name in loaded and _module_key(module) not in kept:
                        steps.unload(module)
        for change in moving:
            if change.new is not None:
                loaded = kernel.loaded()
                for module in change.new.modules:
                    if module.name not in loaded:
                        steps.load(module)
        for change in changes:
            set_slot(layout, change.slot, wanted.get(change.slot))
            replaced.append(change.slot)
    except (ActivationFailed, StoreError) as error:
        failures: list[str] = []
        for slot in reversed(replaced):
            try:
                set_slot(layout, slot, active.get(slot))
            except StoreError as undo_error:
                failures.append(str(undo_error))
        failures += steps.roll_back()
        detail = f"; undoing also failed: {'; '.join(failures)}" if failures else ""
        raise ActivationFailed(f"{error}{detail}") from None
    for change in changes:
        emit(
            on_event,
            SlotChanged(
                slot=change.slot,
                old=active.get(change.slot),
                new=wanted.get(change.slot),
            ),
        )
    return True


def boot(
    layout: StoreLayout,
    kernel: KernelModules,
    wanted: dict[str, str],
    on_event: EventHandler | None = None,
) -> None:
    """Load the modules of every wanted stack and set the slots to ``wanted``.

    A module that is already loaded is left as it is. Every slot is set
    even if a module fails to load; ActivationFailed is then raised with
    every failure.
    """
    failures: list[str] = []
    for slot in sorted(wanted):
        try:
            stack = _stack(layout, wanted[slot])
            for module in stack.modules if stack is not None else ():
                if module.name not in kernel.loaded():
                    kernel.load(module.file)
                    emit(on_event, KernelModuleLoaded(name=module.name, file=module.file))
        except ActivationFailed as error:
            failures.append(f"slot {slot}: {error}")
    active = layout.active_slots()
    for slot in sorted(set(active) | set(wanted)):
        if active.get(slot) != wanted.get(slot):
            set_slot(layout, slot, wanted.get(slot))
            emit(on_event, SlotChanged(slot=slot, old=active.get(slot), new=wanted.get(slot)))
    if failures:
        raise ActivationFailed("; ".join(failures))
