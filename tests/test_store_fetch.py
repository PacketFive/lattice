# SPDX-License-Identifier: GPL-2.0
"""The download behind fetch_url: RFC 0001 sections 7.6 and 16."""

import hashlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation
from lattice.derivation import BUILTIN_FETCH_URL
from lattice.store.fetch import fetch

BODY = b"hello, world\n" * 1000
DIGEST = hashlib.sha256(BODY).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/to-ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/passwd")
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def drv(url: str, sha256: str = DIGEST) -> Derivation:
    return Derivation(
        name="src", builder=BUILTIN_FETCH_URL, env=FrozenMap({"url": url, "sha256": sha256})
    )


@pytest.mark.parametrize("path", ["/file", "/redirect"])
def test_fetch(server: str, tmp_path: Path, path: str) -> None:
    result = fetch(drv(server + path), str(tmp_path / "out"))
    assert (tmp_path / "out").read_bytes() == BODY
    assert (result.sha256, result.size) == (DIGEST, len(BODY))


@pytest.mark.parametrize(
    ("path", "sha256", "message"),
    [
        ("/file", "0" * 64, f"has SHA-256 {DIGEST}, expected 0000"),
        ("/missing", DIGEST, "cannot fetch .*Not Found"),
        ("/to-ftp", DIGEST, "cannot fetch .*refused scheme"),
    ],
)
def test_fetch_fails(server: str, tmp_path: Path, path: str, sha256: str, message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        fetch(drv(server + path, sha256), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_refused_url(tmp_path: Path) -> None:
    with pytest.raises(BuildFailed, match="refuses 'file:///etc/passwd'"):
        fetch(drv("file:///etc/passwd"), str(tmp_path / "out"))
    missing = Derivation(name="src", builder=BUILTIN_FETCH_URL)
    with pytest.raises(BuildFailed, match="needs url and sha256"):
        fetch(missing, str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()
