# SPDX-License-Identifier: GPL-2.0
"""Garbage collection and the process scan: RFC 0001 section 12."""

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice import CollectionFinished, Event
from lattice.procscan import ProcessUse, scan
from lattice.store import Registration, StoreLayout, collect, database
from lattice.store.gc import _referrers_first


def path(letter: str) -> str:
    return f"/lattice/store/{letter * 32}-x"


A, B, C, D, E = (path(x) for x in "abcde")


@pytest.fixture
def layout(tmp_path: Path) -> StoreLayout:
    return StoreLayout(root=str(tmp_path / "root"))


@pytest.fixture
def connection(layout: StoreLayout) -> Iterator[sqlite3.Connection]:
    conn = database.connect(layout, create=True)
    yield conn
    conn.close()


def add(layout: StoreLayout, connection: sqlite3.Connection, store_path: str, *refs: str) -> None:
    physical = Path(layout.physical(store_path))
    physical.mkdir()
    (physical / "file").write_text("12345", encoding="utf-8")
    physical.chmod(0o555)
    database.register(
        connection,
        [
            Registration(
                path=store_path,
                drv_hash="f" * 64,
                content_sha256="0" * 64,
                size=1,
                references=frozenset(refs),
            )
        ],
    )


def test_collect(layout: StoreLayout, connection: sqlite3.Connection) -> None:
    add(layout, connection, A)
    add(layout, connection, B, A)
    add(layout, connection, C)
    add(layout, connection, D, C)
    database.add_root(connection, "system", B)
    stray = Path(layout.store) / ("s" * 32 + "-stray")
    stray.mkdir()
    (Path(layout.staging) / "junk").mkdir()
    (Path(layout.staging) / "x.lock").write_text("", encoding="utf-8")
    held = Path(layout.physical(E))
    add(layout, connection, E)
    events: list[Event] = []
    with open(held / "file", encoding="utf-8"):
        result = collect(layout, events.append)
    assert result.removed == (C, D, f"/lattice/store/{'s' * 32}-stray")
    assert result.freed_bytes > 10
    assert sorted(os.listdir(layout.store)) == sorted(
        Path(layout.physical(p)).name for p in (A, B, E)
    )
    assert os.listdir(layout.staging) == []
    assert set(database.all_paths(connection)) == {A, B, E}
    assert events == [CollectionFinished(removed=3, freed_bytes=result.freed_bytes)]
    assert collect(layout).removed == (E,)


def test_missing_files_are_unregistered(
    layout: StoreLayout, connection: sqlite3.Connection
) -> None:
    add(layout, connection, A)
    Path(layout.physical(A), "file").chmod(0o644)
    Path(layout.physical(A)).chmod(0o755)
    Path(layout.physical(A), "file").unlink()
    Path(layout.physical(A)).rmdir()
    assert collect(layout).removed == (A,)
    assert database.all_paths(connection) == {}


def test_referrers_first() -> None:
    edges = {A: frozenset(), B: frozenset({A}), C: frozenset({B, A}), D: frozenset()}
    order = _referrers_first({A, B, C, D}, edges)
    assert order.index(C) < order.index(B) < order.index(A)
    assert sorted(order) == [A, B, C, D]


def test_scan(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    host = tmp_path / "root" / "store"
    process = proc / "42"
    (process / "fd").mkdir(parents=True)
    (process / "exe").symlink_to(f"{A}/bin/x")
    (process / "cwd").symlink_to(f"{host}/{Path(B).name}/share")
    (process / "root").symlink_to("/")
    (process / "fd" / "3").symlink_to(f"{host}/{Path(C).name}")
    (process / "fd" / "4").symlink_to("/lattice/store")
    (process / "maps").write_text(
        f"7f00-7f01 r-xp 00000000 08:01 12 {host}/{Path(D).name}/lib/libc.so\n"
        "7f02-7f03 rw-p 00000000 00:00 0\n",
        encoding="utf-8",
    )
    (proc / "self").mkdir()
    (proc / "7").mkdir()
    assert scan(str(host), str(proc)) == tuple(
        ProcessUse(pid=42, path=p) for p in sorted((A, B, C, D))
    )
    assert scan(str(host), str(tmp_path / "missing")) == ()


def test_scan_finds_this_process(tmp_path: Path) -> None:
    host = tmp_path / "store"
    target = host / ("q" * 32 + "-x")
    target.mkdir(parents=True)
    with open(target / "f", "w", encoding="utf-8"):
        found = scan(str(host))
    assert ProcessUse(pid=os.getpid(), path=f"/lattice/store/{'q' * 32}-x") in found
