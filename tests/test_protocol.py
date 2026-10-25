# SPDX-License-Identifier: GPL-2.0
"""Protocol and data model tests: RFC 0001 section 6.2."""

import json

import pytest
from pydantic import ValidationError

from lattice import (
    BuildFailed,
    EvaluationFailed,
    EvaluationFinished,
    EvaluationStarted,
    GCResult,
    LatticeError,
    PermissionDenied,
    ProtocolError,
)
from lattice.events import EVENTS
from lattice.models import DerivationWire, derivation_from_wire, parse
from lattice.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    NOT_IMPLEMENTED,
    ErrorObject,
    Request,
    RPCError,
    error_object,
    event_notification,
    raise_error,
)

not_implemented = pytest.mark.xfail(raises=NotImplementedError, reason="not implemented yet")


def test_results_round_trip_through_json() -> None:
    result = GCResult(removed=("/lattice/store/a-x",), freed_bytes=3, seconds=0.5)
    assert parse(GCResult, json.loads(result.model_dump_json())) == result


def test_models_are_strict() -> None:
    with pytest.raises(ValidationError):
        parse(GCResult, {"removed": [], "freed_bytes": "3", "seconds": 0})


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        parse(GCResult, {"removed": [], "freed_bytes": 3, "seconds": 0, "extra": 1})


def test_events_are_rebuilt_by_type() -> None:
    for event in (
        EvaluationStarted(filename="a.pyb"),
        EvaluationFinished(filename="a.pyb", derivations=2, seconds=0.1),
    ):
        message = event_notification(event)
        assert message["method"] == "event"
        assert EVENTS.validate_json(json.dumps(message["params"])) == event


def test_request_without_id_is_notification() -> None:
    assert parse(Request, {"jsonrpc": "2.0", "method": "gc"}).is_notification
    assert not parse(Request, {"jsonrpc": "2.0", "method": "gc", "id": None}).is_notification


def test_request_needs_version_2() -> None:
    with pytest.raises(ValidationError):
        parse(Request, {"jsonrpc": "1.0", "method": "gc", "id": 1})


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (EvaluationFailed("x"), -32010, 422),
        (BuildFailed("x"), -32011, 422),
        (PermissionDenied("x"), -32003, 403),
        (LatticeError("x"), -32000, 400),
        (NotImplementedError(), NOT_IMPLEMENTED, 501),
        (RPCError(INVALID_PARAMS, "bad"), INVALID_PARAMS, 400),
    ],
)
def test_error_codes(error: Exception, code: int, status: int) -> None:
    obj, got = error_object(error)
    assert (obj.code, got) == (code, status)


def test_internal_error_hides_message() -> None:
    obj, status = error_object(RuntimeError("secret"))
    assert (obj.code, obj.message, status) == (INTERNAL_ERROR, "internal error", 500)


@pytest.mark.parametrize("cls", [EvaluationFailed, BuildFailed, PermissionDenied])
def test_errors_are_raised_as_same_class(cls: type[LatticeError]) -> None:
    obj, _ = error_object(cls("message"))
    with pytest.raises(cls, match="message"):
        raise_error(obj)


def test_evaluation_failed_from_daemon_has_no_error() -> None:
    obj, _ = error_object(EvaluationFailed("t.pyb:1:1: rejected"))
    with pytest.raises(EvaluationFailed) as info:
        raise_error(obj)
    assert info.value.error is None


def test_not_implemented_is_raised_as_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        raise_error(ErrorObject(code=NOT_IMPLEMENTED, message="later"))


def test_unknown_protocol_error() -> None:
    with pytest.raises(ProtocolError):
        raise_error(ErrorObject(code=-32600, message="invalid"))


def test_derivation_wire_nests_inputs() -> None:
    wire = parse(
        DerivationWire,
        {
            "name": "hello",
            "builder": "/bin/sh",
            "env": {"src": {"name": "src", "builder": "builtin:fetch_url"}, "x": "1"},
        },
    )
    assert isinstance(wire.env["src"], DerivationWire)
    assert wire.outputs == ("out",)


@not_implemented
def test_derivation_round_trips_through_wire() -> None:
    wire = DerivationWire(name="a", builder="/bin/sh")
    drv = derivation_from_wire(wire)
    assert drv.name == "a"
