# SPDX-License-Identifier: GPL-2.0
"""Building into the store with a stand-in sandbox: RFC 0001 sections 10 and 16."""

from __future__ import annotations

import hashlib
import http.server
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap

from lattice import (
    BuildFailed,
    BuildFinished,
    BuildLog,
    BuildStarted,
    Derivation,
    Event,
    Settings,
    StoreError,
)
from lattice.derivation import BUILTIN_FETCH_URL
from lattice.store import Builder, StoreLayout, database
from lattice.store.builder import ordered
from lattice.store.realise import realise

if TYPE_CHECKING:
    from conftest import StoreBackend
    from lattice.sandbox import SandboxJob

TOOLS = Derivation(name="tools", builder="/bin/sh")
SH = f"{TOOLS.store_path}/bin/sh"


def drv(name: str, *args: str, **inputs: Derivation) -> Derivation:
    return Derivation(name=name, builder=SH, args=args, env=FrozenMap({"tools": TOOLS, **inputs}))


def bound(job: SandboxJob) -> dict[str, str | None]:
    """Return the store paths a build sees, with the host path each is bound from."""
    return {m.destination: m.source for m in job.spec.mounts if m.kind == "bind" and m.read_only}


def register_tools(layout: StoreLayout) -> None:
    connection = database.connect(layout, create=True)
    try:
        work = Path(layout.staging) / "tools"
        (work / "bin").mkdir(parents=True)
        (work / "bin" / "sh").write_text("", encoding="utf-8")
        realise(layout, connection, TOOLS, {"out": str(work)}, [])
    finally:
        connection.close()


@pytest.fixture
def layout(tmp_path: Path) -> StoreLayout:
    layout = StoreLayout(root=str(tmp_path / "root"))
    register_tools(layout)
    return layout


def builder(
    layout: StoreLayout, backend: StoreBackend, events: list[Event] | None = None, jobs: int = 1
) -> Builder:
    settings = Settings.model_validate({"store": {"root": layout.root, "max_jobs": jobs}})
    return Builder(
        layout, settings, backend=backend, on_event=None if events is None else events.append
    )


def test_order() -> None:
    a = drv("a")
    b = drv("b", a=a)
    c = drv("c", a=a, b=b)
    assert [d.name for d in ordered(c)] == ["tools", "a", "b", "c"]


def test_plan(tmp_path: Path, layout: StoreLayout, store_backend: StoreBackend) -> None:
    a = drv("a")
    fetched = Derivation(
        name="src", builder=BUILTIN_FETCH_URL, env=FrozenMap({"url": "x", "sha256": "y"})
    )
    top = drv("top", a=a, src=fetched)
    steps = builder(layout, store_backend).plan(top).steps
    assert [(s.name, s.action) for s in steps] == [
        ("tools", "present"),
        ("a", "build"),
        ("src", "fetch"),
        ("top", "build"),
    ]
    assert steps[-1].outputs == top.output_paths
    empty = StoreLayout(root=str(tmp_path / "none"))
    assert builder(empty, store_backend).plan(TOOLS).steps[0].action == "build"
    assert not empty.exists()


def test_build(layout: StoreLayout, store_backend: StoreBackend) -> None:
    a = drv("a", f"{TOOLS.store_path}/bin/sh")
    b = drv("b", f"{a.store_path}/file", a=a)
    events: list[Event] = []
    result = builder(layout, store_backend, events).build(b)
    assert result.built
    assert result.outputs == b.output_paths
    connection = database.connect(layout)
    try:
        info_a = database.path_info(connection, a.store_path)
        info_b = database.path_info(connection, b.store_path)
    finally:
        connection.close()
    assert info_a is not None
    assert info_b is not None
    assert info_a.references == (TOOLS.store_path,)
    assert info_b.references == (a.store_path,)
    assert Path(layout.physical(b.store_path), "file").read_text(encoding="utf-8") == (
        f"{a.store_path}/file"
    )
    first, second = store_backend.jobs
    assert bound(first) == {TOOLS.store_path: layout.physical(TOOLS.store_path)}
    assert set(bound(second)) == {TOOLS.store_path, a.store_path}
    started = [e.name for e in events if isinstance(e, BuildStarted)]
    finished = [e for e in events if isinstance(e, BuildFinished)]
    assert started == ["a", "b"]
    assert [e.name for e in finished] == ["a", "b"]
    assert any(isinstance(e, BuildLog) and "building b" in e.text for e in events)
    assert sorted(os.listdir(layout.staging)) == sorted(f"{d.hash}.lock" for d in (TOOLS, a, b))

    again = builder(layout, store_backend).build(b)
    assert not again.built
    assert len(store_backend.jobs) == 2


