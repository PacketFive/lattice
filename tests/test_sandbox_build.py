# SPDX-License-Identifier: GPL-2.0
"""Builds in the namespace backend: RFC 0001 sections 9.2 to 9.5.

The builder is sandbox_probe.c, linked statically so it needs nothing
from the sandbox. The tests are skipped where unprivileged user
namespaces or static linking are not available.
"""

import errno
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import BuildFailed, Derivation, SandboxSettings
from lattice.sandbox import BuildOutcome, BuildRequest, run_build
from lattice.sandbox.environment import FIXED_VARIABLES
from lattice.sandbox.probe import namespaces_status

pytestmark = pytest.mark.requires_userns

TOOLS = Derivation(name="sandbox-probe", builder="/bin/sh")
PROBE = f"{TOOLS.store_path}/bin/probe"


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    status = namespaces_status()
    if not status.available:
        pytest.skip(status.reason)
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("no C compiler")
    root = tmp_path_factory.mktemp("store")
    target = root / Path(TOOLS.store_path).name / "bin/probe"
    target.parent.mkdir(parents=True)
    source = Path(__file__).with_name("sandbox_probe.c")
    result = subprocess.run(
        [compiler, "-static", "-O2", "-o", str(target), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot link statically: {result.stderr.strip()[-200:]}")
    return root


def derivation(
    *args: str, outputs: tuple[str, ...] = ("out",), inputs: dict[str, Derivation] | None = None
) -> Derivation:
    return Derivation(
        name="probe-run",
        builder=PROBE,
        args=args,
        env=FrozenMap({"tools": TOOLS, "greeting": "hi", **(inputs or {})}),
        outputs=outputs,
    )


def build(
    store: Path, work: Path, *args: str, inputs: dict[str, Derivation] | None = None, **limits: int
) -> BuildOutcome:
    request = BuildRequest(
        derivation=derivation(*args, inputs=inputs),
        work_dir=str(work),
        host_store=str(store),
        sandbox=SandboxSettings(**limits),
    )
    return run_build(request)


def report(outcome: BuildOutcome) -> dict[str, str]:
    lines = Path(outcome.log).read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and ":" not in line[:4])


def test_environment(store: Path, tmp_path: Path) -> None:
    outcome = build(store, tmp_path, "env", "a b")
    lines = Path(outcome.log).read_text(encoding="utf-8").splitlines()
    env = dict(line[4:].split("=", 1) for line in lines if line.startswith("env:"))
    assert env == {
        **FIXED_VARIABLES,
        "tools": TOOLS.store_path,
        "greeting": "hi",
        "out": derivation("env", "a b").store_path,
    }
    values = report(outcome)
    assert values["hostname"] == "localhost"
    assert (values["pid"], values["uid"]) == ("1", "0")
    assert values["cwd"] == "/build"
    assert values["umask"] == "22"
    assert "arg=a b" in lines
    output = Path(outcome.outputs["out"])
    assert (output / "file").read_text(encoding="utf-8") == "data\n"
    assert stat.S_IMODE((output / "file").stat().st_mode) == 0o644


def test_isolation(store: Path, tmp_path: Path) -> None:
    values = report(build(store, tmp_path, "isolation", TOOLS.store_path))
    assert values["etc"] == str(errno.ENOENT)
    assert values["route"] == str(errno.ENETUNREACH)
    assert values["loopback"] == "0"
    assert values["write_input"] == str(errno.EROFS)
    assert values["umount_input"] == str(errno.EPERM)
    assert values["remount_input"] == str(errno.EPERM)
    assert values["mount"] == str(errno.EPERM)
    assert values["null"] == "0"
    assert values["shm"] == "0"


def test_hardening(store: Path, tmp_path: Path) -> None:
    outcome = build(store, tmp_path, "harden")
    values = report(outcome)
    for name in ("chmod_setuid", "chmod_setgid", "setxattr", "keyctl", "io_uring"):
        assert values[name] == str(errno.EPERM), name
    assert values["chmod_plain"] == "0"
    assert values["create_setuid"] == "0"
    output = Path(outcome.outputs["out"])
    assert stat.S_IMODE((output / "suid").stat().st_mode) == 0o755
    assert stat.S_IMODE((output / "file").stat().st_mode) == 0o700


def test_failing_builder(store: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildFailed, match="exited with status 2") as info:
        build(store, tmp_path, "fail")
    assert "failing on purpose" in str(info.value)


def test_missing_output(store: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildFailed, match="did not create output out"):
        build(store, tmp_path, "nothing")


def test_symlink_output_is_not_followed(store: Path, tmp_path: Path) -> None:
    output = Path(build(store, tmp_path, "escape").outputs["out"])
    assert output.is_symlink()
    assert os.readlink(output) == "/etc/passwd"


def test_symlink_input_is_not_mounted(store: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key").write_text("private\n", encoding="utf-8")
    linked = Derivation(name="linked", builder="/bin/sh", args=("-c", str(secret)))
    link = store / Path(linked.store_path).name
    link.symlink_to(secret)
    (tmp_path / "work").mkdir()
    try:
        outcome = build(
            store, tmp_path / "work", "open", f"{linked.store_path}/key", inputs={"linked": linked}
        )
    finally:
        link.unlink()
    assert report(outcome)["open"] == str(errno.ENOENT)


def test_special_file_fails(store: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildFailed, match="pipe is not a regular file"):
        build(store, tmp_path, "fifo")


def test_timeout_kills_the_build(store: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildFailed, match="timed out after 1 s") as info:
        build(store, tmp_path, "sleep", timeout=1)
    assert "sleeping" in str(info.value)


def test_missing_input_fails_setup(store: Path, tmp_path: Path) -> None:
    request = BuildRequest(
        derivation=derivation("env"), work_dir=str(tmp_path), host_store=str(tmp_path / "none")
    )
    with pytest.raises(BuildFailed, match="sandbox setup failed"):
        run_build(request)
