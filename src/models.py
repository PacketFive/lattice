# SPDX-License-Identifier: GPL-2.0
"""Data models shared by the library, the daemon and the remote API.

Every value a Lattice method returns, and every value the daemon sends or
receives, is a Pydantic model from this module, settings.py, events.py or
protocol.py (RFC 0001 sections 6.1 and 6.2). The daemon validates a request
with the same model the client used to build it, and the client validates
the reply with the same model the daemon used to send it.

Models are immutable and reject unknown fields. Validation is strict, so a
string is never silently turned into a number or a boolean.
"""

from __future__ import annotations

import enum
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .derivation import Derivation


class Model(BaseModel):
    """Base class of every Lattice data model."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True
    )


def parse[M: BaseModel](model: type[M], value: object) -> M:
    """Validate ``value``, decoded from JSON, as ``model``.

    Values decoded from JSON hold lists where models expect tuples, and
    strict validation accepts a list for a tuple only in JSON mode, so the
    value is validated from its JSON text.
    """
    return model.model_validate_json(json.dumps(value))


class Evaluation(Model):
    """The result of evaluating one PyBonsai file.

    ``derivations`` holds every top-level name bound to a derivation, in
    assignment order. ``seconds`` is the evaluation time of section 15.
    """

    filename: str
    derivations: dict[str, Derivation]
    seconds: float


class BuildResult(Model):
    """The outcome of realising one derivation.

    ``built`` is False when the path was already in the store or was
    substituted from a cache instead of being built.
    """

    derivation: Derivation
    store_path: str
    built: bool
    seconds: float


class GCResult(Model):
    """The store paths one collection removed and the bytes it freed."""

    removed: tuple[str, ...]
    freed_bytes: int
    seconds: float


class Generation(Model):
    """One generation of a profile (section 13.1)."""

    profile: str
    number: int
    store_path: str


class Role(enum.StrEnum):
    """What a caller of the daemon may do (RFC 0001 section 6.2)."""

    USER = "user"
    ADMIN = "admin"


class Identity(Model):
    """A caller of the daemon, as established by its transport.

    ``name`` is the user name for a Unix socket caller, the certificate
    common name for a TLS client certificate, and the grant name for a
    bearer token.
    """

    name: str
    role: Role
    transport: Literal["unix", "tls"]
