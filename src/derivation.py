# SPDX-License-Identifier: GPL-2.0
"""Derivations and their hashes (RFC 0001 section 8).

A Derivation is what a PyBonsai file produces through the Lattice standard
library, and the only output of evaluation that later phases consume.
Derivation is registered with pybonsai as a host type (see stdlib.py).
Its hash is the SHA-256 digest of a canonical JSON serialisation, so two
derivations with the same fields have the same hash on any machine.

Derivation is a frozen Pydantic model, so the library, the daemon and the
remote API share one definition. In JSON a derivation also carries its
``hash`` and ``store_path``, and every input derivation is nested in
``env``. When a derivation is read from JSON those two fields are
optional, but if present they MUST match the values computed from the
other fields, so a derivation cannot arrive under a store path it does not
own.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import cached_property
from typing import Annotated, Any

from pybonsai import FrozenMap
from pydantic import (
    BaseModel,
    ConfigDict,
    ModelWrapValidatorHandler,
    PlainSerializer,
    PlainValidator,
    SerializationInfo,
    ValidationInfo,
    computed_field,
    model_validator,
)

STORE_DIR = "/lattice/store"
STORE_HASH_LEN = 32

# A builder that Lattice provides itself instead of a program in the
# sandbox. fetch_url uses it, with ``url`` and ``sha256`` in ``env``.
BUILTIN_FETCH_URL = "builtin:fetch_url"
# Builders that combine their inputs into one tree of symlinks (section 13).
BUILTIN_STACK = "builtin:stack"
BUILTIN_PROFILE = "builtin:profile"
# Unpacks an archive outside the sandbox (RFC 0002 section 5.3).
BUILTIN_UNPACK = "builtin:unpack"

_COMPUTED = ("hash", "store_path")


def _check_name(name: object, what: str = "derivation name") -> None:
    if type(name) is not str:
        raise TypeError(f"{what} must be a string")
    if not name or "/" in name or name.startswith(".") or "\0" in name:
        raise ValueError(f"{what} {name!r} must be non-empty, without '/' or a leading '.'")


def _check_strings(field: str, values: object) -> tuple[str, ...]:
    if type(values) not in (tuple, list):
        raise TypeError(f"derivation {field} must be a tuple of strings")
    assert isinstance(values, (tuple, list))  # noqa: S101
    if not all(type(value) is str for value in values):
        raise TypeError(f"derivation {field} must be a tuple of strings")
    return tuple(values)


def validate_fields(
    name: object,
    builder: object,
    args: object,
    env: object,
    outputs: object,
) -> None:
    """Check field types and values before a Derivation is built.

    ``name`` is a non-empty string with no ``/`` and no leading ``.``.
    ``builder`` is a non-empty string. ``args`` and ``outputs`` are tuples
    of strings, and ``outputs`` is non-empty with no duplicates. ``env`` is
    a FrozenMap from strings to strings or Derivations. Raises TypeError or
    ValueError with a message naming the field.
    """
    _check_name(name)
    if type(builder) is not str:
        raise TypeError("derivation builder must be a string")
    if not builder:
        raise ValueError("derivation builder must not be empty")
    _check_strings("args", args)
    checked = _check_strings("outputs", outputs)
    if not checked:
        raise ValueError("derivation outputs must not be empty")
    if len(set(checked)) != len(checked):
        raise ValueError("derivation outputs must not repeat")
    for output in checked:
        _check_name(output, "derivation output")
    if type(env) is not FrozenMap:
        raise TypeError("derivation env must be a mapping")
    assert isinstance(env, FrozenMap)  # noqa: S101
    for key, value in env.items():
        if type(key) is not str or not key:
            raise TypeError("derivation env keys must be non-empty strings")
        if type(value) is not str and type(value) is not Derivation:
            raise TypeError(f"derivation env value {key!r} must be a string or a derivation")


# Pydantic turns a ValueError raised in a validator into a ValidationError
# but lets a TypeError escape, so the validators below raise ValueError.


def _validate_strings(value: object, info: ValidationInfo) -> tuple[str, ...]:
    try:
        return _check_strings(info.field_name or "field", value)
    except TypeError as error:
        raise ValueError(str(error)) from None


def _serialize_strings(value: tuple[str, ...], info: SerializationInfo) -> object:
    return list(value) if info.mode_is_json() else value


def _validate_env(value: object) -> FrozenMap:
    if type(value) is not FrozenMap and type(value) is not dict:
        raise ValueError("derivation env must be a mapping")
    assert isinstance(value, (FrozenMap, dict))  # noqa: S101
    env: dict[object, object] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            item = Derivation.model_validate(item)
        env[key] = item
    return FrozenMap(env)


def _serialize_env(env: FrozenMap) -> dict[str, object]:
    return {
        str(key): value.model_dump(mode="json") if type(value) is Derivation else value
        for key, value in env.items()
    }


Strings = Annotated[
    tuple[str, ...], PlainValidator(_validate_strings), PlainSerializer(_serialize_strings)
]
Env = Annotated[FrozenMap, PlainValidator(_validate_env), PlainSerializer(_serialize_env)]


class Derivation(BaseModel):
    """An immutable description of one build.

    ``env`` maps strings to strings or other Derivations. Every field is
    checked with validate_fields when the model is built, from Python or
    from JSON. Lists given for ``args`` and ``outputs`` become tuples and a
    dict given for ``env`` becomes a FrozenMap.

    Two derivations are equal when their hashes are equal, and the hash
    of a derivation is computed once.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True
    )

    name: str
    builder: str
    args: Strings = ()
    env: Env = FrozenMap()
    outputs: Strings = ("out",)

    @model_validator(mode="wrap")
    @classmethod
    def _check(cls, data: Any, handler: ModelWrapValidatorHandler[Derivation]) -> Derivation:
        claimed: dict[str, object] = {}
        if isinstance(data, dict):
            data = dict(data)
            for field in _COMPUTED:
                if field in data:
                    claimed[field] = data.pop(field)
        drv = handler(data)
        try:
            validate_fields(drv.name, drv.builder, drv.args, drv.env, drv.outputs)
        except TypeError as error:
            raise ValueError(str(error)) from None
        for field, value in claimed.items():
            if getattr(drv, field) != value:
                raise ValueError(f"derivation {drv.name!r}: {field} does not match its fields")
        return drv

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def hash(self) -> str:
        """The full derivation hash as lowercase hex. See derivation_hash."""
        return derivation_hash(self)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def store_path(self) -> str:
        """The store path of the first output. See store_path."""
        return store_path(self)

    @property
    def output_paths(self) -> dict[str, str]:
        """Map each output name to its store path, in the order of ``outputs``."""
        return {output: output_path(self, output) for output in self.outputs}

    def __str__(self) -> str:
        """Return store_path, so ``f"{drv}/bin"`` in a file names a path in the store."""
        return self.store_path

    def __hash__(self) -> int:
        """Hash of the derivation hash, so nested inputs are not hashed again."""
        return hash(self.hash)

    def __eq__(self, other: object) -> bool:
        """Compare equal to another Derivation with the same hash."""
        if type(other) is not Derivation:
            return NotImplemented
        assert isinstance(other, Derivation)  # noqa: S101
        return self is other or self.hash == other.hash


