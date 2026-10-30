# SPDX-License-Identifier: GPL-2.0
"""Closures and first-boot registration of RFC 0005 section 8.1 and 8.2."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap

from lattice import (
    ActivationNotice,
    BuildFailed,
    Derivation,
    Event,
    Lattice,
    Settings,
    StoreError,
    cli,
    profiles,
)
from lattice.derivation import BUILTIN_CLOSURE
from lattice.stdlib import closure
from lattice.store import database
from lattice.store.realise import realise
from lattice.store.registration import (
    PATHS_FILE,
    REGISTRATION_FILE,
    build_closure,
    registration_path,
)

if TYPE_CHECKING:
    from conftest import FakeKernel, StoreBackend

TOOLS = Derivation(name="tools", builder="/bin/sh")
HELLO = Derivation(
    name="hello",
    builder=f"{TOOLS.store_path}/bin/sh",
    args=(f"{TOOLS.store_path}/bin/sh",),
    env=FrozenMap({"tools": TOOLS}),
)


def make_lattice(
    root: Path, backend: StoreBackend, kernel: FakeKernel, seen: list[Event]
) -> Lattice:
    settings = Settings.model_validate(
        {"store": {"root": str(root), "stacks": str(root.parent / f"{root.name}-stacks")}}
    )
    return Lattice(settings, backend=backend, kernel_modules=kernel, on_event=seen.append)


@pytest.fixture
def lattice(tmp_path: Path, store_backend: StoreBackend, kernel: FakeKernel) -> Lattice:
    built = make_lattice(tmp_path / "root", store_backend, kernel, [])
    connection = database.connect(built.layout, create=True)
    try:
        work = Path(built.layout.staging) / "tools"
        (work / "bin").mkdir(parents=True)
        (work / "bin" / "sh").write_text("", encoding="utf-8")
        realise(built.layout, connection, TOOLS, {"out": str(work)}, [])
    finally:
        connection.close()
    return built


def test_closure_declares_a_builtin() -> None:
    drv = closure(HELLO)
    assert (drv.name, drv.builder, drv.env["of"]) == ("hello-closure", BUILTIN_CLOSURE, HELLO)
    with pytest.raises(TypeError, match="closure needs a derivation"):
        closure("hello")  # type: ignore[arg-type]


def test_closure_lists_paths_and_registration(lattice: Lattice) -> None:
    result = lattice.build(closure(HELLO))
    out = Path(lattice.layout.physical(result.outputs["out"]))
    paths = sorted([HELLO.store_path, TOOLS.store_path])
    assert (out / PATHS_FILE).read_text().splitlines() == paths
    data = (out / REGISTRATION_FILE).read_bytes()
    assert data == lattice.export_registration([HELLO.store_path])
    document = json.loads(data)
    assert document["version"] == 1
    assert [entry["path"] for entry in document["paths"]] == paths
    hello = next(e for e in document["paths"] if e["path"] == HELLO.store_path)
    info = lattice.path_info(HELLO.store_path)
    assert hello["references"] == [TOOLS.store_path]
    assert (hello["content_sha256"], hello["size"]) == (info.content_sha256, info.size)
    assert set(lattice.path_info(result.store_path).references) == set(paths)


def test_closure_refuses_a_bad_derivation(lattice: Lattice, tmp_path: Path) -> None:
    bad = Derivation(name="x", builder=BUILTIN_CLOSURE, env=FrozenMap({"of": "y"}))
    with pytest.raises(BuildFailed, match="exactly one derivation"):
        build_closure(lattice.layout, bad, str(tmp_path / "out"))
    with pytest.raises(StoreError, match="is not registered"):
        lattice.export_registration([HELLO.store_path])


def image_store(source: Lattice, target: Path, data: bytes) -> None:
    """Lay out ``target`` as a disk image does: paths, registration.json and generation 1."""
    store = target / "store"
    store.mkdir(parents=True)
    for entry in json.loads(data)["paths"]:
        physical = source.layout.physical(entry["path"])
        shutil.copytree(physical, store / os.path.basename(physical), symlinks=True)
    (target / "var").mkdir()
    (target / "var" / REGISTRATION_FILE).write_bytes(data)


def test_first_boot_imports_the_registration(
    lattice: Lattice, tmp_path: Path, store_backend: StoreBackend, kernel: FakeKernel
) -> None:
    lattice.build(HELLO)
    data = lattice.export_registration([HELLO.store_path])
    seen: list[Event] = []
    booted = make_lattice(tmp_path / "image", store_backend, kernel, seen)
    image_store(lattice, tmp_path / "image", data)
    profiles.prepare(booted.layout, profiles.SYSTEM)
    os.symlink(HELLO.store_path, profiles.link_path(booted.layout, profiles.SYSTEM, 1))
    os.symlink("system-1-link", profiles.link_path(booted.layout, profiles.SYSTEM))
    booted.activate_boot()
    assert not os.path.exists(registration_path(booted.layout))
    assert booted.path_info(HELLO.store_path).references == (TOOLS.store_path,)
    assert [(r.name, r.path) for r in booted.roots()] == [("profile/system/1", HELLO.store_path)]
    assert ActivationNotice(text="registered 2 store paths") in seen
    assert booted.gc().removed == ()
    (tmp_path / "image" / "var" / REGISTRATION_FILE).write_bytes(data)
    booted.activate_boot()
    assert os.path.exists(registration_path(booted.layout))
    assert any("is ignored: the store database is not empty" in str(e) for e in seen)


def test_first_boot_refuses_changed_paths(
    lattice: Lattice, tmp_path: Path, store_backend: StoreBackend, kernel: FakeKernel
) -> None:
    lattice.build(HELLO)
    data = lattice.export_registration([HELLO.store_path])
    booted = make_lattice(tmp_path / "image", store_backend, kernel, [])
    image_store(lattice, tmp_path / "image", data)
    changed = Path(booted.layout.physical(HELLO.store_path)) / "file"
    changed.chmod(0o644)
    original = changed.read_text()
    changed.write_text("changed")
    with pytest.raises(StoreError, match="does not match its content hash"):
        booted.activate_boot()
    assert os.path.exists(registration_path(booted.layout))
    assert database.all_paths(database.connect(booted.layout)) == {}
    changed.write_text(original)
    changed.chmod(0o444)
    tools = Path(booted.layout.physical(TOOLS.store_path))
    for directory in (tools, tools / "bin"):
        directory.chmod(0o755)
    shutil.rmtree(tools)
    with pytest.raises(StoreError, match="is listed in registration.json but missing"):
        booted.activate_boot()
    (tmp_path / "image" / "var" / REGISTRATION_FILE).write_text("{}")
    with pytest.raises(StoreError, match="is not valid"):
        booted.activate_boot()


def test_cli_export_registration(
    lattice: Lattice, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    lattice.build(HELLO)
    monkeypatch.setattr(cli, "Lattice", lambda settings, on_event: lattice)
    assert cli.main(["export-registration", HELLO.store_path]) == 0
    assert capsysbinary.readouterr().out == lattice.export_registration([HELLO.store_path])
    with pytest.raises(SystemExit):
        cli.main(["--daemon", "export-registration", HELLO.store_path])
