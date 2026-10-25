# SPDX-License-Identifier: GPL-2.0
"""Daemon transports: the Unix socket and the TLS remote API (RFC 0001 section 6.2).

Both transports serve the HTTP interface described in protocol.py with
the standard library's HTTP server, one thread per connection. They differ
only in how a caller is identified:

* On the Unix socket the kernel reports the caller's user and groups
  (``SO_PEERCRED``). Nothing the caller sends can change them.
* Over TLS 1.3 the caller presents a client certificate signed by
  ``tls_client_ca`` and listed in ``clients``, or a bearer token whose
  SHA-256 digest is listed in ``tokens``. A certificate takes precedence.

The socket file is world-writable, so any local user can connect. The
peer credentials, not the file mode, decide what a caller may do.
"""

from __future__ import annotations

import grp
import hashlib
import hmac
import json
import logging
import os
import pwd
import socket
import socketserver
import ssl
import stat
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, ClassVar, Literal

from .errors import AuthenticationFailed, LatticeError, PermissionDenied, SettingsError
from .events import Event, EventHandler
from .models import Identity, Role
from .protocol import (
    API_VERSION,
    INFO_PATH,
    INVALID_REQUEST,
    JSON,
    METHOD_NOT_FOUND,
    NDJSON,
    REST_PREFIX,
    RPC_PATH,
    RPCError,
    ServerInfo,
    error_object,
    event_notification,
)
from .server import METHODS, BackendFactory, Dispatcher
from .settings import DaemonSettings, Settings

logger = logging.getLogger("lattice.daemon")


