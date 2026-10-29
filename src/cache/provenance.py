# SPDX-License-Identifier: GPL-2.0
"""Signed build provenance for cache entries (RFC 0001 section 14.9).

The provenance of a store path is an in-toto Statement version 1 with a
SLSA Provenance version 1 predicate. It is signed with the cache key in a
Dead Simple Signing Envelope (DSSE): the signature covers the
pre-authentication encoding of the payload type and the payload, so a
signed statement cannot be read as some other kind of document.

Field names follow the in-toto and SLSA specifications, which use
camelCase, and the statement type is the JSON field ``_type``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import ConfigDict, Field

from ..errors import SubstitutionFailed
from ..models import Model
from . import signing
from .metadata import CacheEntry, parse_untrusted

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
BUILD_TYPE = "https://www.packetfive.com/pyrixos/docs/provenance/lattice-derivation/v1"
"""The buildType URI of a Lattice derivation build."""

_HEX = Field(pattern=r"^[0-9a-f]{64}$")


class Digest(Model):
    """A content digest, as in-toto writes it."""

    sha256: str = _HEX


class Subject(Model):
    """A store path and the SHA-256 of its contents."""

    name: str
    digest: Digest


class ExternalParameters(Model):
    """What the build was asked to do: realise one derivation."""

    derivation: str = _HEX


class BuildDefinition(Model):
    """The SLSA build definition of a derivation build."""

    buildType: Literal[
        "https://www.packetfive.com/pyrixos/docs/provenance/lattice-derivation/v1"
    ] = BUILD_TYPE
    externalParameters: ExternalParameters
    resolvedDependencies: tuple[Subject, ...] = ()


class Builder(Model):
    """The machine that ran the build, as its settings name it."""

    id: str = Field(min_length=1)


class RunMetadata(Model):
    """The sandbox backend and the start and end of the build, in RFC 3339 UTC."""

    backend: str
    startedOn: str
    finishedOn: str


class RunDetails(Model):
    """Who ran the build, and how."""

    builder: Builder
    metadata: RunMetadata


class Predicate(Model):
    """A SLSA Provenance version 1 predicate."""

    buildDefinition: BuildDefinition
    runDetails: RunDetails


class Statement(Model):
    """An in-toto Statement version 1 about one store path."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["https://in-toto.io/Statement/v1"] = Field(default=STATEMENT_TYPE, alias="_type")
    subject: tuple[Subject, ...] = Field(min_length=1, max_length=1)
    predicateType: Literal["https://slsa.dev/provenance/v1"] = PREDICATE_TYPE
    predicate: Predicate


class EnvelopeSignature(Model):
    """One DSSE signature: the key name and the base64 signature."""

    keyid: str
    sig: str


class Envelope(Model):
    """A DSSE envelope holding a base64 payload and its signatures."""

    payloadType: Literal["application/vnd.in-toto+json"] = PAYLOAD_TYPE
    payload: str
    signatures: tuple[EnvelopeSignature, ...] = ()


def pae(payload_type: str, payload: bytes) -> bytes:
    """Return the DSSE pre-authentication encoding of ``payload``."""
    kind = payload_type.encode()
    return b"DSSEv1 %d %b %d %b" % (len(kind), kind, len(payload), payload)


def statement(
    entry: CacheEntry,
    inputs: tuple[tuple[str, str], ...],
    builder_id: str,
    backend: str,
    started: str,
    finished: str,
) -> Statement:
    """Return the provenance of ``entry``, built from ``inputs`` by ``builder_id``.

    ``inputs`` holds each input store path with the SHA-256 of its contents,
    and on the microVM backend the guest kernel and init.
    """
    return Statement(
        subject=(Subject(name=entry.store_path, digest=Digest(sha256=entry.content_sha256)),),
        predicate=Predicate(
            buildDefinition=BuildDefinition(
                externalParameters=ExternalParameters(derivation=entry.drv_hash),
                resolvedDependencies=tuple(
                    Subject(name=path, digest=Digest(sha256=sha)) for path, sha in sorted(inputs)
                ),
            ),
            runDetails=RunDetails(
                builder=Builder(id=builder_id),
                metadata=RunMetadata(backend=backend, startedOn=started, finishedOn=finished),
            ),
        ),
    )


def sign(document: Statement, key: signing.SecretKey) -> Envelope:
    """Return ``document`` in a DSSE envelope signed with ``key``."""
    payload = document.model_dump_json(by_alias=True).encode()
    name, _, sig = key.sign(pae(PAYLOAD_TYPE, payload)).partition(":")
    return Envelope(
        payload=base64.b64encode(payload).decode(),
        signatures=(EnvelopeSignature(keyid=name, sig=sig),),
    )


def open_envelope(
    data: bytes, where: str, trusted: tuple[str, ...], entry: CacheEntry
) -> Statement:
    """Return the statement in the envelope ``data`` if it is valid for ``entry``.

    The envelope must be signed by a key in ``trusted`` and the statement
    must name the store path, content hash and derivation hash of
    ``entry``. Raises SubstitutionFailed naming ``where`` otherwise.
    """
    envelope = parse_untrusted(Envelope, data, where)
    try:
        payload = base64.b64decode(envelope.payload, validate=True)
    except binascii.Error:
        raise SubstitutionFailed(f"{where}: payload is not base64") from None
    signatures = [f"{item.keyid}:{item.sig}" for item in envelope.signatures]
    if signing.verify(trusted, pae(envelope.payloadType, payload), signatures) is None:
        raise SubstitutionFailed(f"{where} has no signature from a trusted key")
    document = parse_untrusted(Statement, payload, where)
    subject = document.subject[0]
    if (
        subject.name != entry.store_path
        or subject.digest.sha256 != entry.content_sha256
        or document.predicate.buildDefinition.externalParameters.derivation != entry.drv_hash
    ):
        raise SubstitutionFailed(f"{where} does not describe {entry.store_path}")
    return document
