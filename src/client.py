# SPDX-License-Identifier: GPL-2.0
"""The daemon client and the interface it shares with Lattice (RFC 0001 section 6.2).

Client talks to ``lattice daemon`` over its Unix socket or, remotely, over
HTTPS. It has the same methods, results and errors as Lattice, so a program
written against LatticeAPI works with either::

    from lattice import Client, Lattice, LatticeAPI

    def show(api: LatticeAPI) -> None:
        for name, drv in api.evaluate("hello.pyb").derivations.items():
            print(name, drv.store_path)

    show(Lattice())                       # in this process
    show(Client())                        # through the local daemon
    show(Client("https://builder.example:7420", ca_file="ca.pem",
                cert_file="me.pem", key_file="me.key"))

Client uses the JSON-RPC interface. It opens one connection per call and
keeps no other state, so one Client may be used from several threads.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, TypeAdapter, ValidationError

from .derivation import Derivation
from .errors import ProtocolError, SettingsError
from .events import EVENTS, EventHandler, emit
from .models import (
    BuildResult,
    Evaluation,
    GCResult,
    Generation,
    PushResult,
    SlotState,
    SubstitutionResult,
    SystemGeneration,
    SystemMode,
    parse,
)
from .protocol import (
    EVENT_METHOD,
    INFO_PATH,
    JSON,
    NDJSON,
    RPC_PATH,
    CheckParams,
    DeleteGenerationsParams,
    DerivationParams,
    ErrorObject,
    EvaluateSourceParams,
    NoParams,
    PathParams,
    ProfileParams,
    PushParams,
    RollbackParams,
    RollbackSystemParams,
    RootNameParams,
    RootParams,
    ServerInfo,
    SubstituteParams,
    SwitchParams,
    SwitchSystemParams,
    raise_error,
)
from .sandbox import BackendStatus, OciConfig
from .settings import DEFAULT_SOCKET, SandboxBackend
from .store import BuildPlan, GCRootInfo, StorePathInfo, VerifyResult
from .system.config import read_config

DEFAULT_URL = f"unix://{DEFAULT_SOCKET}"


_BACKEND_STATUSES = TypeAdapter(tuple[BackendStatus, ...])
_ROOTS = TypeAdapter(tuple[GCRootInfo, ...])
_GENERATIONS = TypeAdapter(tuple[Generation, ...])
_SLOTS = TypeAdapter(tuple[SlotState, ...])
_SYSTEMS = TypeAdapter(tuple[SystemGeneration, ...])


@runtime_checkable
class LatticeAPI(Protocol):
    """The operations Lattice and Client both provide (section 6.1)."""

    def evaluate(self, path: str | os.PathLike[str]) -> Evaluation:
        """Evaluate the PyBonsai file at ``path``."""
        ...

    def evaluate_source(
        self, source: str, filename: str = "<string>", config_source: str | None = None
    ) -> Evaluation:
        """Evaluate PyBonsai source text, with ``config`` from ``config_source`` if given."""
        ...

    def build(self, drv: Derivation) -> BuildResult:
        """Realise ``drv`` and its inputs."""
        ...

    def check(self, drv: Derivation, backend: SandboxBackend | None = None) -> BuildResult:
        """Rebuild the registered output of ``drv``, on ``backend`` if given, and compare it."""
        ...

    def plan(self, drv: Derivation) -> BuildPlan:
        """List what building ``drv`` would download, build or reuse."""
        ...

    def path_info(self, path: str) -> StorePathInfo:
        """Return what the store database records about ``path``."""
        ...

    def verify(self, path: str) -> VerifyResult:
        """Hash ``path`` again and compare with the database."""
        ...

    def sandbox_config(self, drv: Derivation) -> OciConfig:
        """Describe the sandbox that would build ``drv`` as an OCI runtime configuration."""
        ...

    def backend_status(self) -> tuple[BackendStatus, ...]:
        """Report which sandbox backends can run on the machine that builds."""
        ...

    def gc(self) -> GCResult:
        """Remove every store path not reachable from a root."""
        ...

    def add_root(self, name: str, path: str) -> GCRootInfo:
        """Keep ``path`` under the root ``name``."""
        ...

    def remove_root(self, name: str) -> GCRootInfo:
        """Remove the root ``name``."""
        ...

    def roots(self) -> tuple[GCRootInfo, ...]:
        """Return every root."""
        ...

    def switch(self, profile: str, drv: Derivation) -> Generation:
        """Make ``drv`` the current generation of ``profile``."""
        ...

    def rollback(self, profile: str, generation: int | None = None) -> Generation:
        """Make an existing generation of ``profile`` current again."""
        ...

    def generations(self, profile: str) -> tuple[Generation, ...]:
        """Return every generation of ``profile``."""
        ...

    def delete_generations(self, profile: str, keep: int) -> tuple[Generation, ...]:
        """Delete all but the newest ``keep`` generations and the current one."""
        ...

    def slots(self) -> tuple[SlotState, ...]:
        """Return the active and wanted stack of every slot."""
        ...

    def switch_system(self, drv: Derivation, mode: SystemMode = "switch") -> SystemGeneration:
        """Build the system ``drv`` and activate it in ``mode``."""
        ...

    def rollback_system(self, generation: int | None = None) -> SystemGeneration:
        """Make an earlier system generation current and activate it."""
        ...

    def systems(self) -> tuple[SystemGeneration, ...]:
        """Return the system generations."""
        ...

    def substitute(self, path: str, shallow: bool = False) -> SubstitutionResult:
        """Fetch ``path`` and its missing references from the configured caches."""
        ...

    def materialise(self, path: str) -> StorePathInfo:
        """Turn the shallow path ``path`` into a full copy."""
        ...

    def push(self, paths: Iterable[str], cache: str) -> PushResult:
        """Sign and publish store paths, their closures and provenance to ``cache``."""
        ...


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path
        self._timeout = timeout

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class Client:
    """A connection to ``lattice daemon``.

    ``url`` is ``unix:///path/to/socket`` (the default is the daemon's
    standard socket) or ``https://host:port``; plain ``http`` is refused.
    For HTTPS, ``ca_file`` verifies the daemon's certificate (the system
    store is used if it is None), and the caller authenticates with
    ``cert_file`` and ``key_file``, with ``token``, or with both. Over the
    Unix socket the daemon identifies the caller from the kernel, so no
    credentials are passed.

    ``timeout`` applies to each socket operation and is None (no limit) by
    default, because a build may take hours. ``on_event`` receives the
    daemon's progress events as they happen.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        ca_file: str | None = None,
        cert_file: str | None = None,
        key_file: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        """Check the URL and credentials and prepare TLS. Nothing is connected yet."""
        parts = urllib.parse.urlsplit(url)
        self.url = url
        self.timeout = timeout
        self.on_event = on_event
        self._token = token
        self._ssl: ssl.SSLContext | None = None
        if parts.scheme == "unix":
            if not parts.path:
                raise SettingsError(f"{url!r} names no socket path")
            if token is not None or cert_file is not None:
                raise SettingsError("credentials are not used over the Unix socket")
            self._path = parts.path
        elif parts.scheme == "https":
            if parts.hostname is None:
                raise SettingsError(f"{url!r} names no host")
            self._host = parts.hostname
            self._port = parts.port or 443
            context = ssl.create_default_context(cafile=ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            if cert_file is not None:
                context.load_cert_chain(cert_file, key_file)
            self._ssl = context
        else:
            raise SettingsError(f"{url!r}: the daemon URL must start with unix:// or https://")

    def _connect(self) -> http.client.HTTPConnection:
        if self._ssl is None:
            return _UnixConnection(self._path, self.timeout)
        return http.client.HTTPSConnection(
            self._host, self._port, timeout=self.timeout, context=self._ssl
        )

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Content-Type": JSON, "Accept": accept}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _http_error(response: http.client.HTTPResponse) -> ProtocolError:
        """Raise the error in a non-200 reply, or return one if it has none."""
        body = response.read()
        try:
            error = parse(ErrorObject, json.loads(body)["error"])
        except (ValueError, KeyError, TypeError):
            return ProtocolError(f"daemon replied with HTTP {response.status}")
        raise_error(error)

    def _call(self, method: str, params: BaseModel) -> Any:
        stream = self.on_event is not None
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params.model_dump(mode="json"),
                "id": 1,
            }
        ).encode()
        connection = self._connect()
        try:
            connection.request("POST", RPC_PATH, body, self._headers(NDJSON if stream else JSON))
            response = connection.getresponse()
            if response.status != 200:
                raise self._http_error(response)
            reply: Any = None
            try:
                if stream:
                    for line in response:
                        message = json.loads(line)
                        if message.get("method") == EVENT_METHOD:
                            emit(self.on_event, EVENTS.validate_json(json.dumps(message["params"])))
                        else:
                            reply = message
                else:
                    reply = json.loads(response.read())
            except (ValueError, KeyError, TypeError, ValidationError) as error:
                raise ProtocolError(f"invalid reply from the daemon: {error}") from error
        finally:
            connection.close()
        if not isinstance(reply, dict):
            raise ProtocolError("the daemon closed the connection without a reply")
        if "error" in reply:
            try:
                error = parse(ErrorObject, reply["error"])
            except ValidationError as exc:
                raise ProtocolError("invalid error object from the daemon") from exc
            raise_error(error)
        if "result" not in reply:
            raise ProtocolError("reply has neither result nor error")
        return reply["result"]

    @staticmethod
    def _result[M: BaseModel](model: type[M], value: object) -> M:
        try:
            return parse(model, value)
        except ValidationError as error:
            raise ProtocolError(f"invalid {model.__name__} from the daemon: {error}") from error

    def info(self) -> ServerInfo:
        """Return the daemon's version, API version and method names."""
        connection = self._connect()
        try:
            connection.request("GET", INFO_PATH, headers=self._headers(JSON))
            response = connection.getresponse()
            if response.status != 200:
                raise self._http_error(response)
            value = json.loads(response.read())
        finally:
            connection.close()
        return self._result(ServerInfo, value)

    def evaluate(self, path: str | os.PathLike[str]) -> Evaluation:
        """Read the file at ``path`` here and have the daemon evaluate its text.

        The ``config.pyb`` next to it, if any, is read here and sent too.
        """
        source = Path(path).read_text(encoding="utf-8")
        return self.evaluate_source(source, os.fspath(path), read_config(path))

    def evaluate_source(
        self, source: str, filename: str = "<string>", config_source: str | None = None
    ) -> Evaluation:
        """Have the daemon evaluate PyBonsai source text."""
        params = EvaluateSourceParams(source=source, filename=filename, config_source=config_source)
        return self._result(Evaluation, self._call("evaluate_source", params))

    def build(self, drv: Derivation) -> BuildResult:
        """Have the daemon realise ``drv`` and its inputs."""
        params = DerivationParams(derivation=drv)
        return self._result(BuildResult, self._call("build", params))

    def check(self, drv: Derivation, backend: SandboxBackend | None = None) -> BuildResult:
        """Have the daemon rebuild ``drv``, on ``backend`` if given, and compare the outputs."""
        params = CheckParams(derivation=drv, backend=backend)
        return self._result(BuildResult, self._call("check", params))

    def plan(self, drv: Derivation) -> BuildPlan:
        """Have the daemon list what building ``drv`` would download, build or reuse."""
        params = DerivationParams(derivation=drv)
        return self._result(BuildPlan, self._call("plan", params))

    def path_info(self, path: str) -> StorePathInfo:
        """Have the daemon report what its store database records about ``path``."""
        return self._result(StorePathInfo, self._call("path_info", PathParams(path=path)))

    def verify(self, path: str) -> VerifyResult:
        """Have the daemon hash ``path`` again and compare with its database."""
        return self._result(VerifyResult, self._call("verify", PathParams(path=path)))

    def sandbox_config(self, drv: Derivation) -> OciConfig:
        """Have the daemon describe the sandbox that would build ``drv``."""
        params = DerivationParams(derivation=drv)
        return self._result(OciConfig, self._call("sandbox_config", params))

    def backend_status(self) -> tuple[BackendStatus, ...]:
        """Have the daemon report which sandbox backends can run on its machine."""
        value = self._call("backend_status", NoParams())
        try:
            return _BACKEND_STATUSES.validate_json(json.dumps(value))
        except ValidationError as error:
            raise ProtocolError(f"invalid backend status from the daemon: {error}") from error

    def gc(self) -> GCResult:
        """Have the daemon collect garbage. Needs the admin role."""
        return self._result(GCResult, self._call("gc", NoParams()))

    def add_root(self, name: str, path: str) -> GCRootInfo:
        """Have the daemon keep ``path`` under the root ``name``."""
        params = RootParams(name=name, path=path)
        return self._result(GCRootInfo, self._call("add_root", params))

    def remove_root(self, name: str) -> GCRootInfo:
        """Have the daemon remove the root ``name``."""
        return self._result(GCRootInfo, self._call("remove_root", RootNameParams(name=name)))

    def roots(self) -> tuple[GCRootInfo, ...]:
        """Return every root of the daemon's store."""
        return self._tuple(_ROOTS, self._call("roots", NoParams()), "roots")

    @staticmethod
    def _tuple[T](adapter: TypeAdapter[tuple[T, ...]], value: object, what: str) -> tuple[T, ...]:
        try:
            return adapter.validate_json(json.dumps(value))
        except ValidationError as error:
            raise ProtocolError(f"invalid {what} from the daemon: {error}") from error

    def switch(self, profile: str, drv: Derivation) -> Generation:
        """Have the daemon make ``drv`` the current generation of ``profile``."""
        params = SwitchParams(profile=profile, derivation=drv)
        return self._result(Generation, self._call("switch", params))

    def rollback(self, profile: str, generation: int | None = None) -> Generation:
        """Have the daemon make an existing generation of ``profile`` current again."""
        params = RollbackParams(profile=profile, generation=generation)
        return self._result(Generation, self._call("rollback", params))

    def generations(self, profile: str) -> tuple[Generation, ...]:
        """Return every generation of ``profile`` in the daemon's store."""
        value = self._call("generations", ProfileParams(profile=profile))
        return self._tuple(_GENERATIONS, value, "generations")

    def delete_generations(self, profile: str, keep: int) -> tuple[Generation, ...]:
        """Have the daemon delete all but the newest ``keep`` generations and the current one."""
        params = DeleteGenerationsParams(profile=profile, keep=keep)
        value = self._call("delete_generations", params)
        return self._tuple(_GENERATIONS, value, "generations")

    def slots(self) -> tuple[SlotState, ...]:
        """Return the slots of the daemon's machine."""
        return self._tuple(_SLOTS, self._call("slots", NoParams()), "slots")

    def switch_system(self, drv: Derivation, mode: SystemMode = "switch") -> SystemGeneration:
        """Have the daemon build the system ``drv`` and activate it in ``mode``."""
        params = SwitchSystemParams(derivation=drv, mode=mode)
        return self._result(SystemGeneration, self._call("switch_system", params))

    def rollback_system(self, generation: int | None = None) -> SystemGeneration:
        """Have the daemon activate an earlier system generation."""
        params = RollbackSystemParams(generation=generation)
        return self._result(SystemGeneration, self._call("rollback_system", params))

    def systems(self) -> tuple[SystemGeneration, ...]:
        """Return the system generations of the daemon's machine."""
        return self._tuple(_SYSTEMS, self._call("systems", NoParams()), "systems")

    def substitute(self, path: str, shallow: bool = False) -> SubstitutionResult:
        """Have the daemon fetch ``path`` from its caches."""
        params = SubstituteParams(path=path, shallow=shallow)
        return self._result(SubstitutionResult, self._call("substitute", params))

    def materialise(self, path: str) -> StorePathInfo:
        """Have the daemon turn a shallow path into a full copy. Needs the admin role."""
        return self._result(StorePathInfo, self._call("materialise", PathParams(path=path)))

    def push(self, paths: Iterable[str], cache: str) -> PushResult:
        """Have the daemon push ``paths`` to ``cache``. Needs the admin role."""
        params = PushParams(paths=tuple(paths), cache=cache)
        return self._result(PushResult, self._call("push", params))
