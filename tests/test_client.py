# SPDX-License-Identifier: GPL-2.0
"""Client and daemon transport tests over a real socket: RFC 0001 section 6.2."""

import hashlib
import http.client
import json
import shutil
import socket
import ssl
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from lattice import (
    AuthenticationFailed,
    Client,
    ClientGrant,
    Daemon,
    DaemonSettings,
    EvaluationFailed,
    Event,
    EventHandler,
    LatticeAPI,
    LatticeError,
    PermissionDenied,
    Role,
    Settings,
    SettingsError,
    TokenGrant,
)

Factory = Callable[[EventHandler | None], LatticeAPI]
TOKEN = "s3cret-token"


@pytest.fixture
def unix_daemon(tmp_path: Path, factory: Factory) -> Iterator[Daemon]:
    settings = Settings(daemon=DaemonSettings(socket=str(tmp_path / "d.sock")))
    with Daemon(settings, factory) as daemon:
        yield daemon


def local(daemon: Daemon, **kwargs: object) -> Client:
    return Client(f"unix://{daemon.socket_path}", **kwargs)  # type: ignore[arg-type]


def test_client_satisfies_api_protocol() -> None:
    api: LatticeAPI = Client()
    assert isinstance(api, LatticeAPI)


def test_unix_owner_is_admin(unix_daemon: Daemon) -> None:
    result = local(unix_daemon).gc()
    assert result.removed == ("/lattice/store/a-x",)
    assert result.freed_bytes == 3


def test_unix_socket_is_world_writable(unix_daemon: Daemon) -> None:
    assert Path(unix_daemon.socket_path).stat().st_mode & 0o777 == 0o666


def test_events_stream_to_client(unix_daemon: Daemon) -> None:
    seen: list[Event] = []
    result = local(unix_daemon, on_event=seen.append).evaluate_source("x", "a.pyb")
    assert result.filename == "a.pyb"
    assert [e.model_dump() for e in seen] == [{"type": "evaluation_started", "filename": "a.pyb"}]


def test_evaluate_reads_file_on_client(unix_daemon: Daemon, tmp_path: Path) -> None:
    path = tmp_path / "a.pyb"
    path.write_text("x", encoding="utf-8")
    assert local(unix_daemon).evaluate(path).filename == str(path)


def test_errors_keep_their_class(unix_daemon: Daemon) -> None:
    with pytest.raises(EvaluationFailed, match="rejected"):
        local(unix_daemon).evaluate_source("fail", "t.pyb")


def test_internal_errors_are_hidden(unix_daemon: Daemon) -> None:
    with pytest.raises(LatticeError, match="internal error") as info:
        local(unix_daemon).evaluate_source("crash")
    assert "secret" not in str(info.value)


def test_push_without_result(unix_daemon: Daemon) -> None:
    assert local(unix_daemon).push(["/lattice/store/a-x"], "https://cache") is None


def test_info(unix_daemon: Daemon) -> None:
    info = local(unix_daemon).info()
    assert info.api_version == "1"
    assert "evaluate_source" in info.methods


