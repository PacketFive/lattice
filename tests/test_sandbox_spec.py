# SPDX-License-Identifier: GPL-2.0
"""Sandbox description tests: RFC 0001 sections 8 and 9.2 to 9.7.

Nothing here creates a namespace; test_sandbox_build.py does.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import (
    BackendUnavailable,
    BuildFailed,
    Derivation,
    Lattice,
    SandboxSettings,
    Settings,
    SettingsError,
    cli,
)
from lattice.derivation import STORE_DIR, output_path
from lattice.sandbox import SandboxSpec, probe, select_backend
from lattice.sandbox.environment import FIXED_VARIABLES, build_environment
from lattice.sandbox.namespaces import copy_out
from lattice.sandbox.spec import input_closure, make_spec, oci_config

TOOLS = Derivation(name="tools", builder="/bin/sh", outputs=("out", "dev"))


def hello(**env: object) -> Derivation:
    values = FrozenMap({"tools": TOOLS, **env})
    return Derivation(name="hello", builder=f"{TOOLS.store_path}/bin/sh", env=values)


def spec_of(drv: Derivation, machine: str = "x86_64") -> SandboxSpec:
    return make_spec(drv, SandboxSettings(), uid=1000, gid=100, machine=machine)


def test_output_paths() -> None:
    drv = Derivation(name="lib", builder="/bin/sh", outputs=("out", "dev"))
    out, dev = drv.output_paths["out"], drv.output_paths["dev"]
    assert out == drv.store_path
    assert dev == f"{out}-dev"
    assert output_path(drv, "dev", "/s").startswith("/s/")
    with pytest.raises(ValueError, match="no output 'doc'"):
        output_path(drv, "doc")


def test_store_path_is_first_output() -> None:
    drv = Derivation(name="lib", builder="/bin/sh", outputs=("dev", "out"))
    assert drv.store_path == drv.output_paths["dev"]


def test_sandbox_settings(tmp_path: Path) -> None:
    assert Settings().sandbox == SandboxSettings()
    assert SandboxSettings().max_open_files == 1024
    path = tmp_path / "s.toml"
    path.write_text("[sandbox]\ntimeout = 60\nmax_silent_time = 5\n", encoding="utf-8")
    loaded = Settings.load(path).sandbox
    assert (loaded.timeout, loaded.max_silent_time) == (60, 5)
    for bad in ({"timeout": 0}, {"max_silent_time": -1}, {"max_open_files": 10}):
        with pytest.raises(SettingsError):
            SandboxSettings(**bad)  # type: ignore[arg-type]


def test_environment() -> None:
    drv = hello(greeting="hi")
    env = build_environment(drv)
    assert list(env) == sorted(env)
    assert env["greeting"] == "hi"
    assert env["tools"] == TOOLS.store_path
    assert env["out"] == drv.store_path
    for name, value in FIXED_VARIABLES.items():
        assert env[name] == value
    assert set(env) == {"greeting", "tools", "out", *FIXED_VARIABLES}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PATH": "/bin"}, "sandbox fixes"),
        ({"out": "/x"}, "names an output"),
        ({"a=b": "x"}, "cannot be passed"),
        ({"a": "x\0y"}, "cannot be passed"),
    ],
)
def test_environment_rejects(env: dict[str, str], message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        build_environment(hello(**env))


def test_input_closure_is_transitive() -> None:
    base = Derivation(name="base", builder="/bin/sh")
    middle = Derivation(name="middle", builder="/bin/sh", env=FrozenMap({"b": base}))
    top = Derivation(name="top", builder="/bin/sh", env=FrozenMap({"m": middle, "b2": base}))
    closure = input_closure(top)
    assert set(closure) == {base, middle}
    assert [d.store_path for d in closure] == sorted(d.store_path for d in closure)


def test_spec() -> None:
    drv = hello()
    spec = spec_of(drv)
    assert spec.inputs == tuple(TOOLS.output_paths.values())
    assert spec.outputs == {"out": drv.store_path}
    assert spec.cwd == "/build"
    assert spec.hostname == "localhost"
    assert (spec.uid_map.outside, spec.gid_map.outside) == (1000, 100)
    kinds = {m.destination: (m.kind, m.read_only) for m in spec.mounts}
    assert kinds[STORE_DIR] == ("tmpfs", False)
    assert kinds[TOOLS.store_path] == ("bind", True)
    assert kinds["/proc"] == ("proc", False)
    assert kinds["/dev/null"] == ("bind", False)
    assert "/etc" not in kinds
    destinations = [m.destination for m in spec.mounts]
    assert destinations.index(STORE_DIR) < destinations.index(TOOLS.store_path)
    assert destinations.index("/dev") < destinations.index("/dev/shm")  # noqa: S108


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        ("builtin:fetchurl", "runs in Lattice"),
        ("/bin/sh", "inside one of"),
        (f"{TOOLS.store_path}/../x/bin/sh", "inside one of"),
        (f"{TOOLS.store_path}x/bin/sh", "inside one of"),
    ],
)
def test_spec_rejects_builder(builder: str, message: str) -> None:
    drv = Derivation(name="bad", builder=builder, env=FrozenMap({"tools": TOOLS}))
    with pytest.raises(BuildFailed, match=message):
        spec_of(drv)


def test_spec_rejects_unknown_machine() -> None:
    with pytest.raises(BackendUnavailable, match="riscv64"):
        spec_of(hello(), machine="riscv64")


def test_oci_config() -> None:
    drv = hello()
    value = json.loads(oci_config(spec_of(drv, "aarch64")).model_dump_json())
    assert value["ociVersion"] == "1.2.0"
    assert value["root"] == {"path": "rootfs", "readonly": False}
    process = value["process"]
    assert process["args"] == [drv.builder]
    assert process["noNewPrivileges"] is True
    assert process["user"] == {"uid": 0, "gid": 0}
    assert all(v == [] for v in process["capabilities"].values())
    assert f"out={drv.store_path}" in process["env"]
    assert {"type": "RLIMIT_CORE", "hard": 0, "soft": 0} in process["rlimits"]
    linux = value["linux"]
    assert [n["type"] for n in linux["namespaces"]] == [
        "user",
        "mount",
        "network",
        "pid",
        "uts",
        "ipc",
    ]
    assert linux["uidMappings"] == [{"containerID": 0, "hostID": 1000, "size": 1}]
    assert linux["seccomp"]["architectures"] == ["SCMP_ARCH_AARCH64"]
    tools = next(m for m in value["mounts"] if m["destination"] == TOOLS.store_path)
    assert tools == {
        "destination": TOOLS.store_path,
        "type": "bind",
        "source": TOOLS.store_path,
        "options": ["rbind", "nosuid", "nodev", "ro"],
    }


def test_copy_out(tmp_path: Path) -> None:
    root = tmp_path / "root"
    built = root / "lattice/store/abc-x"
    (built / "bin").mkdir(parents=True)
    (built / "bin/tool").write_text("tool", encoding="utf-8")
    (built / "bin/tool").chmod(0o4755)
    (built / "link").symlink_to("/etc/passwd")
    (built / "share").mkdir(mode=0o555)
    destination = tmp_path / "out/abc-x"
    destination.parent.mkdir()
    copy_out(str(root), "/lattice/store/abc-x", str(destination))
    assert (destination / "bin/tool").read_text(encoding="utf-8") == "tool"
    assert stat.S_IMODE((destination / "bin/tool").stat().st_mode) == 0o755
    assert os.readlink(destination / "link") == "/etc/passwd"
    assert stat.S_IMODE((destination / "share").stat().st_mode) == 0o555
    (destination / "share").chmod(0o755)


def test_copy_out_does_not_follow_links(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "lattice").mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.mkdir()
    (root / "lattice/store").symlink_to(secret)
    (secret / "abc-x").write_text("host file", encoding="utf-8")
    with pytest.raises(OSError):
        copy_out(str(root), "/lattice/store/abc-x", str(tmp_path / "copy"))
    assert not (tmp_path / "copy").exists()


def test_copy_out_rejects_special_files(tmp_path: Path) -> None:
    store = tmp_path / "root/lattice/store"
    (store / "abc-x").mkdir(parents=True)
    os.mkfifo(store / "abc-x/pipe")
    (tmp_path / "out").mkdir()
    with pytest.raises(ValueError, match="pipe is not a regular file"):
        copy_out(str(tmp_path / "root"), "/lattice/store/abc-x", str(tmp_path / "out/abc-x"))


def test_copy_out_missing(tmp_path: Path) -> None:
    (tmp_path / "root/lattice/store").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        copy_out(str(tmp_path / "root"), "/lattice/store/abc-x", str(tmp_path / "abc-x"))


def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", "Traceback\nPermissionError: denied\n")


def test_probe_explains_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sysctl = tmp_path / "restrict"
    sysctl.write_text("1\n", encoding="ascii")
    monkeypatch.setattr(subprocess, "run", failed_run)
    monkeypatch.setattr(
        probe, "_SYSCTLS", ((str(tmp_path / "none"), "0", "no"), (str(sysctl), "1", "restricted"))
    )
    status = probe.namespaces_status()
    assert not status.available
    assert (
        status.reason
        == "unprivileged user namespaces do not work: PermissionError: denied (restricted)"
    )


def test_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("python", 30)

    monkeypatch.setattr(subprocess, "run", slow)
    assert "timed out" in probe.namespaces_status().reason


def test_probe_unsupported_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.libc, "machine", lambda: "riscv64")
    assert probe.namespaces_status().reason == "riscv64 is not supported"


def test_backend_status_lists_every_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", failed_run)
    statuses = Lattice().backend_status()
    assert [s.name for s in statuses] == ["namespaces", "oci", "microvm"]
    assert statuses[1].reason == "not available in this release"


def test_select_backend_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BackendUnavailable, match="oci"):
        select_backend("oci")
    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(BackendUnavailable, match="cannot run here"):
        select_backend("namespaces")


def test_lattice_sandbox_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lattice.api.machine", lambda: "x86_64")
    config = Lattice(Settings(sandbox=SandboxSettings(max_open_files=64))).sandbox_config(hello())
    assert config.linux.uid_mappings[0].host_id == os.getuid()
    assert {"type": "RLIMIT_NOFILE", "hard": 64, "soft": 64} in [
        r.model_dump() for r in config.process.rlimits
    ]


CLI_SOURCE = (
    "t = derivation(name='t', builder='/bin/sh')\n"
    "h = derivation(name='h', builder=f'{t}/bin/sh', env={'t': t})\n"
)


def test_cli_sandbox_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lattice.api.machine", lambda: "x86_64")
    path = tmp_path / "t.pyb"
    path.write_text(CLI_SOURCE, encoding="utf-8")
    assert cli.main(["sandbox-config", str(path), "h"]) == 0
    config = json.loads(capsys.readouterr().out)
    assert config["process"]["args"][0].endswith("-t/bin/sh")
    assert cli.main(["sandbox-config", str(path)]) == 1
    assert "several derivations (t, h)" in capsys.readouterr().err
    assert cli.main(["sandbox-config", str(path), "x"]) == 1
    assert "no derivation named 'x'" in capsys.readouterr().err
    assert cli.main(["sandbox-config", str(path), "t"]) == 1
    assert "inside one of" in capsys.readouterr().err
    path.write_text(CLI_SOURCE.splitlines()[0] + "\n", encoding="utf-8")
    assert cli.main(["sandbox-config", str(path)]) == 1


def test_cli_backend_status(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", failed_run)
    assert cli.main(["backend-status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("namespaces: unprivileged user namespaces do not work")
    assert lines[1:] == [
        "oci: not available in this release",
        "microvm: not available in this release",
    ]
