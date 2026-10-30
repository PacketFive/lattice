# SPDX-License-Identifier: GPL-2.0
"""The daemon core: permissions and dispatch (RFC 0001 section 6.2).

Dispatcher turns one decoded request into one call on a LatticeAPI
backend, normally a Lattice object owned by the daemon, after checking that
the caller's Identity may make it. It knows nothing about sockets or HTTP;
transport.py reads requests, establishes the Identity and writes replies.

Permissions:

======================  ======  ============================================
Method                  Role    Further rule
======================  ======  ============================================
``evaluate_source``     user
``build``               user
``check``               user
``plan``                user
``path_info``           user
``verify``              user
``substitute``          user
``roots``               user
``add_root``            user    A user may only add or remove roots named
``remove_root``         user    ``per-user/<name>`` or below it; admins any.
``generations``         user
``slots``               user
``switch_system``       admin
``rollback_system``     admin
``systems``             admin
``switch``              user    A user may only switch, roll back or delete
``rollback``            user    generations of ``per-user/<name>`` and
``delete_generations``  user    profiles below it; admins any profile.
``gc``                  admin
``materialise``         admin
``push``                admin
======================  ======  ============================================
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .client import LatticeAPI
from .errors import PermissionDenied
from .events import EventHandler
from .models import Identity, Model, Role, parse
from .protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    CheckParams,
    DeleteGenerationsParams,
    DerivationParams,
    EvaluateSourceParams,
    NoParams,
    PathParams,
    ProfileParams,
    PushParams,
    Request,
    RollbackParams,
    RollbackSystemParams,
    RootNameParams,
    RootParams,
    RPCError,
    SubstituteParams,
    SwitchParams,
    SwitchSystemParams,
    error_object,
    failure,
    success,
)

logger = logging.getLogger("lattice.daemon")

BackendFactory = Callable[[EventHandler | None], LatticeAPI]
"""Creates the backend for one request, with that request's event handler."""


@dataclass(frozen=True)
class Method:
    """One operation the daemon offers: its parameters, required role and call."""

    params: type[Model]
    role: Role
    call: Callable[[LatticeAPI, Any], BaseModel | tuple[BaseModel, ...] | None]


METHODS: dict[str, Method] = {
    "evaluate_source": Method(
        EvaluateSourceParams,
        Role.USER,
        lambda api, p: api.evaluate_source(p.source, p.filename, p.config_source),
    ),
    "build": Method(DerivationParams, Role.USER, lambda api, p: api.build(p.derivation)),
    "check": Method(CheckParams, Role.USER, lambda api, p: api.check(p.derivation, p.backend)),
    "plan": Method(DerivationParams, Role.USER, lambda api, p: api.plan(p.derivation)),
    "path_info": Method(PathParams, Role.USER, lambda api, p: api.path_info(p.path)),
    "verify": Method(PathParams, Role.USER, lambda api, p: api.verify(p.path)),
    "roots": Method(NoParams, Role.USER, lambda api, p: api.roots()),
    "add_root": Method(RootParams, Role.USER, lambda api, p: api.add_root(p.name, p.path)),
    "remove_root": Method(RootNameParams, Role.USER, lambda api, p: api.remove_root(p.name)),
    "switch": Method(SwitchParams, Role.USER, lambda api, p: api.switch(p.profile, p.derivation)),
    "rollback": Method(
        RollbackParams, Role.USER, lambda api, p: api.rollback(p.profile, p.generation)
    ),
    "generations": Method(ProfileParams, Role.USER, lambda api, p: api.generations(p.profile)),
    "delete_generations": Method(
        DeleteGenerationsParams,
        Role.USER,
        lambda api, p: api.delete_generations(p.profile, p.keep),
    ),
    "slots": Method(NoParams, Role.USER, lambda api, p: api.slots()),
    "switch_system": Method(
        SwitchSystemParams, Role.ADMIN, lambda api, p: api.switch_system(p.derivation, p.mode)
    ),
    "rollback_system": Method(
        RollbackSystemParams, Role.ADMIN, lambda api, p: api.rollback_system(p.generation)
    ),
    "systems": Method(NoParams, Role.ADMIN, lambda api, p: api.systems()),
    "sandbox_config": Method(
        DerivationParams, Role.USER, lambda api, p: api.sandbox_config(p.derivation)
    ),
    "backend_status": Method(NoParams, Role.USER, lambda api, p: api.backend_status()),
    "gc": Method(NoParams, Role.ADMIN, lambda api, p: api.gc()),
    "substitute": Method(
        SubstituteParams, Role.USER, lambda api, p: api.substitute(p.path, p.shallow)
    ),
    "materialise": Method(PathParams, Role.ADMIN, lambda api, p: api.materialise(p.path)),
    "push": Method(PushParams, Role.ADMIN, lambda api, p: api.push(p.paths, p.cache)),
}