def test_parallel(layout: StoreLayout, store_backend: StoreBackend) -> None:
    parts = {f"p{i}": drv(f"p{i}") for i in range(4)}
    top = drv("top", **parts)
    assert builder(layout, store_backend, jobs=3).build(top).built
    assert len(store_backend.jobs) == 5
    assert store_backend.jobs[-1].spec.name == "top"


def test_failure_stops_the_build(layout: StoreLayout, store_backend: StoreBackend) -> None:
    good = drv("good")
    bad = drv("bad", "fail")
    top = drv("top", good=good, bad=bad)
    with pytest.raises(BuildFailed, match="bad: the program exited with status 2"):
        builder(layout, store_backend, jobs=2).build(top)
    assert "top" not in [job.spec.name for job in store_backend.jobs]
    connection = database.connect(layout)
    try:
        assert not database.registered(connection, [bad.store_path, top.store_path])
    finally:
        connection.close()
    assert all(name.endswith(".lock") for name in os.listdir(layout.staging))


def test_check(layout: StoreLayout, new_backend: Callable[..., StoreBackend]) -> None:
    a = drv("a")
    with pytest.raises(StoreError, match="a: output out is not built"):
        builder(layout, new_backend()).check(a)
    builder(layout, new_backend()).build(a)
    assert builder(layout, new_backend()).check(a).built
    with pytest.raises(BuildFailed, match="a: the rebuilt outputs differ:\nout: file"):
        builder(layout, new_backend("changed")).check(a)
    connection = database.connect(layout)
    try:
        info = database.path_info(connection, a.store_path)
    finally:
        connection.close()
    assert info is not None
    assert Path(layout.physical(a.store_path), "file").read_text(encoding="utf-8") == ""


def test_check_on_another_backend(
    layout: StoreLayout, new_backend: Callable[..., StoreBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = drv("a", f"{TOOLS.store_path}/bin/sh")
    b = drv("b", a=a)
    configured = new_backend()
    builder(layout, configured).build(b)
    chosen: list[tuple[str, str]] = []
    other = new_backend("changed")

    def select(name: str, sandbox: object, host_store: str) -> StoreBackend:
        chosen.append((name, host_store))
        return other

    monkeypatch.setattr("lattice.store.builder.select_backend", select)
    with pytest.raises(BuildFailed, match="b: the rebuilt outputs differ"):
        builder(layout, configured).check(b, "oci")
    assert chosen == [("oci", layout.store)]
    assert [job.spec.name for job in other.jobs] == ["b"]


def test_microvm_records_guest_files(
    tmp_path: Path, layout: StoreLayout, new_backend: Callable[..., StoreBackend]
) -> None:
    backend = new_backend()
    backend.name = "microvm"
    init = tmp_path / "init"
    init.write_bytes(b"guest init")
    settings = Settings.model_validate(
        {
            "store": {"root": layout.root},
            "sandbox": {"microvm": {"kernel": SH, "init": str(init)}},
        }
    )
    a = drv("a")
    events: list[Event] = []
    Builder(layout, settings, backend=backend, on_event=events.append).build(a)
    connection = database.connect(layout)
    try:
        record = database.build_record(connection, a.store_path)
        tools = database.path_info(connection, TOOLS.store_path)
    finally:
        connection.close()
    assert record is not None
    assert tools is not None
    assert record.backend == "microvm"
    assert (TOOLS.store_path, tools.content_sha256) in record.inputs
    assert (str(init), hashlib.sha256(b"guest init").hexdigest()) in record.inputs
    finished = [e for e in events if isinstance(e, BuildFinished)]
    assert finished[-1].boot_seconds is None


BODY = b"source code\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def url() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/src.tar"
    httpd.shutdown()
    httpd.server_close()


def test_fetch(layout: StoreLayout, store_backend: StoreBackend, url: str) -> None:
    env = {"url": url, "sha256": hashlib.sha256(BODY).hexdigest()}
    src = Derivation(name="src", builder=BUILTIN_FETCH_URL, env=FrozenMap(env))
    top = drv("top", src=src)
    builder(layout, store_backend).build(top)
    assert Path(layout.physical(src.store_path)).read_bytes() == BODY
    assert set(bound(store_backend.jobs[0])) == {TOOLS.store_path, src.store_path}
    two = Derivation(
        name="src", builder=BUILTIN_FETCH_URL, env=FrozenMap(env), outputs=("out", "doc")
    )
    with pytest.raises(BuildFailed, match="single output"):
        builder(layout, store_backend).build(two)
