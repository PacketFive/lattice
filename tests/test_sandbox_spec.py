# SPDX-License-Identifier: GPL-2.0
"""How Lattice describes a build to Cloche: RFC 0001 sections 8 and 9.2 to 9.7.

Nothing here creates a namespace; test_sandbox_build.py does. Cloche's
own tests cover the sandbox rules that do not involve derivations.
"""

import json
import os
import subprocess
from pathlib import Path

import cloche
import pytest
from cloche import FIXED_VARIABLES, Limits, Outcome, RunFailed, SandboxUnavailable
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
    sandbox,
)
from lattice.derivation import STORE_DIR, output_path
from lattice.sandbox import (
    BackendStatus,
    BuildRequest,
    SandboxSpec,
    build_command,
    host_path,
    input_closure,
    make_spec,
    microvm_config,
    oci_config,
    run_build,
    select_backend,
)

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
    env = spec_of(drv).env
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
        ({"PATH": "/bin"}, "hello: env sets PATH, which the sandbox fixes"),
        ({"out": "/x"}, "hello: env sets out, which names an output"),
        ({"a=b": "x"}, "cannot be passed"),
        ({"a": "x\0y"}, "cannot be passed"),
    ],
)
def test_environment_rejects(env: dict[str, str], message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        spec_of(hello(**env))


def test_command() -> None:
    drv = hello()
    command = build_command(drv, host_store="/srv/store")
    assert command.program == drv.builder
    assert command.directories == (STORE_DIR,)
    assert command.outputs == {"out": drv.store_path}
    sources = {b.destination: b.source for b in command.binds}
    assert sources == {
        path: f"/srv/store/{path.rpartition('/')[2]}" for path in TOOLS.output_paths.values()
    }
    only = build_command(drv, inputs=(TOOLS.store_path,))
    assert [b.destination for b in only.binds] == [TOOLS.store_path]


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
    assert spec.program == drv.builder
    assert spec.outputs == {"out": drv.store_path}
    assert spec.cwd == "/build"
    assert spec.hostname == "localhost"
    assert (spec.uid_map.outside, spec.gid_map.outside) == (1000, 100)
    kinds = {m.destination: (m.kind, m.read_only) for m in spec.mounts}
    assert kinds[STORE_DIR] == ("tmpfs", False)
    assert kinds[TOOLS.store_path] == ("bind", True)
    assert kinds[f"{TOOLS.store_path}-dev"] == ("bind", True)
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
    assert value["ociVersion"] == "1.0.2"
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


def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", "Traceback\nPermissionError: denied\n")


def test_backend_status_lists_every_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", failed_run)
    statuses = Lattice().backend_status()
    assert [s.name for s in statuses] == ["namespaces", "oci", "microvm", "cgroups"]
    assert statuses[1].reason.startswith("unprivileged user namespaces do not work")
    assert statuses[2].reason == "no guest kernel and init are configured"


def test_cgroup_limits_reach_cloche(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SandboxSettings(max_memory_mib=256, max_cpu=0.5, max_processes=64)
    limits = sandbox._limits(settings)
    assert (limits.max_memory, limits.max_cpu, limits.max_processes) == (256 << 20, 0.5, 64)
    assert sandbox._limits(SandboxSettings()).max_memory is None
    monkeypatch.setattr(
        cloche, "cgroup_status", lambda: cloche.CgroupStatus(available=False, reason="no")
    )
    assert sandbox.backend_status()[-1] == BackendStatus(
        name="cgroups", available=False, reason="no"
    )


def test_errors_become_lattice_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(error: Exception) -> object:
        def run(*args: object, **kwargs: object) -> Outcome:
            raise error

        return run

    request = BuildRequest(derivation=hello(), work_dir=str(tmp_path))
    monkeypatch.setattr("cloche.run", failing(RunFailed("hello: the program exited")))
    with pytest.raises(BuildFailed, match="hello: the program exited"):
        run_build(request)
    monkeypatch.setattr("cloche.run", failing(SandboxUnavailable("no backend")))
    with pytest.raises(BackendUnavailable, match="no backend"):
        run_build(request)


def test_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def run(command: object, work: str, limits: object, **kwargs: object) -> Outcome:
        seen.update(command=command, limits=limits)
        return Outcome(outputs={"out": f"{work}/out/out"}, log=f"{work}/run.log", seconds=1.5)

    monkeypatch.setattr("cloche.run", run)
    sandbox = SandboxSettings(timeout=9, max_open_files=64)
    outcome = run_build(BuildRequest(derivation=hello(), work_dir=str(tmp_path), sandbox=sandbox))
    assert outcome.outputs == {"out": f"{tmp_path}/out/out"}
    assert outcome.seconds == 1.5
    assert seen["command"] == build_command(hello())
    assert seen["limits"] == Limits(timeout=9, max_open_files=64)


def test_select_backend_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BackendUnavailable, match="no guest kernel"):
        select_backend("microvm")
    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(BackendUnavailable, match="cannot run here"):
        select_backend("oci")
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
    assert lines[1].startswith("oci: unprivileged user namespaces do not work")
    assert lines[2] == "microvm: no guest kernel and init are configured"


def test_host_path() -> None:
    assert host_path(f"{STORE_DIR}/x-a/vmlinux", "/srv/root/store") == "/srv/root/store/x-a/vmlinux"
    assert host_path("/boot/vmlinux", "/srv/root/store") == "/boot/vmlinux"


def test_microvm_config_maps_store_files() -> None:
    sandbox = SandboxSettings.model_validate(
        {
            "microvm": {
                "vmm": "cloud-hypervisor",
                "kernel": f"{STORE_DIR}/x-k/vmlinux",
                "init": "/srv/init",
                "cpus": 4,
            }
        }
    )
    config = microvm_config(sandbox, "/r/store")
    assert config.vmm == "cloud-hypervisor"
    assert config.kernel == "/r/store/x-k/vmlinux"
    assert config.init == "/srv/init"
    assert config.cpus == 4


def test_select_backend_passes_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def select(name: str, **kwargs: object) -> str:
        seen.update(kwargs, name=name)
        return "backend"

    monkeypatch.setattr("cloche.select_backend", select)
    sandbox = SandboxSettings.model_validate(
        {"runtime": "/usr/bin/crun", "microvm": {"kernel": f"{STORE_DIR}/x-k/vmlinux"}}
    )
    assert select_backend("oci", sandbox, "/r/store") == "backend"
    assert seen["name"] == "oci"
    assert seen["runtime"] == "/usr/bin/crun"
    assert seen["microvm"].kernel == "/r/store/x-k/vmlinux"  # type: ignore[attr-defined]


def test_sandbox_settings_reject_relative_paths() -> None:
    assert SandboxSettings(runtime="crun").runtime == "crun"
    with pytest.raises(SettingsError, match="runtime"):
        SandboxSettings(runtime="bin/crun")
    for name in ("vmm_path", "kernel", "init"):
        with pytest.raises(SettingsError, match=name):
            SandboxSettings(microvm={name: "vmlinux"})  # type: ignore[arg-type]
    with pytest.raises(SettingsError, match="kernel"):
        SandboxSettings(microvm={"kernel": "/boot/../vmlinux"})  # type: ignore[arg-type]
    with pytest.raises(SettingsError, match="vmm"):
        SandboxSettings(microvm={"vmm": "qemu"})  # type: ignore[arg-type]
