# SPDX-License-Identifier: GPL-2.0
"""Daemon dispatch and permission tests: RFC 0001 section 6.2."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from lattice import Event, EventHandler, Identity, LatticeAPI, PermissionDenied, Role
from lattice.protocol import INVALID_PARAMS, NOT_IMPLEMENTED, NoParams
from lattice.server import Dispatcher, authorize, may_switch

ADMIN = Identity(name="root", role=Role.ADMIN, transport="unix")
ALICE = Identity(name="alice", role=Role.USER, transport="tls")

Factory = Callable[[EventHandler | None], LatticeAPI]


def rpc(dispatcher: Dispatcher, value: object, who: Identity = ADMIN) -> Any:
    return dispatcher.handle_rpc(json.dumps(value).encode(), who)


def test_rpc_call_returns_result(factory: Factory) -> None:
    reply = rpc(Dispatcher(factory), {"jsonrpc": "2.0", "method": "gc", "id": 7})
    assert reply == {
        "jsonrpc": "2.0",
        "result": {"removed": ["/lattice/store/a-x"], "freed_bytes": 3, "seconds": 0.5},
        "id": 7,
    }


def test_rpc_passes_events(factory: Factory) -> None:
    seen: list[Event] = []
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "evaluate_source", "params": {"source": "x"}, "id": 1}
    )
    Dispatcher(factory).handle_rpc(body.encode(), ALICE, seen.append)
    assert [e.model_dump()["type"] for e in seen] == ["evaluation_started"]


def test_rpc_user_may_not_collect(factory: Factory) -> None:
    reply = rpc(Dispatcher(factory), {"jsonrpc": "2.0", "method": "gc", "id": 1}, ALICE)
    assert reply["error"]["code"] == -32003
    assert reply["error"]["data"] == {"type": "PermissionDenied"}


def test_rpc_unknown_method(factory: Factory) -> None:
    reply = rpc(Dispatcher(factory), {"jsonrpc": "2.0", "method": "rm", "id": 1})
    assert reply["error"]["code"] == -32601


def test_rpc_invalid_params(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "evaluate_source", "params": {"src": 1}, "id": 1}
    reply = rpc(Dispatcher(factory), request)
    assert reply["error"]["code"] == -32602
    assert "source" in reply["error"]["message"]


def test_rpc_positional_params_are_refused(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "evaluate_source", "params": ["x"], "id": 1}
    assert rpc(Dispatcher(factory), request)["error"]["code"] == -32602


def test_rpc_parse_error(factory: Factory) -> None:
    reply = Dispatcher(factory).handle_rpc(b"{", ADMIN)
    assert reply == {
        "jsonrpc": "2.0",
        "error": {"code": -32700, "message": "parse error", "data": None},
        "id": None,
    }


def test_rpc_invalid_request(factory: Factory) -> None:
    reply = rpc(Dispatcher(factory), {"method": "gc", "id": 3})
    assert reply["error"]["code"] == -32600
    assert reply["id"] == 3


def test_rpc_notification_has_no_reply(factory: Factory) -> None:
    assert rpc(Dispatcher(factory), {"jsonrpc": "2.0", "method": "gc"}) is None


def test_rpc_batch(factory: Factory) -> None:
    batch = [
        {"jsonrpc": "2.0", "method": "gc", "id": 1},
        {"jsonrpc": "2.0", "method": "gc"},
        {"jsonrpc": "2.0", "method": "nope", "id": 2},
    ]
    replies = rpc(Dispatcher(factory), batch)
    assert [r["id"] for r in replies] == [1, 2]
    assert "result" in replies[0] and "error" in replies[1]


def test_rpc_empty_batch(factory: Factory) -> None:
    assert rpc(Dispatcher(factory), [])["error"]["code"] == -32600


def _without_result(factory: Factory) -> Factory:
    def make(on_event: EventHandler | None) -> LatticeAPI:
        api = factory(on_event)
        setattr(api, "push", lambda paths, cache: None)  # noqa: B010 - replaces a method
        return api

    return make


def test_rpc_method_without_result(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "push", "params": {"paths": [], "cache": "c"}, "id": 1}
    reply = rpc(Dispatcher(_without_result(factory)), request)
    assert reply == {"jsonrpc": "2.0", "result": None, "id": 1}


def test_rpc_cache_methods(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "push", "params": {"paths": ["p"], "cache": "c"}}
    request["id"] = 1
    assert rpc(Dispatcher(factory), request)["result"]["pushed"] == ["p"]
    request = {"jsonrpc": "2.0", "method": "substitute", "params": {"path": "p"}, "id": 2}
    assert rpc(Dispatcher(factory), request)["result"]["substituted"] == []
    request = {"jsonrpc": "2.0", "method": "materialise", "params": {"path": "p"}, "id": 3}
    assert rpc(Dispatcher(factory), request)["error"]["code"] == NOT_IMPLEMENTED


def test_rpc_internal_error_hides_detail(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "evaluate_source", "params": {"source": "crash"}}
    request["id"] = 1
    reply = rpc(Dispatcher(factory), request)
    assert reply["error"]["code"] == -32603
    assert "secret" not in json.dumps(reply)


def test_rest_status_codes(factory: Factory) -> None:
    d = Dispatcher(factory)
    assert d.handle_rest("gc", b"", ADMIN)[0] == 200
    assert d.handle_rest("gc", b"", ALICE)[0] == 403
    assert d.handle_rest("push", b'{"paths": [], "cache": "c"}', ALICE)[0] == 403
    empty = Dispatcher(_without_result(factory))
    assert empty.handle_rest("push", b'{"paths": [], "cache": "c"}', ADMIN) == (204, None)
    assert d.handle_rest("nope", b"{}", ADMIN)[0] == 404
    assert d.handle_rest("evaluate_source", b"{", ADMIN)[0] == 400
    status, body = d.handle_rest("evaluate_source", b'{"source": "fail"}', ALICE)
    assert status == 422
    assert body == {
        "error": {
            "code": -32010,
            "message": "<string>:1:1: rejected",
            "data": {"type": "EvaluationFailed"},
        }
    }


@pytest.mark.parametrize(
    ("profile", "allowed"),
    [
        ("per-user/alice", True),
        ("per-user/alice/dev", True),
        ("per-user/alice/../bob", False),
        ("per-user/alicex", False),
        ("per-user/bob", False),
        ("system", False),
    ],
)
def test_users_switch_only_their_profiles(profile: str, allowed: bool) -> None:
    assert may_switch(ALICE, profile) is allowed
    assert may_switch(ADMIN, profile)


def test_authorize_admin_methods() -> None:
    authorize(ADMIN, "gc", NoParams())
    with pytest.raises(PermissionDenied):
        authorize(ALICE, "gc", NoParams())


def test_rpc_not_implemented(factory: Factory) -> None:
    drv = {"name": "a", "builder": "/bin/sh"}
    request = {"jsonrpc": "2.0", "method": "build", "params": {"derivation": drv}, "id": 1}
    reply = rpc(Dispatcher(factory), request)
    assert reply["error"]["code"] == NOT_IMPLEMENTED


def test_rpc_check_rejects_unknown_backend(factory: Factory) -> None:
    drv = {"name": "a", "builder": "/bin/sh"}
    params = {"derivation": drv, "backend": "docker"}
    request = {"jsonrpc": "2.0", "method": "check", "params": params, "id": 1}
    reply = rpc(Dispatcher(factory), request)
    assert reply["error"]["code"] == INVALID_PARAMS


def test_rpc_invalid_derivation(factory: Factory) -> None:
    request = {"jsonrpc": "2.0", "method": "check", "params": {"derivation": {}}, "id": 1}
    reply = rpc(Dispatcher(factory), request)
    assert reply["error"]["code"] == INVALID_PARAMS


def test_rpc_returns_a_list_for_a_tuple(factory: Factory) -> None:
    reply = rpc(Dispatcher(factory), {"jsonrpc": "2.0", "method": "backend_status", "id": 1}, ALICE)
    assert reply["result"] == [
        {"name": "namespaces", "available": True, "reason": ""},
        {"name": "oci", "available": False, "reason": "not available in this release"},
    ]
