# SPDX-License-Identifier: GPL-2.0
"""Publishing outputs in the store: RFC 0001 sections 10.2 and 10.3."""

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation, StoreError
from lattice.store import StoreLayout, content_hash, database
from lattice.store.realise import EPOCH_NS, realise, remove_tree

LIB = Derivation(name="lib", builder="/bin/sh")


@pytest.fixture
def layout(tmp_path: Path) -> StoreLayout:
    return StoreLayout(root=str(tmp_path / "root"))


@pytest.fixture
def connection(layout: StoreLayout) -> Iterator[sqlite3.Connection]:
    conn = database.connect(layout, create=True)
    yield conn
    conn.close()


def outputs(work: Path, drv: Derivation, contents: dict[str, str]) -> dict[str, str]:
    built = {}
    for name, text in contents.items():
        out = work / name
        (out / "sub").mkdir(parents=True)
        (out / "sub" / "file").write_text(text, encoding="utf-8")
        (out / "link").symlink_to("sub/file")
        built[name] = str(out)
    return built


def test_realise(layout: StoreLayout, connection: sqlite3.Connection, tmp_path: Path) -> None:
    lib_dir = Path(layout.physical(LIB.store_path))
    lib_dir.mkdir()
    database.register(
        connection,
        [
            database.Registration(
                path=LIB.store_path, drv_hash=LIB.hash, content_sha256="0" * 64, size=0
            )
        ],
    )
    drv = Derivation(
        name="app", builder="/bin/sh", env=FrozenMap({"lib": LIB}), outputs=("out", "dev")
    )
    out, dev = drv.output_paths["out"], drv.output_paths["dev"]
    work = Path(layout.staging) / "w"
    built = outputs(work, drv, {"out": f"uses {LIB.store_path}/lib\n", "dev": f"see {out}\n"})
    stale = Path(layout.physical(out))
    stale.mkdir()
    (stale / "old").write_text("x", encoding="utf-8")
    stale.chmod(0o555)
    entries = {e.path: e for e in realise(layout, connection, drv, built, [LIB.store_path])}
    assert entries[out].references == {LIB.store_path}
    assert entries[dev].references == {out}
    for path in (out, dev):
        physical = Path(layout.physical(path))
        assert not (physical / "old").exists()
        assert (entries[path].content_sha256, entries[path].size) == content_hash(str(physical))
        for entry in (physical, physical / "sub", physical / "sub" / "file", physical / "link"):
            info = entry.lstat()
            assert info.st_mtime_ns == EPOCH_NS
            if not stat.S_ISLNK(info.st_mode):
                assert not info.st_mode & 0o222
    info = database.path_info(connection, dev)
    assert info is not None
    assert info.references == (out,)
    assert not work.joinpath("out").exists()
    with pytest.raises(StoreError, match="already registered"):
        realise(layout, connection, drv, outputs(work / "again", drv, {"out": "", "dev": ""}), [])


def test_missing_output(layout: StoreLayout, connection: sqlite3.Connection) -> None:
    drv = Derivation(name="app", builder="/bin/sh", outputs=("out", "dev"))
    built = outputs(Path(layout.staging), drv, {"out": "x"})
    with pytest.raises(BuildFailed, match="did not create output dev"):
        realise(layout, connection, drv, built, [])
    assert database.all_paths(connection) == {}


def test_cycle_between_outputs(layout: StoreLayout, connection: sqlite3.Connection) -> None:
    drv = Derivation(name="app", builder="/bin/sh", outputs=("out", "dev"))
    paths = drv.output_paths
    built = outputs(
        Path(layout.staging), drv, {"out": f"{paths['dev']}/x", "dev": f"{paths['out']}/y"}
    )
    with pytest.raises(BuildFailed, match="app: outputs refer to each other in a cycle"):
        realise(layout, connection, drv, built, [])
    assert os.listdir(layout.store) == []


def test_single_file_output(layout: StoreLayout, connection: sqlite3.Connection) -> None:
    drv = Derivation(name="file", builder="/bin/sh")
    source = Path(layout.staging) / "out"
    source.write_text("text", encoding="utf-8")
    (entry,) = realise(layout, connection, drv, {"out": str(source)}, [])
    physical = Path(layout.physical(drv.store_path))
    assert physical.read_text(encoding="utf-8") == "text"
    assert stat.S_IMODE(physical.stat().st_mode) == 0o444
    assert entry.references == frozenset()


def test_remove_tree(tmp_path: Path) -> None:
    top = tmp_path / "t"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "f").write_text("x", encoding="utf-8")
    for entry in (top / "a" / "b", top / "a", top):
        entry.chmod(0o555)
    remove_tree(str(top))
    assert not top.exists()
    remove_tree(str(top))
    (tmp_path / "f").write_text("x", encoding="utf-8")
    remove_tree(str(tmp_path / "f"))
    assert not (tmp_path / "f").exists()
