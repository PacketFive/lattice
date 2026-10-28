# SPDX-License-Identifier: GPL-2.0
"""Archives, keys, metadata, provenance and cache transports: RFC 0001 section 14."""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import lzma
import os
import stat
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice import Derivation, Settings, SettingsError, SubstitutionFailed
from lattice.cache import archive, provenance, signing
from lattice.cache.base import open_cache
from lattice.cache.cvmfs import CvmfsCache, run
from lattice.cache.http import HttpCache
from lattice.cache.metadata import (
    MAX_METADATA,
    ArchiveRef,
    CacheEntry,
    CacheInfo,
    parse_untrusted,
    path_hash,
)
from lattice.store.verify import content_hash

A = Derivation(name="a", builder="/bin/sh")
B = Derivation(name="b", builder="/bin/sh")
SHA = "0" * 64


def entry(**fields: object) -> CacheEntry:
    values: dict[str, object] = {
        "store_path": A.store_path,
        "drv_hash": A.hash,
        "content_sha256": SHA,
        "size": 10,
    }
    values.update(fields)
    return CacheEntry.model_validate(values)


def make_tree(root: Path) -> Path:
    tree = root / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a").write_text("hi", encoding="utf-8")
    (tree / "sub" / "x").write_text("y" * 10, encoding="utf-8")
    (tree / "sub" / "x").chmod(0o755)
    (tree / "link").symlink_to("a")
    return tree


# Keys and signatures


def test_key_round_trip(tmp_path: Path) -> None:
    key = signing.generate_key("cache.example.org-1")
    signature = key.sign(b"data")
    assert signature.startswith("cache.example.org-1:")
    assert signing.verify([key.public()], b"data", [signature]) == key.public()
    assert signing.verify([key.public()], b"other", [signature]) is None
    assert signing.SecretKey.parse(key.text()).public() == key.public()
    file = tmp_path / "key"
    signing.write_secret_key(str(file), key)
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert signing.SecretKey.load(str(file)).public() == key.public()
    with pytest.raises(FileExistsError):
        signing.write_secret_key(str(file), key)


def test_verify_needs_a_trusted_key_of_the_same_name() -> None:
    key, other = signing.generate_key("k"), signing.generate_key("k")
    renamed = signing.SecretKey("j", base64.b64decode(key.text().partition(":")[2]))
    signature = key.sign(b"data")
    assert signing.verify([other.public()], b"data", [signature]) is None
    assert signing.verify([other.public(), key.public()], b"data", [signature]) == key.public()
    assert signing.verify([renamed.public()], b"data", [signature]) is None
    assert signing.verify([key.public()], b"data", ["k:bad", "nocolon", signature])


def test_bad_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a key name"):
        signing.generate_key("bad name")
    with pytest.raises(ValueError, match="secret key"):
        signing.SecretKey.parse("k:" + base64.b64encode(b"short").decode())
    (tmp_path / "bad").write_text("k:!!", encoding="ascii")
    with pytest.raises(SettingsError, match="cannot read a secret key"):
        signing.SecretKey.load(str(tmp_path / "bad"))
    with pytest.raises(SettingsError):
        signing.SecretKey.load(str(tmp_path / "missing"))


# Metadata


def test_entry_validation() -> None:
    assert entry().fingerprint() == f"lattice-entry-1;{A.store_path};{SHA};10;".encode()
    two = entry(references=tuple(sorted([B.store_path, A.store_path.replace("-a", "-c")])))
    assert two.fingerprint().endswith(",".join(two.references).encode())
    with pytest.raises(ValueError, match="does not belong"):
        entry(drv_hash=B.hash)
    with pytest.raises(ValueError, match="sorted"):
        entry(references=(B.store_path, B.store_path))
    with pytest.raises(ValueError, match="itself"):
        entry(references=(A.store_path,))
    with pytest.raises(ValueError, match="not a store path"):
        entry(store_path="/etc/passwd")
    with pytest.raises(ValueError, match="not a store path"):
        entry(references=("/etc",))
    with pytest.raises(ValueError, match="archive url"):
        ArchiveRef(sha256=SHA, size=1, url="https://elsewhere/x")
    assert path_hash(A.store_path) == A.store_path.rpartition("/")[2][:32]


