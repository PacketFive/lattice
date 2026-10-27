# SPDX-License-Identifier: GPL-2.0
"""Slot symlinks (RFC 0001 section 13.2).

Slot ``s`` is the symlink ``<stacks>/<s>``, where ``stacks`` is the
``[store] stacks`` setting. It points at the store path of the active
stack. Only the ``system`` profile assigns stacks to slots.
"""

from __future__ import annotations

import os

from ..errors import StoreError
from ..models import SlotState
from ..store import StoreLayout
from ..store.combine import ProfileManifest, read_manifest
from .generations import current, replace_link, store_path

SYSTEM = "system"


def wanted(layout: StoreLayout, environment: str | None) -> dict[str, str]:
    """Return the stack the generation ``environment`` assigns to each slot.

    An environment that is not a profile assigns none.
    """
    if environment is None:
        return {}
    manifest = read_manifest(layout, environment, ProfileManifest)
    return dict(manifest.stacks) if manifest is not None else {}


def system_environment(layout: StoreLayout) -> str | None:
    """Return the store path of the current ``system`` generation, or None."""
    number = current(layout, SYSTEM)
    return None if number is None else store_path(layout, SYSTEM, number)


def states(layout: StoreLayout) -> tuple[SlotState, ...]:
    """Return every slot that is active or wanted, sorted by name."""
    active = layout.active_slots()
    want = wanted(layout, system_environment(layout))
    return tuple(
        SlotState(slot=slot, active=active.get(slot), wanted=want.get(slot))
        for slot in sorted(set(active) | set(want))
    )


def set_slot(layout: StoreLayout, slot: str, stack: str | None) -> None:
    """Point slot ``slot`` at ``stack``, or remove it if ``stack`` is None."""
    path = os.path.join(layout.stacks, slot)
    if stack is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise StoreError(f"cannot remove slot {slot}: {error}") from None
        return
    try:
        os.makedirs(layout.stacks, exist_ok=True)
        replace_link(path, stack)
    except OSError as error:
        raise StoreError(f"cannot set slot {slot}: {error}") from None
