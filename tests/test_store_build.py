# SPDX-License-Identifier: GPL-2.0
"""Building into the store in the namespace backend: RFC 0001 sections 10 to 16.

The builder is sandbox_probe.c, linked statically and registered as the
output of TOOLS. The tests are skipped where unprivileged user
namespaces or static linking are not available.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation, Lattice, Settings
from lattice.sandbox.probe import namespaces_status
from lattice.store import database
from lattice.store.realise import realise

pytestmark = pytest.mark.requires_userns

TOOLS = Derivation(name="sandbox-probe", builder="/bin/sh")
PROBE = f"{TOOLS.store_path}/bin/probe"


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    status = namespaces_status()
    if not status.available:
        pytest.skip(status.reason)
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("no C compiler")
    target = tmp_path_factory.mktemp("probe") / "probe"
    source = Path(__file__).with_name("sandbox_probe.c")
    result = subprocess.run(
        [compiler, "-static", "-O2", "-o", str(target), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot link statically: {result.stderr.strip()[-200:]}")
    return target


@pytest.fixture
def lattice(tmp_path: Path, probe: Path) -> Lattice:
    settings = Settings.model_validate({"store": {"root": str(tmp_path / "root")}})
    lattice = Lattice(settings)
    connection = database.connect(lattice.layout, create=True)
    try:
        work = Path(lattice.layout.staging) / "tools"
        (work / "bin").mkdir(parents=True)
        shutil.copy(probe, work / "bin" / "probe")
        realise(lattice.layout, connection, TOOLS, {"out": str(work)}, [])
    finally:
        connection.close()
    return lattice


def run(name: str, *args: str, **inputs: Derivation) -> Derivation:
    return Derivation(name=name, builder=PROBE, args=args, env=FrozenMap(inputs))


def test_build_records_references(lattice: Lattice) -> None:
    a = run("a", "refer", PROBE, tools=TOOLS)
    c = run("c", "refer", f"{a.store_path}/refs", a=a)
    result = lattice.build(c)
    assert result.built
    assert lattice.path_info(a.store_path).references == (TOOLS.store_path,)
    assert lattice.path_info(c.store_path).references == (a.store_path,)
    physical = Path(lattice.layout.physical(c.store_path))
    assert (physical / "refs").read_text(encoding="utf-8") == f"{a.store_path}/refs\n"
    assert lattice.verify(c.store_path).ok
    assert not lattice.build(c).built


def test_check(lattice: Lattice) -> None:
    same = run("same", "env", tools=TOOLS)
    lattice.build(same)
    assert lattice.check(same).built
    clock = run("clock", "clock", tools=TOOLS)
    lattice.build(clock)
    with pytest.raises(BuildFailed, match="clock: the rebuilt outputs differ:\nout: clock"):
        lattice.check(clock)


def test_gc_keeps_roots(lattice: Lattice) -> None:
    a = run("a", "refer", PROBE, tools=TOOLS)
    lattice.build(a)
    lattice.add_root("system", a.store_path)
    assert lattice.gc().removed == ()
    lattice.remove_root("system")
    assert set(lattice.gc().removed) == {a.store_path, TOOLS.store_path}