def test_parse_untrusted() -> None:
    data = entry().model_dump_json().encode()
    assert parse_untrusted(CacheEntry, data, "x") == entry()
    with pytest.raises(SubstitutionFailed, match="larger than"):
        parse_untrusted(CacheEntry, b" " * (MAX_METADATA + 1), "x")
    with pytest.raises(SubstitutionFailed, match="x: size"):
        parse_untrusted(CacheEntry, data.replace(b'"size":10', b'"size":"10"'), "x")
    with pytest.raises(SubstitutionFailed, match="x: "):
        parse_untrusted(CacheEntry, b"[]", "x")
    with pytest.raises(SubstitutionFailed, match="priority"):
        parse_untrusted(CacheInfo, b'{"version": 1, "priority": -1}', "x")


# Archives


def test_archive_is_deterministic(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    first = archive.create(str(tree), "n", str(tmp_path / "1"))
    os.utime(tree / "a", (5, 5))
    (tree / "a").chmod(0o600)
    second = archive.create(str(tree), "n", str(tmp_path / "2"))
    assert first == second
    assert first.sha256 == hashlib.sha256((tmp_path / "1").read_bytes()).hexdigest()
    with tarfile.open(tmp_path / "1") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == ["n", "n/a", "n/link", "n/sub", "n/sub/x"]
    assert {(m.mtime, m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, 0, "", "")}
    assert [m.mode for m in members] == [0o755, 0o644, 0o777, 0o755, 0o755]


def test_archive_round_trip(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    archive.create(str(tree), "n", str(tmp_path / "a.tar.xz"))
    out = tmp_path / "out"
    archive.extract(str(tmp_path / "a.tar.xz"), "n", str(out), 1000)
    assert content_hash(str(out)) == content_hash(str(tree))
    size = content_hash(str(tree))[1]
    with pytest.raises(SubstitutionFailed, match="larger than its signed size"):
        archive.extract(str(tmp_path / "a.tar.xz"), "n", str(tmp_path / "small"), size - 1)
    archive.extract(str(tmp_path / "a.tar.xz"), "n", str(tmp_path / "exact"), size)


def test_single_file_archive(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x", encoding="utf-8")
    archive.create(str(tmp_path / "f"), "n", str(tmp_path / "a"))
    size = content_hash(str(tmp_path / "f"))[1]
    with pytest.raises(SubstitutionFailed, match="larger than its signed size"):
        archive.extract(str(tmp_path / "a"), "n", str(tmp_path / "out"), size - 1)
    archive.extract(str(tmp_path / "a"), "n", str(tmp_path / "out"), size)
    assert (tmp_path / "out").read_text(encoding="utf-8") == "x"


def test_archive_refuses_special_files(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(SubstitutionFailed, match="not a file"):
        archive.create(str(tmp_path / "fifo"), "n", str(tmp_path / "a"))


def write_archive(path: Path, members: list[tuple[str, bytes, str]]) -> None:
    """Write an xz tar of ``members``: name, kind (d, f, l, h or c) and data or link target."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if "\0" in name + data:
                info.pax_headers = {"path": name, "linkpath": data}
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data.encode()))
                continue
            info.linkname = data
            tar.addfile(info)
    path.write_bytes(lzma.compress(buffer.getvalue(), format=lzma.FORMAT_XZ))


D, F, L, H, C = tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([("n", D, ""), ("n/../x", F, "")], "not a plain path"),
        ([("n", D, ""), ("n//x", F, "")], "not a plain path"),
        ([("m", D, "")], "is not under n"),
        ([("n/x", F, "")], "must start with its top entry"),
        ([("n", D, ""), ("n", D, "")], "must start with its top entry"),
        ([("n", D, ""), ("n/d/x", F, "")], "comes before its directory"),
        ([("n", D, ""), ("n/l", L, "/etc"), ("n/l/x", F, "")], "comes before its directory"),
        ([("n", D, ""), ("n/x", F, ""), ("n/x", F, "")], "holds an entry twice"),
        ([("n", D, ""), ("n/h", H, "n")], "not a file, directory or link"),
        ([("n", D, ""), ("n/c", C, "")], "not a file, directory or link"),
        ([("n", D, ""), ("n/a\0b", F, "")], "not a plain path"),
        ([("n", D, ""), ("n/l", L, "a\0b")], "not a plain link"),
        ([], "is empty"),
    ],
)
def test_extract_refuses(
    tmp_path: Path, members: list[tuple[str, bytes, str]], message: str
) -> None:
    write_archive(tmp_path / "a", members)
    with pytest.raises(SubstitutionFailed, match=message):
        archive.extract(str(tmp_path / "a"), "n", str(tmp_path / "out"), 100)


def test_extract_charges_every_entry(tmp_path: Path) -> None:
    members = [("n", D, "")] + [(f"n/{index}", F, "") for index in range(100)]
    write_archive(tmp_path / "a", members)
    with pytest.raises(SubstitutionFailed, match="larger than its signed size"):
        archive.extract(str(tmp_path / "a"), "n", str(tmp_path / "out"), 1000)
    assert len(os.listdir(tmp_path / "out")) < 50


def test_extract_refuses_damaged_archives(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"not xz")
    with pytest.raises(SubstitutionFailed, match="damaged"):
        archive.extract(str(tmp_path / "a"), "n", str(tmp_path / "out"), 100)
    write_archive(tmp_path / "b", [("n", D, ""), ("n/x", F, "data")])
    data = lzma.decompress((tmp_path / "b").read_bytes())
    (tmp_path / "b").write_bytes(lzma.compress(data[:1100]))
    with pytest.raises(SubstitutionFailed, match="damaged|truncated"):
        archive.extract(str(tmp_path / "b"), "n", str(tmp_path / "out2"), 100)


# Provenance


def test_provenance_round_trip() -> None:
    key = signing.generate_key("k")
    statement = provenance.statement(
        entry(), ((B.store_path, SHA),), "ci-1", "namespaces", "2026-01-01T00:00:00Z", "later"
    )
    envelope = provenance.sign(statement, key)
    data = envelope.model_dump_json().encode()
    payload = json.loads(base64.b64decode(envelope.payload))
    assert payload["_type"] == provenance.STATEMENT_TYPE
    assert payload["predicateType"] == provenance.PREDICATE_TYPE
    assert payload["predicate"]["buildDefinition"]["externalParameters"] == {"derivation": A.hash}
    assert payload["predicate"]["runDetails"]["builder"] == {"id": "ci-1"}
    opened = provenance.open_envelope(data, "p", (key.public(),), entry())
    assert opened == statement
    with pytest.raises(SubstitutionFailed, match="no signature from a trusted key"):
        provenance.open_envelope(data, "p", (signing.generate_key("k").public(),), entry())
    with pytest.raises(SubstitutionFailed, match="does not describe"):
        provenance.open_envelope(data, "p", (key.public(),), entry(content_sha256="1" * 64))
    broken = envelope.model_copy(update={"payload": "!!"}).model_dump_json().encode()
    with pytest.raises(SubstitutionFailed, match="not base64"):
        provenance.open_envelope(broken, "p", (key.public(),), entry())


def test_provenance_signature_covers_the_payload_type() -> None:
    key = signing.generate_key("k")
    statement = provenance.statement(entry(), (), "ci", "builtin", "s", "f")
    envelope = provenance.sign(statement, key)
    payload = base64.b64decode(envelope.payload)
    assert signing.verify(
        [key.public()],
        provenance.pae(provenance.PAYLOAD_TYPE, payload),
        [f"k:{envelope.signatures[0].sig}"],
    )
    assert not signing.verify(
        [key.public()], provenance.pae("text/plain", payload), [f"k:{envelope.signatures[0].sig}"]
    )


# Directory and HTTP caches


def test_directory_cache(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    cache = HttpCache(f"file://{root}")
    assert cache.read("x.json") is None
    assert not cache.exists("x.json")
    assert cache.info() == CacheInfo()
    source = tmp_path / "src"
    source.write_bytes(b"data")
    cache.write("archive/x", str(source))
    assert cache.exists("archive/x")
    assert stat.S_IMODE((root / "archive" / "x").stat().st_mode) == 0o644
    assert cache.read("archive/x") == b"data"
    assert cache.download("archive/x", str(tmp_path / "d"), 4) == (
        hashlib.sha256(b"data").hexdigest(),
        4,
    )
    with pytest.raises(SubstitutionFailed, match="larger than 3 bytes"):
        cache.read("archive/x", limit=3)
    with pytest.raises(SubstitutionFailed, match="larger than 3 bytes"):
        cache.download("archive/x", str(tmp_path / "e"), 3)
    with pytest.raises(SubstitutionFailed, match="is missing"):
        cache.download("archive/y", str(tmp_path / "f"), 3)
    (root / "dir.json").mkdir()
    with pytest.raises(SubstitutionFailed, match="cannot read"):
        cache.read("dir.json")
    (root / "lattice-cache.json").write_text('{"version": 1, "priority": 10}', encoding="utf-8")
    assert HttpCache(f"file://{root}").info().priority == 10


class Handler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/broken":
            self.send_error(500)
        elif self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
        elif self.path == "/cut":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"10\r\nshort")
            self.close_connection = True
        elif self.path in self.files:
            body = self.files[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        if self.path.startswith("/readonly"):
            self.send_error(403)
            return
        self.files[self.path] = self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def server() -> Iterator[str]:
    Handler.files = {}
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_cache(tmp_path: Path, server: str) -> None:
    cache = HttpCache(server + "/")
    assert cache.read("x.json") is None
    source = tmp_path / "src"
    source.write_bytes(b"data")
    cache.write("archive/x", str(source))
    assert Handler.files["/archive/x"] == b"data"
    assert cache.exists("archive/x")
    assert not cache.exists("archive/y")
    assert cache.read("archive/x") == b"data"
    assert cache.download("archive/x", str(tmp_path / "d"), 10)[1] == 4
    with pytest.raises(SubstitutionFailed, match="cannot read .*broken"):
        cache.read("broken")
    with pytest.raises(SubstitutionFailed, match="cannot read .*away"):
        cache.read("away")
    with pytest.raises(SubstitutionFailed, match="cannot read .*cut"):
        cache.read("cut")
    with pytest.raises(SubstitutionFailed, match="cannot read .*cut"):
        cache.download("cut", str(tmp_path / "cut"), 100)
    source.write_bytes(b"odd")
    cache.write("a?b+c", str(source))
    assert Handler.files["/a%3Fb%2Bc"] == b"odd"
    with pytest.raises(SubstitutionFailed, match="cannot write"):
        cache.write("readonly/x", str(source))
    with pytest.raises(SubstitutionFailed, match="cannot read"):
        HttpCache("http://127.0.0.1:1").read("x")


# CernVM-FS caches


def test_open_cache(tmp_path: Path) -> None:
    settings = Settings.model_validate({"cache": {"cvmfs_root": str(tmp_path)}})
    cvmfs = open_cache("cvmfs://sw.example.org", settings)
    assert isinstance(cvmfs, CvmfsCache)
    assert cvmfs.root == str(tmp_path / "sw.example.org")
    assert cvmfs.shallow_capable
    assert isinstance(open_cache("https://cache.example.org", settings), HttpCache)


def test_cvmfs_cache(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    cache = CvmfsCache("cvmfs://sw", str(tmp_path), "srv", commands.append)
    assert cache.read("x.json") is None
    assert cache.tree(A.store_path) is None
    tree = make_tree(tmp_path)
    cache.publish([(str(tree), A.store_path, {"h.json": b"{}"})])
    assert commands == [["srv", "transaction", "sw"], ["srv", "publish", "sw"]]
    published = cache.tree(A.store_path)
    assert published is not None
    assert published == str(tmp_path / "sw" / "store" / A.store_path.rpartition("/")[2])
    assert content_hash(published) == content_hash(str(tree))
    assert cache.read("h.json") == b"{}"
    assert cache.exists("h.json")
    with pytest.raises(SubstitutionFailed, match="larger than 1 bytes"):
        cache.read("h.json", limit=1)
    with pytest.raises(SubstitutionFailed, match="holds no archives"):
        cache.download("x", str(tmp_path / "x"), 1)
    (tmp_path / "sw" / "dir.json").mkdir()
    with pytest.raises(SubstitutionFailed, match="cannot read"):
        cache.read("dir.json")


def test_cvmfs_publish_aborts(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    cache = CvmfsCache("cvmfs://sw", str(tmp_path), "srv", commands.append)
    with pytest.raises(FileNotFoundError):
        cache.publish([(str(tmp_path / "missing"), A.store_path, {})])
    assert commands == [["srv", "transaction", "sw"], ["srv", "abort", "-f", "sw"]]


def test_run(tmp_path: Path) -> None:
    run(["true"])
    with pytest.raises(SubstitutionFailed, match="failed: no"):
        run(["sh", "-c", "echo no >&2; exit 1"])
    with pytest.raises(SubstitutionFailed, match="cannot run"):
        run([str(tmp_path / "missing")])
