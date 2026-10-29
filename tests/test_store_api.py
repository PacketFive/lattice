# SPDX-License-Identifier: GPL-2.0
"""The store through Lattice, the command line and the daemon: RFC 0001 sections 6 and 10 to 16."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap

from lattice import (
    Client,
    Derivation,
    EventHandler,
    Identity,
    Lattice,
    LatticeAPI,
    PermissionDenied,
    Role,
    Settings,
    StoreError,
    cli,
)
from lattice.protocol import RootNameParams, RootParams
from lattice.server import authorize
from lattice.store import database, lock
from lattice.store.realise import realise
from lattice.transport import Daemon

if TYPE_CHECKING:
    from conftest import StoreBackend

TOOLS = Derivation(name="tools", builder="/bin/sh")
HELLO = Derivation(
    name="hello", builder=f"{TOOLS.store_path}/bin/sh", env=FrozenMap({"tools": TOOLS})
)
SOURCE = (
    "tools = derivation(name='tools', builder='/bin/sh')\n"
    "hello = derivation(name='hello', builder=f'{tools}/bin/sh', env={'tools': tools})\n"
)
ALICE = Identity(name="alice", role=Role.USER, transport="tls")
ADMIN = Identity(name="root", role=Role.ADMIN, transport="unix")


@pytest.fixture
def lattice(tmp_path: Path, store_backend: StoreBackend) -> Lattice:
    settings = Settings.model_validate({"store": {"root": str(tmp_path / "root")}})
    return Lattice(settings, backend=store_backend)


def register_tools(lattice: Lattice) -> None:
    connection = database.connect(lattice.layout, create=True)
    try:
        work = Path(lattice.layout.staging) / "tools"
        (work / "bin").mkdir(parents=True)
        (work / "bin" / "sh").write_text("", encoding="utf-8")
        realise(lattice.layout, connection, TOOLS, {"out": str(work)}, [])
    finally:
        connection.close()


def test_read_operations_need_a_store(lattice: Lattice) -> None:
    with pytest.raises(StoreError, match="no store"):
        lattice.path_info(HELLO.store_path)
    with pytest.raises(StoreError, match="not a store path"):
        lattice.path_info("/etc/passwd")
    assert lattice.roots() == ()
    with pytest.raises(StoreError, match="no store"):
        lattice.verify(HELLO.store_path)
    assert [s.action for s in lattice.plan(HELLO).steps] == ["build", "build"]
    assert lattice.sandbox_config(HELLO).mounts
    assert not lattice.layout.exists()


def test_store_operations(lattice: Lattice) -> None:
    register_tools(lattice)
    result = lattice.build(HELLO)
    assert result.built
    assert [s.action for s in lattice.plan(HELLO).steps] == ["present", "present"]
    info = lattice.path_info(HELLO.store_path)
    assert info.references == ()
    assert lattice.verify(HELLO.store_path).ok
    assert lattice.check(HELLO).built
    root = lattice.add_root("per-user/alice/hello", HELLO.store_path)
    assert lattice.roots() == (root,)
    assert lattice.gc().removed == (TOOLS.store_path,)
    assert lattice.remove_root("per-user/alice/hello") == root
    assert lattice.gc().removed == (HELLO.store_path,)


def test_verify_finds_changes(lattice: Lattice) -> None:
    register_tools(lattice)
    physical = Path(lattice.layout.physical(TOOLS.store_path))
    (physical / "bin").chmod(0o755)
    (physical / "bin" / "sh").chmod(0o644)
    (physical / "bin" / "sh").write_text("changed", encoding="utf-8")
    result = lattice.verify(TOOLS.store_path)
    assert not result.ok
    assert result.actual_size == result.expected_size + len("changed")
    (physical / "bin" / "sh").unlink()
    physical.chmod(0o755)
    (physical / "bin").rmdir()
    physical.rmdir()
    with pytest.raises(StoreError, match="files are missing"):
        lattice.verify(TOOLS.store_path)


def test_sandbox_config_uses_the_database(lattice: Lattice) -> None:
    lib = Derivation(name="lib", builder=f"{TOOLS.store_path}/bin/sh", env=FrozenMap({"t": TOOLS}))
    app = Derivation(name="app", builder=f"{lib.store_path}/bin/sh", env=FrozenMap({"lib": lib}))
    register_tools(lattice)

    def inputs() -> set[str]:
        return {m.destination for m in lattice.sandbox_config(app).mounts}

    assert {lib.store_path, TOOLS.store_path} <= inputs()
    lattice.build(lib)
    assert lib.store_path in inputs()
    assert TOOLS.store_path not in inputs()


def test_cli(
    tmp_path: Path,
    lattice: Lattice,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_tools(lattice)
    monkeypatch.setattr(cli, "make_api", lambda args: lattice)
    source = tmp_path / "hello.pyb"
    source.write_text(SOURCE, encoding="utf-8")
    hello = str(source)
    assert cli.main(["plan", hello, "hello"]) == 0
    assert capsys.readouterr().out.split() == [
        "present",
        TOOLS.store_path,
        "build",
        HELLO.store_path,
    ]
    assert cli.main(["build", hello, "hello"]) == 0
    assert capsys.readouterr().out == f"{HELLO.store_path}\n"
    assert cli.main(["build", "--check", hello, "hello"]) == 0
    assert "rebuilt with the same contents" in capsys.readouterr().out
    assert cli.main(["check", hello, "hello"]) == 0
    capsys.readouterr()
    assert cli.main(["build", "--backend", "oci", hello, "hello"]) == 1
    assert "--backend applies only with --check" in capsys.readouterr().err
    chosen: list[str] = []
    monkeypatch.setattr(
        "lattice.store.builder.select_backend",
        lambda name, *args: chosen.append(name) or lattice.backend,
    )
    assert cli.main(["check", "--backend", "microvm", hello, "hello"]) == 0
    assert cli.main(["build", "--check", "--backend", "oci", hello, "hello"]) == 0
    assert chosen == ["microvm", "oci"]
    capsys.readouterr()
    assert cli.main(["path-info", HELLO.store_path]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == HELLO.store_path
    assert cli.main(["verify", HELLO.store_path]) == 0
    assert capsys.readouterr().out == f"{HELLO.store_path}: ok\n"
    assert cli.main(["add-root", "system", HELLO.store_path]) == 0
    assert cli.main(["roots"]) == 0
    assert capsys.readouterr().out == f"system {HELLO.store_path}\n" * 2
    assert cli.main(["gc"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == TOOLS.store_path
    assert lines[1].startswith("removed 1 store paths, freed ")
    assert cli.main(["remove-root", "system"]) == 0
    assert cli.main(["roots"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["build", hello]) == 1
    assert "defines several derivations (tools, hello)" in capsys.readouterr().err
    assert cli.main(["check", hello, "nope"]) == 1
    assert "no derivation named 'nope'" in capsys.readouterr().err


def test_cli_verify_fails(
    lattice: Lattice, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    register_tools(lattice)
    physical = Path(lattice.layout.physical(TOOLS.store_path))
    (physical / "bin").chmod(0o755)
    (physical / "bin" / "sh").chmod(0o644)
    (physical / "bin" / "sh").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "make_api", lambda args: lattice)
    assert cli.main(["verify", TOOLS.store_path]) == 1
    assert f"{TOOLS.store_path}: changed: SHA-256" in capsys.readouterr().out


def test_progress(capsys: pytest.CaptureFixture[str]) -> None:
    from lattice import BuildLog, BuildStarted

    cli.progress(BuildStarted(name="hello", store_path=HELLO.store_path))
    cli.progress(BuildLog(name="hello", text="compiling\n"))
    assert capsys.readouterr().err == f"building hello ({HELLO.store_path})\ncompiling\n"


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("per-user/alice", True),
        ("per-user/alice/hello", True),
        ("per-user/alicex", False),
        ("per-user/bob/x", False),
        ("system", False),
    ],
)
def test_root_permissions(name: str, allowed: bool) -> None:
    for params in (RootParams(name=name, path=HELLO.store_path), RootNameParams(name=name)):
        authorize(ADMIN, "add_root", params)
        if allowed:
            authorize(ALICE, "add_root", params)
        else:
            with pytest.raises(PermissionDenied, match="may not change root"):
                authorize(ALICE, "remove_root", params)


@pytest.fixture
def daemon(
    tmp_path: Path, factory: Callable[[EventHandler | None], LatticeAPI]
) -> Iterator[Daemon]:
    settings = Settings.model_validate({"daemon": {"socket": str(tmp_path / "d.sock")}})
    with Daemon(settings, factory) as daemon:
        yield daemon


def test_client_root_methods(daemon: Daemon) -> None:
    client = Client(f"unix://{daemon.socket_path}")
    assert client.add_root("system", HELLO.store_path).path == HELLO.store_path
    assert client.remove_root("system").name == "system"
    assert [r.name for r in client.roots()] == ["system"]
    for call in (
        lambda: client.plan(HELLO),
        lambda: client.path_info(HELLO.store_path),
        lambda: client.verify(HELLO.store_path),
    ):
        with pytest.raises(NotImplementedError):
            call()


def test_add_root_waits_for_collection(lattice: Lattice) -> None:
    register_tools(lattice)
    added = threading.Event()

    def add() -> None:
        lattice.add_root("system", TOOLS.store_path)
        added.set()

    with lock.exclusive(lattice.layout):
        thread = threading.Thread(target=add)
        thread.start()
        assert not added.wait(0.2)
    thread.join(5)
    assert added.is_set()
    assert [root.name for root in lattice.roots()] == ["system"]
