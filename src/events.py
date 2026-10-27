# SPDX-License-Identifier: GPL-2.0
"""Progress events reported by the Lattice library (RFC 0001 section 6.1).

A program passes an ``on_event`` callable to Lattice or Client and receives
one Event per step, for example to drive its own progress display. Every
event is also logged to the ``lattice`` logger at DEBUG level, so a program
that only configures logging sees the same steps.

Events are immutable Pydantic models. The ``type`` field names the event,
so the daemon can send events to a client as JSON and the client can
rebuild the same objects. Phase 5 adds events for substitution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from .models import Model

logger = logging.getLogger("lattice")


class Event(Model):
    """Base class for every progress event."""


class EvaluationStarted(Event):
    """Evaluation of ``filename`` has started."""

    type: Literal["evaluation_started"] = "evaluation_started"
    filename: str


class EvaluationFinished(Event):
    """Evaluation of ``filename`` produced ``derivations`` derivations in ``seconds``."""

    type: Literal["evaluation_finished"] = "evaluation_finished"
    filename: str
    derivations: int
    seconds: float


class BuildStarted(Event):
    """Lattice has started to download or build the derivation ``name``."""

    type: Literal["build_started"] = "build_started"
    name: str
    store_path: str


class BuildLog(Event):
    """The build of ``name`` printed ``text``, which may end part way through a line."""

    type: Literal["build_log"] = "build_log"
    name: str
    text: str


class BuildFinished(Event):
    """The derivation ``name`` is in the store; ``built`` is False if it already was."""

    type: Literal["build_finished"] = "build_finished"
    name: str
    store_path: str
    built: bool
    seconds: float


class CollectionFinished(Event):
    """Garbage collection removed ``removed`` store paths and freed ``freed_bytes``."""

    type: Literal["collection_finished"] = "collection_finished"
    removed: int
    freed_bytes: int


class ProfileSwitched(Event):
    """A profile now points at another generation (section 13.1)."""

    type: Literal["profile_switched"] = "profile_switched"
    profile: str
    number: int
    store_path: str


class ActivationDeferred(Event):
    """A slot cannot change now and will change at the next boot (section 13.3)."""

    type: Literal["activation_deferred"] = "activation_deferred"
    slot: str
    reason: str


class SlotChanged(Event):
    """A slot symlink now points at another stack, or was created or removed."""

    type: Literal["slot_changed"] = "slot_changed"
    slot: str
    old: str | None
    new: str | None


class KernelModuleLoaded(Event):
    """A kernel module was loaded from ``file``."""

    type: Literal["kernel_module_loaded"] = "kernel_module_loaded"
    name: str
    file: str


class KernelModuleUnloaded(Event):
    """A kernel module was unloaded."""

    type: Literal["kernel_module_unloaded"] = "kernel_module_unloaded"
    name: str


EventUnion = (
    EvaluationStarted
    | EvaluationFinished
    | BuildStarted
    | BuildLog
    | BuildFinished
    | CollectionFinished
    | ProfileSwitched
    | ActivationDeferred
    | SlotChanged
    | KernelModuleLoaded
    | KernelModuleUnloaded
)
AnyEvent = Annotated[EventUnion, Field(discriminator="type")]
EVENTS: TypeAdapter[EventUnion] = TypeAdapter(AnyEvent)
"""Validates any event from its JSON form, choosing the class by ``type``."""

EventHandler = Callable[[Event], None]


def emit(handler: EventHandler | None, event: Event) -> None:
    """Log ``event`` to the ``lattice`` logger and pass it to ``handler`` if set.

    An exception raised by the handler propagates to the caller of the
    Lattice method, so a program can stop an operation from its handler.
    """
    logger.debug("%r", event)
    if handler is not None:
        handler(event)
