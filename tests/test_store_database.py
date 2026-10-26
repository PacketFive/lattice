# SPDX-License-Identifier: GPL-2.0
"""The store database: RFC 0001 sections 11 and 12."""

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice import StoreError
from lattice.store import Registration, StoreLayout, check_root_name, database


def path(letter: str, name: str = "x") -> str:
    return f"/lattice/store/{letter * 32}-{name}"


A, B, C, D = path("a"), path("b"), path("c"), path("d")


def entry(store_path: str, *references: str) -> Registration:
    return Registration(
        path=store_path,
        drv_hash="f" * 64,
        content_sha256="0" * 64,
        size=1,
        references=frozenset(references),
    )


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = database.connect(StoreLayout(root=str(tmp_path)), create=True)
    yield conn
    conn.close()


def test_missing_store(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no store"):
        database.connect(StoreLayout(root=str(tmp_path)))


@pytest.mark.parametrize("trial", range(10))
def test_concurrent_creation(tmp_path: Path, trial: int) -> None:
    layout = StoreLayout(root=str(tmp_path / str(trial)))
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def open_store() -> None:
        barrier.wait()
        try:
            database.connect(layout, create=True).close()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_reopen_keeps_schema(tmp_path: Path) -> None:
    layout = StoreLayout(root=str(tmp_path))
    database.connect(layout, create=True).close()
    conn = database.connect(layout)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(database.MIGRATIONS)
    conn.execute(f"PRAGMA user_version = {len(database.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(StoreError, match="newer than this Lattice"):
        database.connect(layout)


def test_register_and_info(connection: sqlite3.Connection) -> None:
    database.register(connection, [entry(A)])
    database.register(connection, [entry(B, A), entry(C, B)])
    info = database.path_info(connection, C)
    assert info is not None
    assert info.references == (B,)
    assert (info.location, info.source, info.size) == ("local", "", 1)
    assert database.path_info(connection, D) is None
    assert database.registered(connection, [A, D]) == {A}
    assert database.referrers(connection, A) == {B}
    assert database.all_paths(connection) == {A: 1, B: 1, C: 1}


def test_register_is_atomic(connection: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="unregistered"):
        database.register(connection, [entry(A), entry(B, D)])
    assert database.all_paths(connection) == {}
    database.register(connection, [entry(A)])
    with pytest.raises(StoreError, match="already registered"):
        database.register(connection, [entry(A)])


def test_self_reference_is_dropped(connection: sqlite3.Connection) -> None:
    database.register(connection, [entry(A, A)])
    info = database.path_info(connection, A)
    assert info is not None
    assert info.references == ()


def test_closure(connection: sqlite3.Connection) -> None:
    database.register(connection, [entry(A), entry(B, A), entry(C, B), entry(D)])
    assert database.closure(connection, [C]) == {A, B, C}
    assert database.closure(connection, [B, D, path("e")]) == {A, B, D}


def test_unregister(connection: sqlite3.Connection) -> None:
    database.register(connection, [entry(A), entry(B, A)])
    with pytest.raises(StoreError, match="still referred to"):
        database.unregister(connection, A)
    database.unregister(connection, B)
    database.unregister(connection, A)
    assert database.all_paths(connection) == {}


def test_roots_and_live_paths(connection: sqlite3.Connection) -> None:
    database.register(connection, [entry(A), entry(B, A), entry(C), entry(D)])
    assert database.live_paths(connection, []) == frozenset()
    root = database.add_root(connection, "per-user/alice/x", B)
    assert (root.name, root.path) == ("per-user/alice/x", B)
    assert database.live_paths(connection, [D]) == {A, B, D}
    database.add_root(connection, "per-user/alice/x", C)
    assert [r.path for r in database.roots(connection)] == [C]
    with pytest.raises(StoreError, match="still referred to"):
        database.unregister(connection, C)
    assert database.remove_root(connection, "per-user/alice/x").path == C
    with pytest.raises(StoreError, match="no root"):
        database.remove_root(connection, "per-user/alice/x")
    with pytest.raises(StoreError, match="not registered"):
        database.add_root(connection, "system", path("e"))


@pytest.mark.parametrize("name", ["system", "per-user/alice", "a/b.c/d_e+f-1"])
def test_root_names(name: str) -> None:
    assert check_root_name(name) == name


@pytest.mark.parametrize("name", ["", "a//b", "/a", "a/", "a/../b", ".", "a b", "a/./b"])
def test_bad_root_names(name: str) -> None:
    with pytest.raises(StoreError, match="not a valid root name"):
        check_root_name(name)