def may_switch(identity: Identity, profile: str) -> bool:
    """Return True if ``identity`` may change ``profile`` or the root of that name.

    Admins may change any profile or root. A user may change
    ``per-user/<name>`` and names below it, where ``<name>`` is the user's
    own name.
    """
    if identity.role is Role.ADMIN:
        return True
    own = f"per-user/{identity.name}"
    parts = profile.split("/")
    return (profile == own or profile.startswith(own + "/")) and ".." not in parts


def authorize(identity: Identity, name: str, params: BaseModel) -> None:
    """Raise PermissionDenied unless ``identity`` may call ``name`` with ``params``."""
    method = METHODS[name]
    if method.role is Role.ADMIN and identity.role is not Role.ADMIN:
        raise PermissionDenied(f"{name} needs the admin role")
    if isinstance(
        params, SwitchParams | RollbackParams | DeleteGenerationsParams
    ) and not may_switch(identity, params.profile):
        raise PermissionDenied(f"{identity.name} may not change profile {params.profile!r}")
    if isinstance(params, RootParams | RootNameParams) and not may_switch(identity, params.name):
        raise PermissionDenied(f"{identity.name} may not change root {params.name!r}")


def _invalid_params(error: ValidationError) -> RPCError:
    parts = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        parts.append(f"{where}: {item['msg']}" if where else item["msg"])
    return RPCError(INVALID_PARAMS, "invalid params: " + "; ".join(parts))


class Dispatcher:
    """Checks and runs requests for the transports.

    ``factory`` creates the backend for each request, so each request has
    its own event handler and nothing is shared between callers.
    """

    def __init__(self, factory: BackendFactory) -> None:
        """Keep ``factory``; no backend is created until a request arrives."""
        self.factory = factory

    def call(
        self,
        name: str,
        params: object,
        identity: Identity,
        on_event: EventHandler | None = None,
    ) -> object:
        """Run method ``name`` and return its result as a JSON value.

        Raises RPCError for an unknown method or invalid params,
        PermissionDenied if the caller may not run it, and whatever the
        backend raises.
        """
        method = METHODS.get(name)
        if method is None:
            raise RPCError(METHOD_NOT_FOUND, f"unknown method {name!r}", 404)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RPCError(INVALID_PARAMS, "params must be an object")
        try:
            model = parse(method.params, params)
        except ValidationError as error:
            raise _invalid_params(error) from None
        authorize(identity, name, model)
        logger.info("%s (%s) calls %s", identity.name, identity.transport, name)
        result = method.call(self.factory(on_event), model)
        if result is None:
            return None
        if isinstance(result, tuple):
            return [item.model_dump(mode="json") for item in result]
        return result.model_dump(mode="json")

    def handle_rpc_value(
        self, value: object, identity: Identity, on_event: EventHandler | None = None
    ) -> dict[str, object] | None:
        """Run one decoded JSON-RPC request and return its response, or None for a notification."""
        request_id = None
        if isinstance(value, dict):
            raw_id = value.get("id")
            request_id = raw_id if isinstance(raw_id, int | str) else None
        try:
            request = parse(Request, value)
        except ValidationError:
            obj, _ = error_object(RPCError(INVALID_REQUEST, "invalid JSON-RPC 2.0 request"))
            return failure(request_id, obj)
        try:
            result = self.call(request.method, request.params, identity, on_event)
        except Exception as error:
            # Every failure, expected or not, becomes an error reply.
            obj, status = error_object(error)
            if status == 500:
                logger.error("internal error in %s", request.method, exc_info=error)
            return None if request.is_notification else failure(request.id, obj)
        return None if request.is_notification else success(request.id, result)

    def handle_rpc(
        self, body: bytes, identity: Identity, on_event: EventHandler | None = None
    ) -> object:
        """Run a JSON-RPC request or batch from ``body``.

        Returns the JSON value to send, or None if nothing is sent because
        every request was a notification. Events are reported only for a
        single request, never for a batch.
        """
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            obj, _ = error_object(RPCError(PARSE_ERROR, "parse error"))
            return failure(None, obj)
        if not isinstance(value, list):
            return self.handle_rpc_value(value, identity, on_event)
        if not value:
            obj, _ = error_object(RPCError(INVALID_REQUEST, "empty batch"))
            return failure(None, obj)
        replies = [r for v in value if (r := self.handle_rpc_value(v, identity)) is not None]
        return replies or None

    def handle_rest(self, name: str, body: bytes, identity: Identity) -> tuple[int, object]:
        """Run REST method ``name`` with the JSON object in ``body``.

        Returns the HTTP status and the JSON value to send: the result on
        success, None for a method without a result, and
        ``{"error": <error object>}`` on failure.
        """
        try:
            try:
                params = json.loads(body) if body else {}
            except (ValueError, RecursionError):
                raise RPCError(PARSE_ERROR, "parse error") from None
            result = self.call(name, params, identity)
        except Exception as error:
            # Every failure, expected or not, becomes an error reply.
            obj, status = error_object(error)
            if status == 500:
                logger.error("internal error in %s", name, exc_info=error)
            return status, {"error": obj.model_dump(mode="json")}
        return (204, None) if result is None else (200, result)
