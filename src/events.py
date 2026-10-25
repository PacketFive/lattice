# SPDX-License-Identifier: GPL-2.0
"""Progress events reported by the Lattice library (RFC 0001 section 6.1).

A program that embeds Lattice passes an ``on_event`` callable to Lattice
and receives one Event per step, for example to drive its own progress
display. Every event is also logged to the ``lattice`` logger at DEBUG
level, so a program that only configures logging sees the same steps.

Events carry plain values and are immutable. Later phases add events for
builds, collection, switching and substitution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("lattice")


@dataclass(frozen=True)
class Event:
    """Base class for every progress event."""


@dataclass(frozen=True)
class EvaluationStarted(Event):
    """Evaluation of ``filename`` has started."""

    filename: str


@dataclass(frozen=True)
class EvaluationFinished(Event):
    """Evaluation of ``filename`` produced ``derivations`` derivations in ``seconds``."""

    filename: str
    derivations: int
    seconds: float


EventHandler = Callable[[Event], None]


def emit(handler: EventHandler | None, event: Event) -> None:
    """Log ``event`` to the ``lattice`` logger and pass it to ``handler`` if set.

    An exception raised by the handler propagates to the caller of the
    Lattice method, so a program can stop an operation from its handler.
    """
    logger.debug("%r", event)
    if handler is not None:
        handler(event)
