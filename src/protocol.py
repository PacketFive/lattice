# SPDX-License-Identifier: GPL-2.0
"""The daemon protocol: JSON-RPC 2.0 and REST over HTTP (RFC 0001 section 6.2).

The daemon serves one HTTP/1.1 interface on its Unix socket and, when
enabled, over TLS:

=================  ====================================================
Request            Meaning
=================  ====================================================
``POST /v1/rpc``   A JSON-RPC 2.0 request, notification or batch
``POST /v1/<m>``   REST: the body is the parameters of method ``<m>``
``GET /v1/info``   The daemon version, API version and method names
=================  ====================================================

Both styles take the same parameter models and return the same result
models. A JSON-RPC client that sends ``Accept: application/x-ndjson``
receives progress events as ``event`` notifications, one JSON object per
line, before the final response line.

Errors carry a JSON-RPC error object in both styles. REST also sets the
HTTP status. ``data.type`` names the LatticeError subclass, so a client can
raise the same class the daemon raised.
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict

from . import errors
from .derivation import Derivation
from .errors import LatticeError
from .events import Event
from .models import Model

JSONRPC_VERSION = "2.0"
API_VERSION = "1"
EVENT_METHOD = "event"
JSON = "application/json"
NDJSON = "application/x-ndjson"

RPC_PATH = "/v1/rpc"
REST_PREFIX = "/v1/"
INFO_PATH = "/v1/info"

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Lattice codes, in the range JSON-RPC reserves for servers.
NOT_IMPLEMENTED = -32001

# (JSON-RPC code, HTTP status) for each LatticeError subclass. The most
# specific class in the exception's MRO is used.
ERROR_CODES: dict[type[LatticeError], tuple[int, int]] = {
    errors.LatticeError: (-32000, 400),
    errors.AuthenticationFailed: (-32002, 401),
    errors.PermissionDenied: (-32003, 403),
    errors.ProtocolError: (INVALID_REQUEST, 400),
    errors.EvaluationFailed: (-32010, 422),
    errors.BuildFailed: (-32011, 422),
    errors.BackendUnavailable: (-32012, 503),
    errors.StoreError: (-32013, 409),
    errors.SubstitutionFailed: (-32014, 502),
    errors.SettingsError: (-32015, 400),
}
_ERROR_TYPES: dict[str, type[LatticeError]] = {cls.__name__: cls for cls in ERROR_CODES}


class RPCError(Exception):
    """A protocol-level failure: bad JSON, an invalid request, or an unknown method."""

    def __init__(self, code: int, message: str, status: int = 400) -> None:
        """Record the JSON-RPC ``code`` and the HTTP ``status`` for REST."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ErrorObject(Model):
    """The JSON-RPC 2.0 error object, also the body of a REST error."""

    code: int
    message: str
    data: dict[str, Any] | None = None


class Request(BaseModel):
    """A JSON-RPC 2.0 request. A request without ``id`` is a notification."""

    model_config = ConfigDict(extra="forbid", strict=True)

    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any] | list[Any] | None = None
    id: int | str | None = None

    @property
    def is_notification(self) -> bool:
        """True if the request has no ``id`` member, so no reply is sent."""
        return "id" not in self.model_fields_set


class NoParams(Model):
    """Parameters of a method that takes none."""


class EvaluateSourceParams(Model):
    """Parameters of ``evaluate_source``.

    The daemon never reads files named by a caller. Client.evaluate reads
    the file itself and sends its text here.
    """

    source: str
    filename: str = "<string>"


class DerivationParams(Model):
    """Parameters of ``build`` and ``check``."""

    derivation: Derivation


class SwitchParams(Model):
    """Parameters of ``switch``."""

    profile: str
    derivation: Derivation


class PushParams(Model):
    """Parameters of ``push``."""

    paths: tuple[str, ...]
    cache: str


class ServerInfo(Model):
    """The reply to ``GET /v1/info``."""

    version: str
    api_version: str
    methods: tuple[str, ...]


def error_object(error: BaseException) -> tuple[ErrorObject, int]:
    """Return the error object and HTTP status that report ``error`` to a client.

    Errors that are neither LatticeError, RPCError nor NotImplementedError
    are internal. Their message is not sent, because it may describe the
    daemon's machine; the daemon logs it instead.
    """
    if isinstance(error, RPCError):
        return ErrorObject(code=error.code, message=error.message), error.status
    if isinstance(error, NotImplementedError):
        message = str(error) or "not implemented yet"
        return ErrorObject(code=NOT_IMPLEMENTED, message=message), 501
    if isinstance(error, LatticeError):
        for cls in type(error).__mro__:
            if cls in ERROR_CODES:
                code, status = ERROR_CODES[cls]
                data = {"type": type(error).__name__}
                return ErrorObject(code=code, message=str(error), data=data), status
    return ErrorObject(code=INTERNAL_ERROR, message="internal error"), 500


def raise_error(error: ErrorObject) -> NoReturn:
    """Raise the exception a client should see for ``error`` from the daemon."""
    if error.code == NOT_IMPLEMENTED:
        raise NotImplementedError(error.message)
    name = error.data.get("type") if error.data else None
    cls = _ERROR_TYPES.get(name) if isinstance(name, str) else None
    if cls is not None:
        raise cls(error.message)
    if PARSE_ERROR <= error.code <= INVALID_REQUEST:
        raise errors.ProtocolError(f"{error.message} (code {error.code})")
    raise LatticeError(f"{error.message} (code {error.code})")


def success(request_id: int | str | None, result: object) -> dict[str, object]:
    """Return a JSON-RPC response carrying ``result``."""
    return {"jsonrpc": JSONRPC_VERSION, "result": result, "id": request_id}


def failure(request_id: int | str | None, error: ErrorObject) -> dict[str, object]:
    """Return a JSON-RPC response carrying ``error``."""
    return {"jsonrpc": JSONRPC_VERSION, "error": error.model_dump(mode="json"), "id": request_id}


def event_notification(event: Event) -> dict[str, object]:
    """Return the JSON-RPC notification that carries ``event`` to a client."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "method": EVENT_METHOD,
        "params": event.model_dump(mode="json"),
    }