def canonical_form(drv: Derivation) -> dict[str, object]:
    """Return the plain dict that is serialised for hashing.

    Every Derivation inside ``env`` is replaced by its own ``hash``, so the
    hash of a derivation covers all of its dependencies transitively.
    """
    return {
        "name": drv.name,
        "builder": drv.builder,
        "args": list(drv.args),
        "env": {
            str(key): value.hash if type(value) is Derivation else value
            for key, value in drv.env.items()
        },
        "outputs": list(drv.outputs),
    }


def canonical_json(drv: Derivation) -> bytes:
    """Serialise canonical_form as JSON: keys sorted, no whitespace, UTF-8.

    Equivalent to ``json.dumps(form, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False).encode("utf-8")``.
    """
    form = canonical_form(drv)
    return json.dumps(form, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def derivation_hash(drv: Derivation) -> str:
    """Return the SHA-256 of canonical_json(drv) as 64 lowercase hex characters."""
    return hashlib.sha256(canonical_json(drv)).hexdigest()


def store_hash(drv: Derivation) -> str:
    """Return the hash used in store paths.

    The SHA-256 digest encoded with the RFC 4648 base32 alphabet, lowercased,
    with padding removed and truncated to STORE_HASH_LEN characters.
    """
    digest = bytes.fromhex(drv.hash)
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:STORE_HASH_LEN]


def output_path(drv: Derivation, output: str, store_dir: str = STORE_DIR) -> str:
    """Return the store path of ``output``, one of ``drv.outputs``.

    The output ``out`` is ``<store_dir>/<store hash>-<name>`` and any other
    output adds ``-<output>``, so ``dev`` of ``hello`` is ``...-hello-dev``.
    """
    if output not in drv.outputs:
        raise ValueError(f"derivation {drv.name!r} has no output {output!r}")
    suffix = "" if output == "out" else f"-{output}"
    return f"{store_dir}/{store_hash(drv)}-{drv.name}{suffix}"


def store_path(drv: Derivation, store_dir: str = STORE_DIR) -> str:
    """Return the store path of the first output, which ``str(drv)`` gives a file."""
    return output_path(drv, drv.outputs[0], store_dir)
