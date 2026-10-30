# SPDX-License-Identifier: GPL-2.0
"""The builder behind unpack: RFC 0002 section 5.3."""

import io
import os
import stat
import tarfile
from pathlib import Path
from typing import Literal

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation
from lattice.derivation import BUILTIN_UNPACK
from lattice.stdlib import fetch_url, unpack, unpacked_name
from lattice.store import StoreLayout
from lattice.store.unpack import _check_links
from lattice.store.unpack import unpack as run_unpack

Entry = tuple[str, str, object]
WriteMode = Literal["w", "w:gz", "w:xz"]


def make_archive(path: str, entries: list[Entry], mode: WriteMode = "w:xz") -> None:
    with tarfile.open(path, mode) as tar:
        for name, kind, value in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = str(value).encode()
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
                continue
            if kind == "mode":
                info.size = 1
                info.mode = int(str(value), 8)
                tar.addfile(info, io.BytesIO(b"x"))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE,
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
                "char": tarfile.CHRTYPE,
            }[kind]
            info.mode = 0o755
            if kind in ("symlink", "hardlink"):
                info.linkname = str(value)
            tar.addfile(info)


def setup(tmp_path: Path, entries: list[Entry], name: str = "seed.tar.xz") -> tuple:
    layout = StoreLayout(root=str(tmp_path / "root"))
    archive = fetch_url(f"https://example.org/{name}", "0" * 64)
    os.makedirs(layout.store)
    mode: WriteMode = "w:gz" if name.endswith(".gz") else "w:xz" if name.endswith(".xz") else "w"
    make_archive(layout.physical(archive.store_path), entries, mode)
    return layout, unpack(archive), str(tmp_path / "out")


def test_name() -> None:
    assert unpacked_name("hello-2.12.1.tar.gz") == "hello-2.12.1"
    assert unpacked_name("seed.tar.xz") == "seed"
    assert unpacked_name("plain") == "plain"
    assert unpacked_name(".tar") == ".tar"
    drv = unpack(fetch_url("https://example.org/a/make-4.4.1.tar.gz", "1" * 64))
    assert (drv.name, drv.builder, tuple(drv.env)) == ("make-4.4.1", BUILTIN_UNPACK, ("archive",))
    with pytest.raises(TypeError):
        unpack("make.tar.gz")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["seed.tar.xz", "seed.tar.gz", "seed.tar"])
def test_unpack(tmp_path: Path, name: str) -> None:
    entries: list[Entry] = [
        ("bin", "dir", None),
        ("bin/sh", "file", "#!shell"),
        ("bin/ash", "symlink", "sh"),
        ("bin/hush", "hardlink", "bin/sh"),
        ("lib/x", "symlink", "../bin/sh"),
    ]
    layout, drv, out = setup(tmp_path, entries, name)
    assert run_unpack(layout, drv, out) == {"out": out}
    assert Path(out, "bin/sh").read_text() == "#!shell"
    assert os.readlink(Path(out, "bin/ash")) == "sh"
    assert Path(out, "bin/hush").read_text() == "#!shell"
    assert Path(out, "lib/x").read_text() == "#!shell"


def test_modes_cleared(tmp_path: Path) -> None:
    layout, drv, out = setup(tmp_path, [("suid", "mode", "6777"), ("ro", "mode", "0555")])
    run_unpack(layout, drv, out)
    assert stat.S_IMODE(os.stat(Path(out, "suid")).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(Path(out, "ro")).st_mode) == 0o755


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("/etc/passwd", "file", "x")], "'/etc/passwd' leaves the output"),
        ([("a/../../x", "file", "x")], "leaves the output"),
        ([("l", "symlink", "/etc/passwd")], "'l' points outside"),
        ([("d/l", "symlink", "../../x")], "'d/l' points outside"),
        ([("h", "hardlink", "../x")], "'h' points outside"),
        ([("p", "fifo", None)], "'p' is not a file, directory or link"),
        ([("c", "char", None)], "'c' is not a file, directory or link"),
        ([("l", "symlink", "."), ("l/x", "file", "x")], "'l/x' passes through a link"),
        (
            [("a", "dir", None), ("a/b", "symlink", ".."), ("a/b/c", "symlink", "..")],
            "'a/b/c' passes through a link",
        ),
    ],
)
def test_refused(tmp_path: Path, entries: list[Entry], message: str) -> None:
    layout, drv, out = setup(tmp_path, entries)
    with pytest.raises(BuildFailed, match=message):
        run_unpack(layout, drv, out)
    assert not Path(tmp_path, "x").exists()


def test_not_an_archive(tmp_path: Path) -> None:
    layout = StoreLayout(root=str(tmp_path / "root"))
    archive = fetch_url("https://example.org/seed.tar.xz", "0" * 64)
    os.makedirs(layout.store)
    Path(layout.physical(archive.store_path)).write_bytes(b"not a tar")
    with pytest.raises(BuildFailed, match="cannot read seed.tar.xz"):
        run_unpack(layout, unpack(archive), str(tmp_path / "out"))


def test_bad_env(tmp_path: Path) -> None:
    layout = StoreLayout(root=str(tmp_path / "root"))
    drv = Derivation(name="x", builder=BUILTIN_UNPACK, env=FrozenMap({"archive": "x"}))
    with pytest.raises(BuildFailed, match="exactly one derivation"):
        run_unpack(layout, drv, str(tmp_path / "out"))


def test_final_link_check(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "a").mkdir(parents=True)
    (out / "a" / "b").symlink_to("..")
    _check_links(Derivation(name="x", builder=BUILTIN_UNPACK), str(out))
    (out / "a" / "c").symlink_to("b/..")
    with pytest.raises(BuildFailed, match="'a/c' points outside"):
        _check_links(Derivation(name="x", builder=BUILTIN_UNPACK), str(out))


def test_build_through_store(tmp_path: Path) -> None:
    from lattice import Lattice, Settings
    from lattice.store import database
    from lattice.store.realise import realise

    lattice = Lattice(Settings.model_validate({"store": {"root": str(tmp_path / "root")}}))
    archive = fetch_url("https://example.org/tools-1.tar.xz", "0" * 64)
    source = tmp_path / "tools-1.tar.xz"
    make_archive(str(source), [("tools-1/bin/sh", "file", "#!shell")])
    connection = database.connect(lattice.layout, create=True)
    try:
        realise(lattice.layout, connection, archive, {"out": str(source)}, [])
    finally:
        connection.close()
    drv = unpack(archive)
    assert lattice.build(drv).built
    physical = Path(lattice.layout.physical(drv.store_path))
    assert (physical / "tools-1/bin/sh").read_text() == "#!shell"
    assert not os.access(physical / "tools-1/bin/sh", os.W_OK)
    assert lattice.verify(drv.store_path).ok
    assert lattice.path_info(drv.store_path).references == ()
