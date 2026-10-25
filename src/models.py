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
from typing import Annotated, Literal

from pybonsai import FrozenMap
from pydantic import BaseModel, ConfigDict, PlainSerializer, PlainValidator

from .derivation import Derivation, validate_fields


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


class DerivationWire(Model):
    """A Derivation as it is sent to and from the daemon.

    ``hash`` and ``store_path`` are always sent. When received, they are
    optional, and if present MUST match the values computed from the other
    fields, so a derivation cannot arrive under a store path it does not own.
    """

    name: str
    builder: str
    args: tuple[str, ...] = ()
    env: dict[str, str | DerivationWire] = {}
    outputs: tuple[str, ...] = ("out",)
    hash: str | None = None
    store_path: str | None = None


def derivation_to_wire(drv: Derivation) -> DerivationWire:
    """Return the wire form of ``drv``, with every input derivation nested in ``env``."""
    env: dict[str, str | DerivationWire] = {}
    for key, value in drv.env.items():
        env[str(key)] = derivation_to_wire(value) if isinstance(value, Derivation) else str(value)
    return DerivationWire(
        name=drv.name,
        builder=drv.builder,
        args=drv.args,
        env=env,
        outputs=drv.outputs,
        hash=drv.hash,
        store_path=drv.store_path,
    )


def derivation_from_wire(wire: DerivationWire) -> Derivation:
    """Rebuild a Derivation from its wire form.

    Raises ValueError or TypeError if a field is invalid (see
    derivation.validate_fields) or if ``hash`` or ``store_path`` does not
    match the fields.
    """
    env = FrozenMap(
        {
            key: derivation_from_wire(value) if isinstance(value, DerivationWire) else value
            for key, value in wire.env.items()
        }
    )
    validate_fields(wire.name, wire.builder, wire.args, env, wire.outputs)
    drv = Derivation(wire.name, wire.builder, wire.args, env, wire.outputs)
    if wire.hash is not None and wire.hash != drv.hash:
        raise ValueError(f"derivation {wire.name!r}: hash does not match its fields")
    if wire.store_path is not None and wire.store_path != drv.store_path:
        raise ValueError(f"derivation {wire.name!r}: store path does not match its fields")
    return drv


def _validate_derivation(value: object) -> Derivation:
    if isinstance(value, Derivation):
        return value
    if not isinstance(value, DerivationWire):
        value = parse(DerivationWire, value)
    return derivation_from_wire(value)


def _serialize_derivation(drv: Derivation) -> dict[str, object]:
    return derivation_to_wire(drv).model_dump(mode="json")


DerivationField = Annotated[
    Derivation,
    PlainValidator(_validate_derivation),
    PlainSerializer(_serialize_derivation),
]
"""A Derivation in a model: a Derivation in Python, a DerivationWire in JSON."""


class Evaluation(Model):
    """The result of evaluating one PyBonsai file.

    ``derivations`` holds every top-level name bound to a derivation, in
    assignment order. ``seconds`` is the evaluation time of section 15.
    """

    filename: str
    derivations: dict[str, DerivationField]
    seconds: float


class BuildResult(Model):
    """The outcome of realising one derivation.

    ``built`` is False when the path was already in the store or was
    substituted from a cache instead of being built.
    """

    derivation: DerivationField
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