def _group_id(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def _common_name(cert: dict[str, Any]) -> str | None:
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class _Context:
    """Configuration every connection handler reads from its server."""

    transport: ClassVar[Literal["unix", "tls"]]
    dispatcher: Dispatcher
    settings: DaemonSettings

    def handle_error(self, request: object, client_address: object) -> None:
        """Log a failed connection, such as a TLS handshake error, without a traceback on stderr."""
        logger.debug("connection failed", exc_info=True)


class _UnixServer(_Context, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    transport = "unix"
    daemon_threads = True


class _TLSServer(_Context, socketserver.ThreadingMixIn, socketserver.TCPServer):
    transport = "tls"
    daemon_threads = True
    allow_reuse_address = True


class _TLSServer6(_TLSServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _UnixServer | _TLSServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"
    server_version = "lattice"
    sys_version = ""
    timeout = 60.0

    def setup(self) -> None:
        """Complete the TLS handshake in this connection's thread, not the accept loop."""
        self.request.settimeout(self.timeout)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def address_string(self) -> str:
        """Name the transport in log lines; a Unix socket peer has no address."""
        return self.server.transport

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Send the access log to the ``lattice.daemon`` logger."""
        logger.debug("%s %s", self.address_string(), format % args)

    def _send_json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        if value is not None:
            self.send_header("Content-Type", JSON)
        self.send_header("Content-Length", str(len(body)))
        for key, header in (headers or {}).items():
            self.send_header(key, header)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: Exception) -> None:
        obj, status = error_object(error)
        headers = {}
        if status == 401 and self.server.settings.tokens:
            headers["WWW-Authenticate"] = 'Bearer realm="lattice"'
        self._send_json(status, {"error": obj.model_dump(mode="json")}, headers)

    def _write_line(self, value: object) -> None:
        self.wfile.write(json.dumps(value).encode() + b"\n")
        self.wfile.flush()

    def _unix_identity(self) -> Identity:
        creds = self.request.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _pid, uid, gid = struct.unpack("3i", creds)
        try:
            name = pwd.getpwuid(uid).pw_name
            groups = set(os.getgrouplist(name, gid))
        except KeyError:
            name, groups = str(uid), {gid}
        settings = self.server.settings
        if uid in (0, os.getuid()) or _group_id(settings.admin_group) in groups:
            return Identity(name=name, role=Role.ADMIN, transport="unix")
        if _group_id(settings.user_group) in groups:
            return Identity(name=name, role=Role.USER, transport="unix")
        raise PermissionDenied(f"{name} is not a member of {settings.user_group}")

    def _tls_identity(self) -> Identity:
        settings = self.server.settings
        cert = self.request.getpeercert() if isinstance(self.request, ssl.SSLSocket) else None
        if cert:
            name = _common_name(dict(cert))
            for grant in settings.clients:
                if name is not None and grant.common_name == name:
                    return Identity(name=name, role=grant.role, transport="tls")
            raise PermissionDenied(f"client certificate {name!r} is not listed in clients")
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            digest = hashlib.sha256(token.strip().encode()).hexdigest()
            found = None
            for grant in settings.tokens:
                if hmac.compare_digest(grant.sha256, digest):
                    found = grant
            if found is not None:
                return Identity(name=found.name, role=found.role, transport="tls")
        raise AuthenticationFailed("a client certificate or a valid bearer token is required")

    def _identity(self) -> Identity | None:
        try:
            if self.server.transport == "unix":
                return self._unix_identity()
            return self._tls_identity()
        except LatticeError as error:
            logger.info("refused %s caller: %s", self.server.transport, error)
            self._send_error(error)
            return None

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_error(RPCError(INVALID_REQUEST, "Content-Length is required", 411))
            return None
        if length < 0 or length > self.server.settings.max_request_bytes:
            self._send_error(RPCError(INVALID_REQUEST, "request body too large", 413))
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        """Serve ``GET /v1/info``."""
        if urllib.parse.urlsplit(self.path).path != INFO_PATH:
            self._send_error(RPCError(METHOD_NOT_FOUND, "not found", 404))
            return
        if self._identity() is None:
            return
        from . import __version__

        info = ServerInfo(version=__version__, api_version=API_VERSION, methods=tuple(METHODS))
        self._send_json(200, info.model_dump(mode="json"))

    def do_POST(self) -> None:  # noqa: N802
        """Serve JSON-RPC at ``/v1/rpc`` and REST at ``/v1/<method>``."""
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith(REST_PREFIX):
            self._send_error(RPCError(METHOD_NOT_FOUND, "not found", 404))
            return
        identity = self._identity()
        if identity is None:
            return
        body = self._read_body()
        if body is None:
            return
        dispatcher = self.server.dispatcher
        if path != RPC_PATH:
            status, value = dispatcher.handle_rest(path[len(REST_PREFIX) :], body, identity)
            self._send_json(status, value)
        elif NDJSON in self.headers.get("Accept", ""):
            self.send_response(200)
            self.send_header("Content-Type", NDJSON)
            self.end_headers()

            def on_event(event: Event) -> None:
                self._write_line(event_notification(event))

            reply = dispatcher.handle_rpc(body, identity, on_event)
            if reply is not None:
                self._write_line(reply)
        else:
            reply = dispatcher.handle_rpc(body, identity)
            self._send_json(204 if reply is None else 200, reply)


def _remove_stale_socket(path: str) -> None:
    """Remove a socket file left by a daemon that is no longer running."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise LatticeError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        os.unlink(path)
        return
    finally:
        probe.close()
    raise LatticeError(f"another daemon is listening on {path}")


def _split_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    try:
        number = int(port)
    except ValueError:
        number = -1
    if not host or not 0 <= number <= 65535:
        raise SettingsError(f"listen {listen!r} must be host:port")
    return host, number


class Daemon:
    """``lattice daemon``: serves a LatticeAPI backend to other processes.

    ``settings.daemon`` chooses the socket, groups and remote API.
    ``factory`` creates the backend for each request; by default it is a
    Lattice object with ``settings``, so the daemon builds with its own
    settings, never the caller's.

    Use start and stop, or a ``with`` block, to run the daemon in
    background threads, and serve_forever to run it until interrupted.
    """

    def __init__(self, settings: Settings | None = None, factory: BackendFactory | None = None):
        """Prepare the daemon. Nothing is bound until start is called."""
        self.settings = settings if settings is not None else Settings()
        if factory is None:
            from .api import Lattice

            own = self.settings

            def default_factory(on_event: EventHandler | None) -> Lattice:
                return Lattice(own, on_event=on_event)

            factory = default_factory
        self.dispatcher = Dispatcher(factory)
        self._servers: list[_UnixServer | _TLSServer] = []
        self._threads: list[threading.Thread] = []

    def _context(self, server: _UnixServer | _TLSServer) -> None:
        server.dispatcher = self.dispatcher
        server.settings = self.settings.daemon

    def _unix_server(self) -> _UnixServer:
        path = self.settings.daemon.socket
        _remove_stale_socket(path)
        server = _UnixServer(path, _Handler, bind_and_activate=False)
        try:
            server.server_bind()
            os.chmod(path, 0o666)  # noqa: S103  access is decided by peer credentials
            server.server_activate()
        except BaseException:
            server.server_close()
            raise
        self._context(server)
        return server

    def _tls_server(self, listen: str) -> _TLSServer:
        daemon = self.settings.daemon
        if daemon.tls_cert is None or daemon.tls_key is None:
            raise SettingsError("listen needs tls_cert and tls_key")
        host, port = _split_listen(listen)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(daemon.tls_cert, daemon.tls_key)
        if daemon.tls_client_ca is not None:
            context.load_verify_locations(daemon.tls_client_ca)
            context.verify_mode = ssl.CERT_OPTIONAL if daemon.tokens else ssl.CERT_REQUIRED
        cls = _TLSServer6 if ":" in host else _TLSServer
        server = cls((host, port), _Handler)
        server.socket = context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )
        self._context(server)
        return server

    @property
    def socket_path(self) -> str:
        """The path of the Unix socket."""
        return self.settings.daemon.socket

    @property
    def tls_address(self) -> tuple[str, int] | None:
        """The host and port the remote API is bound to, once started; None if disabled."""
        for server in self._servers:
            if isinstance(server, _TLSServer):
                host, port = server.server_address[:2]
                return str(host), int(port)
        return None

    def start(self) -> None:
        """Bind the socket and, if configured, the TLS port, and serve in background threads."""
        if self._servers:
            raise LatticeError("the daemon is already running")
        try:
            self._servers.append(self._unix_server())
            if self.settings.daemon.listen is not None:
                self._servers.append(self._tls_server(self.settings.daemon.listen))
        except BaseException:
            self.stop()
            raise
        for server in self._servers:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"lattice-{server.transport}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        logger.info("daemon listening on %s", self.socket_path)

    def stop(self) -> None:
        """Stop serving, close the listeners and remove the socket file."""
        for server, thread in zip(self._servers, self._threads, strict=False):
            server.shutdown()
            thread.join()
        for server in self._servers:
            server.server_close()
            if isinstance(server, _UnixServer):
                try:
                    os.unlink(self.socket_path)
                except FileNotFoundError:
                    pass
        self._servers.clear()
        self._threads.clear()

    def serve_forever(self) -> None:
        """Run until interrupted with SIGINT, then stop."""
        self.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def __enter__(self) -> Daemon:
        """Start the daemon."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the daemon."""
        self.stop()
