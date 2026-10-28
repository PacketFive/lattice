# SPDX-License-Identifier: GPL-2.0
"""Pushing to and substituting from binary caches through Lattice: RFC 0001 section 14."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap

from lattice import (
    CacheSkipped,
    Derivation,
    Event,
    Lattice,
    PathPushed,
    Settings,
    SettingsError,
    StoreError,
    SubstitutionFailed,
    SubstitutionFinished,
    SubstitutionStarted,
    cli,
)
from lattice.cache import signing
from lattice.cache.metadata import entry_name
from lattice.store import database
from lattice.store.builder import shallow_paths
from lattice.store.realise import realise

if TYPE_CHECKING:
    from conftest import StoreBackend

TOOLS = Derivation(name="tools", builder="/bin/sh")
SH = f"{TOOLS.store_path}/bin/sh"
BASE = Derivation(name="base", builder=SH, args=("base",), env=FrozenMap({"tools": TOOLS}))
APP = Derivation(
    name="app", builder=SH, args=(BASE.store_path,), env=FrozenMap({"tools": TOOLS, "base": BASE})
)


def register_tools(lattice: Lattice) -> None:
    connection = database.connect(lattice.layout, create=True)
    try:
        work = Path(lattice.layout.staging) / "tools"
        (work / "bin").mkdir(parents=True)
        (work / "bin" / "sh").write_text("", encoding="utf-8")
        realise(lattice.layout, connection, TOOLS, {"out": str(work)}, [])
    finally:
        connection.close()


class Machine:
    """A store at ``root`` with its own settings, events and fake backend."""

    def __init__(self, root: Path, backend: StoreBackend, **settings: object) -> None:
        self.events: list[Event] = []
        self.settings = Settings.model_validate_json(
            json.dumps({"store": {"root": str(root)}, **settings})
        )
        self.lattice = Lattice(self.settings, backend=backend, on_event=self.events.append)
        register_tools(self.lattice)

    def actions(self, drv: Derivation) -> list[str]:
        return [s.action for s in self.lattice.plan(drv).steps]


@pytest.fixture
def key(tmp_path: Path) -> tuple[signing.SecretKey, str]:
    key = signing.generate_key("cache-1")
    file = tmp_path / "secret"
    signing.write_secret_key(str(file), key)
    return key, str(file)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "cache"
    directory.mkdir()
    return directory


def builder_machine(
    tmp_path: Path, backend: StoreBackend, key: tuple[signing.SecretKey, str], **cache: object
) -> Machine:
    return Machine(
        tmp_path / "builder",
        backend,
        cache={"secret_key_file": key[1], "builder_id": "ci", **cache},
    )


def test_push_and_substitute(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(APP)
    url = f"file://{cache}"
    result = ci.lattice.push([APP.store_path], url)
    assert result.pushed == (BASE.store_path, APP.store_path)
    assert result.skipped == ()
    assert [e.path for e in ci.events if isinstance(e, PathPushed)] == list(result.pushed)
    assert (cache / "lattice-cache.json").exists()
    metadata = json.loads((cache / f"{entry_name(APP.store_path)}.json").read_text())
    assert metadata["references"] == [BASE.store_path]
    assert (cache / f"{entry_name(APP.store_path)}.provenance.json").exists()
    again = ci.lattice.push([APP.store_path, BASE.store_path], url)
    assert again.pushed == ()
    assert again.skipped == (BASE.store_path, APP.store_path)

    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[url],
        trusted_keys=[key[0].public()],
        require_provenance=True,
        cache={"secret_key_file": key[1]},
    )
    assert user.actions(APP) == ["substitute"]
    jobs = len(store_backend.jobs)
    built = user.lattice.build(APP)
    assert len(store_backend.jobs) == jobs
    assert built.store_path == APP.store_path
    info = user.lattice.path_info(APP.store_path)
    assert info.source == url
    assert info.location == "local"
    assert info.references == (BASE.store_path,)
    assert user.lattice.verify(APP.store_path).ok
    assert {type(e) for e in user.events} >= {SubstitutionStarted, SubstitutionFinished}
    assert user.actions(APP) == ["present", "present", "present"]
    with pytest.raises(StoreError, match="not built on this machine"):
        user.lattice.push([APP.store_path], f"file://{tmp_path / 'other'}")


def test_outputs_of_one_derivation(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    lib = Derivation(
        name="lib", builder=SH, outputs=("out", "dev"), env=FrozenMap({"tools": TOOLS})
    )
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(lib)
    paths = sorted(lib.output_paths.values())
    assert ci.lattice.push(paths, f"file://{cache}").pushed == tuple(paths)
    assert ci.lattice.push(paths, f"file://{cache}").skipped == tuple(paths)
    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[f"file://{cache}"],
        trusted_keys=[key[0].public()],
    )
    assert user.actions(lib) == ["substitute"]
    user.lattice.build(lib)
    for path in paths:
        assert user.lattice.path_info(path).source == f"file://{cache}"


def test_substitute_command(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(APP)
    ci.lattice.push([APP.store_path], f"file://{cache}")
    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[f"file://{cache}"],
        trusted_keys=[key[0].public()],
    )
    result = user.lattice.substitute(APP.store_path)
    assert result.substituted == (BASE.store_path, APP.store_path)
    assert user.lattice.substitute(APP.store_path).substituted == ()
    with pytest.raises(SubstitutionFailed):
        user.lattice.substitute(TOOLS.store_path.replace("tools", "other"))


def test_untrusted_and_unreachable_caches_are_skipped(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(APP)
    ci.lattice.push([APP.store_path], f"file://{cache}")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "lattice-cache.json").write_text("{}", encoding="utf-8")
    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[f"file://{tmp_path / 'bad'}", f"file://{cache}"],
        trusted_keys=[signing.generate_key("other").public()],
    )
    assert user.actions(APP) == ["present", "build", "build"]
    assert any(isinstance(e, CacheSkipped) for e in user.events)
    user.lattice.build(APP)
    assert user.lattice.path_info(APP.store_path).source == ""


def test_tampered_archives_fail(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(BASE)
    ci.lattice.push([BASE.store_path], f"file://{cache}")
    (archive,) = (cache / "archive").iterdir()
    archive.write_bytes(archive.read_bytes()[:-8] + bytes(8))
    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[f"file://{cache}"],
        trusted_keys=[key[0].public()],
    )
    with pytest.raises(SubstitutionFailed):
        user.lattice.build(BASE)
    assert not os.path.lexists(user.lattice.layout.physical(BASE.store_path))


def test_provenance_can_be_required(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str], cache: Path
) -> None:
    ci = builder_machine(tmp_path, store_backend, key)
    ci.lattice.build(BASE)
    ci.lattice.push([BASE.store_path], f"file://{cache}")
    (cache / f"{entry_name(BASE.store_path)}.provenance.json").unlink()
    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=[f"file://{cache}"],
        trusted_keys=[key[0].public()],
        require_provenance=True,
    )
    assert user.actions(BASE) == ["present", "build"]


def test_push_needs_a_key_and_a_valid_url(tmp_path: Path, store_backend: StoreBackend) -> None:
    ci = Machine(tmp_path / "builder", store_backend)
    ci.lattice.build(BASE)
    with pytest.raises(SettingsError, match="secret_key_file"):
        ci.lattice.push([BASE.store_path], f"file://{tmp_path}")
    with pytest.raises(SettingsError, match="not an https"):
        ci.lattice.push([BASE.store_path], "ftp://x")


def test_cvmfs_shallow_paths(
    tmp_path: Path, store_backend: StoreBackend, key: tuple[signing.SecretKey, str]
) -> None:
    cvmfs = tmp_path / "cvmfs"
    (cvmfs / "sw.example.org").mkdir(parents=True)
    ci = builder_machine(tmp_path, store_backend, key, cvmfs_root=str(cvmfs), cvmfs_server="true")
    ci.lattice.build(APP)
    pushed = ci.lattice.push([APP.store_path], "cvmfs://sw.example.org")
    assert pushed.pushed == (BASE.store_path, APP.store_path)
    assert (cvmfs / "sw.example.org" / "lattice-cache.json").exists()

    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=["cvmfs://sw.example.org"],
        trusted_keys=[key[0].public()],
        cache={"cvmfs_root": str(cvmfs), "shallow": {"packages": ["app"]}},
    )
    user.lattice.build(APP)
    physical = user.lattice.layout.physical(APP.store_path)
    assert os.path.islink(physical)
    assert not os.path.islink(user.lattice.layout.physical(BASE.store_path))
    assert user.lattice.path_info(APP.store_path).location == "shallow"
    assert user.lattice.verify(APP.store_path).ok

    info = user.lattice.materialise(APP.store_path)
    assert info.location == "local"
    assert not os.path.islink(physical)
    assert user.lattice.verify(APP.store_path).ok
    assert user.lattice.materialise(APP.store_path) == info
    with pytest.raises(StoreError, match="not registered"):
        user.lattice.materialise(TOOLS.store_path.replace("tools", "other"))

    other = Machine(
        tmp_path / "other",
        store_backend,
        substituters=["cvmfs://sw.example.org"],
        trusted_keys=[key[0].public()],
        cache={"cvmfs_root": str(cvmfs)},
    )
    assert other.lattice.substitute(BASE.store_path, shallow=True).substituted == (BASE.store_path,)
    assert os.path.islink(other.lattice.layout.physical(BASE.store_path))
    tree = os.readlink(other.lattice.layout.physical(BASE.store_path))
    assert BASE.store_path in other.lattice.gc().removed
    assert not os.path.lexists(other.lattice.layout.physical(BASE.store_path))
    assert os.path.isdir(tree)


def test_shallow_paths() -> None:
    stack = Derivation(
        name="stack",
        builder="builtin:stack",
        env=FrozenMap({"slot": "apps", "content_0": APP, "kernel_module_0": BASE}),
    )
    rules = Settings.model_validate_json(
        '{"cache": {"shallow": {"slots": ["apps"]}}}'
    ).cache.shallow
    assert shallow_paths((stack, APP, BASE), rules) == {stack.store_path, APP.store_path}
    rules = rules.model_copy(update={"slots": (), "packages": ("ba*",)})
    assert shallow_paths((stack, APP, BASE), rules) == set()
    assert shallow_paths((APP, BASE), rules) == {BASE.store_path}


def test_cli(
    tmp_path: Path,
    store_backend: StoreBackend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = tmp_path / "secret"
    assert cli.main(["generate-key", "ci-1", str(secret)]) == 0
    public = capsys.readouterr().out.strip()
    assert public == signing.SecretKey.load(str(secret)).public()
    with pytest.raises(SystemExit):
        cli.main(["generate-key", "bad name", str(tmp_path / "other")])
    with pytest.raises(SystemExit):
        cli.main(["--daemon", "generate-key", "ci", str(tmp_path / "other")])
    capsys.readouterr()

    cvmfs = tmp_path / "cvmfs"
    (cvmfs / "sw").mkdir(parents=True)
    ci = Machine(
        tmp_path / "builder",
        store_backend,
        cache={"secret_key_file": str(secret), "cvmfs_root": str(cvmfs), "cvmfs_server": "true"},
    )
    ci.lattice.build(APP)
    url = f"file://{tmp_path / 'cache'}"
    monkeypatch.setattr(cli, "make_api", lambda args: ci.lattice)
    assert cli.main(["push", "--to", url, APP.store_path]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [BASE.store_path, APP.store_path, "pushed 2 store paths, 0 already in the cache"]

    assert cli.main(["push", "--to", "cvmfs://sw", BASE.store_path]) == 0
    capsys.readouterr()

    user = Machine(
        tmp_path / "user",
        store_backend,
        substituters=["cvmfs://sw", url],
        trusted_keys=[public],
        cache={"cvmfs_root": str(cvmfs)},
    )
    user.lattice.on_event = cli.progress
    monkeypatch.setattr(cli, "make_api", lambda args: user.lattice)
    assert cli.main(["substitute", "--shallow", BASE.store_path]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{BASE.store_path}\n"
    assert f"fetching {BASE.store_path} from cvmfs://sw" in captured.err
    assert cli.main(["materialise", BASE.store_path]) == 0
    assert capsys.readouterr().out == f"{BASE.store_path}: local\n"
    assert cli.main(["substitute", APP.store_path]) == 0
    assert capsys.readouterr().out == f"{APP.store_path}\n"


def test_progress(capsys: pytest.CaptureFixture[str]) -> None:
    cli.progress(CacheSkipped(path=APP.store_path, cache="https://c", reason="no entry"))
    cli.progress(PathPushed(path=APP.store_path, cache="https://c"))
    assert capsys.readouterr().err == (
        f"skipped https://c: no entry\npushed {APP.store_path} to https://c\n"
    )