def test_rest_over_unix_socket(unix_daemon: Daemon) -> None:
    from lattice.client import _UnixConnection

    connection = _UnixConnection(unix_daemon.socket_path, 5)
    connection.request("POST", "/v1/gc", b"{}", {"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read())["freed_bytes"] == 3


def test_second_daemon_is_refused(unix_daemon: Daemon, factory: Factory) -> None:
    with pytest.raises(LatticeError, match="another daemon"):
        Daemon(unix_daemon.settings, factory).start()


def test_stale_socket_is_replaced(tmp_path: Path, factory: Factory) -> None:
    path = tmp_path / "d.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))  # bound but never listening, like a crashed daemon's socket
    stale.close()
    settings = Settings(daemon=DaemonSettings(socket=str(path)))
    with Daemon(settings, factory) as daemon:
        assert local(daemon).gc().freed_bytes == 3


@pytest.mark.parametrize(
    ("url", "kwargs"),
    [
        ("http://example.org", {}),
        ("unix:///run/x.sock", {"token": "t"}),
        ("unix://", {}),
    ],
)
def test_client_refuses_unsafe_urls(url: str, kwargs: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        Client(url, **kwargs)  # type: ignore[arg-type]


# TLS remote API, with certificates made by the openssl command.

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _leaf(pki: Path, name: str, ext: str) -> None:
    (pki / f"{name}.ext").write_text(ext, encoding="ascii")
    _openssl(
        "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}",
        cwd=pki,
    )  # fmt: skip
    _openssl(
        "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "1", "-out", f"{name}.pem", "-extfile", f"{name}.ext",
        cwd=pki,
    )  # fmt: skip


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pki = tmp_path_factory.mktemp("pki")
    _openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1", "-subj", "/CN=test-ca",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        cwd=pki,
    )  # fmt: skip
    server = "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
    _leaf(pki, "daemon", server)
    for name in ("admin", "alice", "mallory"):
        _leaf(pki, name, "extendedKeyUsage=clientAuth\n")
    return pki


@pytest.fixture
def tls_daemon(tmp_path: Path, pki: Path, factory: Factory) -> Iterator[Daemon]:
    daemon_settings = DaemonSettings(
        socket=str(tmp_path / "d.sock"),
        listen="127.0.0.1:0",
        tls_cert=str(pki / "daemon.pem"),
        tls_key=str(pki / "daemon.key"),
        tls_client_ca=str(pki / "ca.pem"),
        clients=(
            ClientGrant(common_name="admin", role=Role.ADMIN),
            ClientGrant(common_name="alice"),
        ),
        tokens=(TokenGrant(name="bot", sha256=hashlib.sha256(TOKEN.encode()).hexdigest()),),
    )
    with Daemon(Settings(daemon=daemon_settings), factory) as daemon:
        yield daemon


def remote(daemon: Daemon, pki: Path, cert: str | None = None, token: str | None = None) -> Client:
    address = daemon.tls_address
    assert address is not None
    return Client(
        f"https://{address[0]}:{address[1]}",
        ca_file=str(pki / "ca.pem"),
        cert_file=None if cert is None else str(pki / f"{cert}.pem"),
        key_file=None if cert is None else str(pki / f"{cert}.key"),
        token=token,
        timeout=10,
    )


@needs_openssl
def test_tls_admin_certificate(tls_daemon: Daemon, pki: Path) -> None:
    assert remote(tls_daemon, pki, cert="admin").gc().freed_bytes == 3


@needs_openssl
def test_tls_user_certificate(tls_daemon: Daemon, pki: Path) -> None:
    client = remote(tls_daemon, pki, cert="alice")
    assert client.evaluate_source("x").seconds == 0.25
    with pytest.raises(PermissionDenied, match="admin"):
        client.gc()


@needs_openssl
def test_tls_unlisted_certificate(tls_daemon: Daemon, pki: Path) -> None:
    with pytest.raises(PermissionDenied, match="mallory"):
        remote(tls_daemon, pki, cert="mallory").evaluate_source("x")


@needs_openssl
def test_tls_token(tls_daemon: Daemon, pki: Path) -> None:
    seen: list[Event] = []
    client = remote(tls_daemon, pki, token=TOKEN)
    client.on_event = seen.append
    assert client.evaluate_source("x").filename == "<string>"
    assert len(seen) == 1
    with pytest.raises(PermissionDenied):
        client.gc()


@needs_openssl
@pytest.mark.parametrize("token", [None, "wrong"])
def test_tls_without_valid_credential(tls_daemon: Daemon, pki: Path, token: str | None) -> None:
    with pytest.raises(AuthenticationFailed):
        remote(tls_daemon, pki, token=token).evaluate_source("x")


@needs_openssl
def test_tls_rest_needs_credentials(tls_daemon: Daemon, pki: Path) -> None:
    address = tls_daemon.tls_address
    assert address is not None
    context = ssl.create_default_context(cafile=str(pki / "ca.pem"))
    connection = http.client.HTTPSConnection(*address, context=context, timeout=10)
    connection.request("POST", "/v1/gc", b"{}")
    response = connection.getresponse()
    assert response.status == 401
    assert response.getheader("WWW-Authenticate") == 'Bearer realm="lattice"'
    assert json.loads(response.read())["error"]["data"] == {"type": "AuthenticationFailed"}


@needs_openssl
def test_tls_refuses_old_protocols(tls_daemon: Daemon, pki: Path) -> None:
    address = tls_daemon.tls_address
    assert address is not None
    context = ssl.create_default_context(cafile=str(pki / "ca.pem"))
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(*address, context=context, timeout=10)
    with pytest.raises((ssl.SSLError, ConnectionError)):
        connection.request("GET", "/v1/info")
        connection.getresponse()
