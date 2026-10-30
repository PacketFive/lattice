# SPDX-License-Identifier: GPL-2.0
"""Whole systems: machine, the system builder and activation (RFC 0004)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap
from pydantic import BaseModel, ValidationError

from lattice import (
    ActivationFailed,
    ActivationNotice,
    ActivationStep,
    BuildFailed,
    Client,
    Derivation,
    EvaluationFailed,
    Event,
    EventHandler,
    Identity,
    Lattice,
    LatticeAPI,
    OfflineManager,
    PermissionDenied,
    ProfileSwitched,
    Role,
    Settings,
    StoreError,
    SystemGeneration,
    SystemSettings,
    cli,
)
from lattice.derivation import BUILTIN_SYSTEM
from lattice.protocol import NoParams, RollbackSystemParams, SwitchSystemParams
from lattice.server import authorize
from lattice.stdlib import etc_file, file_system, group, machine, service, user
from lattice.store import database
from lattice.store.realise import realise
from lattice.system import boot
from lattice.system import manager as manager_module
from lattice.system.accounts import read_ids
from lattice.system.build import read_system, system_spec
from lattice.system.config import CONFIG_FILE, read_config
from lattice.system.manager import SystemdManager
from lattice.transport import Daemon

if TYPE_CHECKING:
    from conftest import FakeKernel, StoreBackend


def package(name: str) -> Derivation:
    return Derivation(name=name, builder="/bin/sh", args=(name,))


SH = package("sh")
DAEMON = package("daemon")
KERNEL = package("kernel")
INITRD = package("initrd")
NO_CONFIG = FrozenMap()
plain_machine = partial(machine, config=NO_CONFIG)


def host(
    *,
    users: tuple[FrozenMap, ...] = (),
    services: tuple[FrozenMap, ...] = (),
    etc: tuple[FrozenMap, ...] = (),
    file_systems: tuple[FrozenMap, ...] = (),
    host_name: str = "host",
    kernel: Derivation | None = KERNEL,
) -> Derivation:
    return machine(
        "host",
        kernel=kernel,
        initrd=INITRD if kernel is not None else None,
        packages=(SH, DAEMON),
        users=users,
        groups=(group("staff"),),
        file_systems=(
            file_system("/", "/dev/vda2", "ext4", needed_for_boot=True),
            *file_systems,
        ),
        services=services,
        etc=etc,
        host_name=host_name,
        config=NO_CONFIG,
    )


ALICE_USER = user("alice", groups=("staff",), shell=f"{SH.store_path}/bin/sh")  # noqa: S604
WEB = service("web", f"{DAEMON.store_path}/bin/daemon --port 80", after=("network.target",))
FIRST = host(
    users=(ALICE_USER,),
    services=(WEB,),
    etc=(etc_file("motd", text="hello\n"), etc_file("app/settings.conf", text="a=1\n")),
)
SECOND = host(
    services=(service("web", f"{DAEMON.store_path}/bin/daemon --port 8080"),),
    etc=(etc_file("motd", text="hello again\n"),),
    file_systems=(file_system("/data", "/dev/vdb", "xfs"),),
    host_name="host2",
)


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def manager() -> OfflineManager:
    return OfflineManager()


@pytest.fixture
def lattice(
    tmp_path: Path,
    store_backend: StoreBackend,
    kernel: FakeKernel,
    events: list[Event],
    manager: OfflineManager,
) -> Lattice:
    settings = Settings.model_validate(
        {
            "store": {"root": str(tmp_path / "store"), "stacks": str(tmp_path / "stacks")},
            "system": {"root": str(tmp_path / "sysroot")},
        }
    )
    lattice = Lattice(
        settings,
        backend=store_backend,
        kernel_modules=kernel,
        on_event=events.append,
        system_manager=manager,
    )
    for drv, files in (
        (SH, {"bin/sh": b"sh"}),
        (DAEMON, {"bin/daemon": b"daemon"}),
        (KERNEL, {"bzImage": b"kernel image"}),
        (INITRD, {"initrd": b"initrd image"}),
    ):
        register(lattice, drv, files)
    return lattice


def register(lattice: Lattice, drv: Derivation, files: dict[str, bytes]) -> None:
    connection = database.connect(lattice.layout, create=True)
    try:
        work = Path(lattice.layout.staging) / uuid.uuid4().hex
        work.mkdir()
        for name, data in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_bytes(data)
        realise(lattice.layout, connection, drv, {"out": str(work)}, [])
    finally:
        connection.close()


def root(lattice: Lattice) -> Path:
    return Path(lattice.settings.system.root)


def etc_text(lattice: Lattice, name: str) -> str:
    """Read ``/etc/<name>`` of the system root, following its link through ``/etc/static``."""
    etc = root(lattice) / "etc"
    assert os.readlink(etc / name) == f"/etc/static/{name}"
    system = os.path.dirname(os.readlink(etc / "static"))
    return (Path(lattice.layout.physical(system)) / "etc" / name).read_text()


def table(path: Path) -> dict[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.split(":")[0]: line.split(":") for line in lines}


def test_machine_records_the_system() -> None:
    assert FIRST.builder == BUILTIN_SYSTEM
    assert FIRST.outputs == ("out",)
    spec = system_spec(FIRST)
    assert (spec.name, spec.host_name, spec.kernel) == ("host", "host", KERNEL.store_path)
    assert [u.name for u in spec.users] == ["alice"]
    assert spec.config == {}
    assert isinstance(FIRST.env["sw"], Derivation)
    assert FIRST.env["kernel"] == KERNEL
    assert host() != host(host_name="other")


@pytest.mark.parametrize(
    ("call", "error", "match"),
    [
        (
            lambda: plain_machine(
                "m", services=(service("x", "/lattice/store/" + "a" * 32 + "-y"),)
            ),
            ValueError,
            "not in the system's packages",
        ),
        (lambda: plain_machine("m", users=(group("g"),)), TypeError, "list of user records"),
        (lambda: plain_machine("m", kernel="k"), TypeError, "kernel must be a derivation"),  # pyright: ignore[reportArgumentType]
        (lambda: plain_machine(1), TypeError, "name must be a string"),  # pyright: ignore[reportArgumentType]
        (lambda: plain_machine("m", host_name="-bad"), ValueError, "not a valid host name"),
        (lambda: plain_machine("m", users=(user("root"),)), ValueError, "created by Lattice"),
        (lambda: plain_machine("m", etc=(etc_file("passwd", text="x"),)), ValueError, "written by"),
        (lambda: user("Bad Name"), ValueError, "not a valid"),
        (lambda: service("x", "/bin/sh"), ValueError, "must start with a store path"),
        (lambda: etc_file("a", source="x"), TypeError, "source must be a derivation"),  # pyright: ignore[reportArgumentType]
    ],
)
def test_machine_errors(call: Callable[[], object], error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        call()


def test_machine_needs_config() -> None:
    with pytest.raises(TypeError):
        machine("m")  # type: ignore[call-arg]


def test_system_spec_checks_env() -> None:
    env = dict(FIRST.env.items())
    env["extra"] = "x"
    tampered = Derivation(name=FIRST.name, builder=BUILTIN_SYSTEM, env=FrozenMap(env))
    with pytest.raises(BuildFailed, match="unexpected env entry"):
        system_spec(tampered)


def test_switch_activates_the_system(
    lattice: Lattice, manager: OfflineManager, events: list[Event]
) -> None:
    result = lattice.switch_system(FIRST)
    path = result.store_path
    assert (result.number, result.name, result.current) == (1, "host", True)
    sysroot = root(lattice)
    out = Path(lattice.layout.physical(path))

    assert json.loads((out / "lattice-system.json").read_text())["name"] == "host"
    assert os.readlink(out / "sw") == system_spec(FIRST).sw
    assert os.readlink(out / "kernel") == f"{KERNEL.store_path}/bzImage"
    assert os.readlink(out / "initrd") == f"{INITRD.store_path}/initrd"
    assert stat.S_IMODE((out / "init").stat().st_mode) == 0o555
    assert f"lattice activate-boot --system {path}" in (out / "init").read_text()
    assert (out / "etc/hostname").read_text() == "host\n"
    assert "/dev/vda2 / ext4 defaults 0 1" in (out / "etc/fstab").read_text()
    assert "ExecStart=" in (out / "etc/systemd/system/web.service").read_text()
    assert os.readlink(out / "units") == "etc/systemd/system"
    assert read_system(lattice.layout, path) == system_spec(FIRST)

    etc = sysroot / "etc"
    assert os.readlink(etc / "static") == f"{path}/etc"
    assert os.readlink(etc / "motd") == "/etc/static/motd"
    assert os.readlink(etc / "app/settings.conf") == "/etc/static/app/settings.conf"
    passwd = table(etc / "passwd")
    assert passwd["alice"][2:4] == ["1000", "100"]
    assert passwd["alice"][6] == f"{SH.store_path}/bin/sh"
    assert passwd["root"][2] == "0"
    assert table(etc / "group")["staff"][2:] == ["1000", "alice"]
    assert table(etc / "shadow")["alice"][1] == "!"
    assert stat.S_IMODE((etc / "shadow").stat().st_mode) == 0o600
    assert stat.S_IMODE((sysroot / "home/alice").stat().st_mode) == 0o700
    assert read_ids(str(sysroot)).managed_users == ("alice",)

    entry = Path(boot.entry_path(str(sysroot), 1)).read_text()
    assert "linux /pyrixos/" in entry and "initrd /pyrixos/" in entry
    assert f"options root=/dev/vda2 rootfstype=ext4 init={path}/init" in entry
    assert "default pyrixos-1.conf" in (sysroot / boot.LOADER_CONF).read_text()
    images = {(sysroot / boot.IMAGES / i).read_bytes() for i in os.listdir(sysroot / boot.IMAGES)}
    assert images == {b"initrd image", b"kernel image"}
    assert os.readlink(sysroot / "run/current-system") == path

    assert manager.actions[:2] == ["chown 1000:100 " + str(sysroot / "home/alice"), "daemon-reload"]
    assert manager.actions[2:] == ["start web.service"]
    steps = [e.step for e in events if isinstance(e, ActivationStep)]
    assert steps == ["boot entry", "etc", "users", "file systems", "slots", "units", "current"]
    assert ProfileSwitched(profile="system", number=1, store_path=path) in events
    assert any(isinstance(e, ActivationNotice) and "/ is needed" in e.text for e in events)
    assert lattice.systems() == (
        SystemGeneration(
            number=1,
            store_path=path,
            name="host",
            current=True,
            boot_entry=boot.entry_path(str(sysroot), 1),
        ),
    )


def test_switch_again_and_roll_back(
    lattice: Lattice, manager: OfflineManager, events: list[Event]
) -> None:
    first = lattice.switch_system(FIRST)
    manager.actions.clear()
    second = lattice.switch_system(SECOND)
    sysroot = root(lattice)
    etc = sysroot / "etc"
    assert second.number == 2
    assert etc_text(lattice, "hostname") == "host2\n"
    assert etc_text(lattice, "motd") == "hello again\n"
    assert not os.path.lexists(etc / "app")
    assert "alice" not in table(etc / "passwd")
    assert (sysroot / "home/alice").is_dir()
    assert manager.actions == ["mount /data", "daemon-reload", "restart web.service"]
    assert "default pyrixos-2.conf" in (sysroot / boot.LOADER_CONF).read_text()

    manager.actions.clear()
    back = lattice.rollback_system()
    assert (back.number, back.store_path, back.current) == (1, first.store_path, True)
    assert "alice" in table(etc / "passwd")
    assert read_ids(str(sysroot)).users["alice"] == 1000
    assert any(
        "/data is no longer described" in e.text for e in events if isinstance(e, ActivationNotice)
    )
    generation = lattice.rollback("system", 2)
    assert (generation.number, generation.store_path) == (2, second.store_path)
    assert os.readlink(sysroot / "run/current-system") == second.store_path
    assert [s.current for s in lattice.systems()] == [False, True]

    deleted = lattice.delete_generations("system", 0)
    assert [g.number for g in deleted] == [1]
    assert sorted(boot.entries(str(sysroot))) == [2]


def test_boot_mode_waits_for_the_next_boot(lattice: Lattice, manager: OfflineManager) -> None:
    lattice.switch_system(FIRST)
    manager.actions.clear()
    result = lattice.switch_system(SECOND, "boot")
    sysroot = root(lattice)
    assert (result.number, result.current) == (2, True)
    assert etc_text(lattice, "hostname") == "host\n"
    assert os.readlink(sysroot / "run/current-system") != result.store_path
    assert "default pyrixos-2.conf" in (sysroot / boot.LOADER_CONF).read_text()
    assert manager.actions == []


def test_test_mode_has_no_generation(lattice: Lattice) -> None:
    result = lattice.switch_system(FIRST, "test")
    assert (result.number, result.current, result.boot_entry) == (0, False, None)
    assert os.readlink(root(lattice) / "run/current-system") == result.store_path
    assert lattice.generations("system") == ()
    assert {r.name for r in lattice.roots()} == {"system/test"}
    assert not (root(lattice) / boot.LOADER_CONF).exists()


def test_a_mounted_boot_file_system_needs_no_note(
    lattice: Lattice, events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os.path, "ismount", lambda path: path == str(root(lattice)))
    lattice.switch_system(FIRST)
    assert [e.text for e in events if isinstance(e, ActivationNotice)] == []


def test_system_without_kernel_has_no_boot_entry(lattice: Lattice) -> None:
    result = lattice.switch_system(host(kernel=None))
    assert result.boot_entry is None
    assert boot.entries(str(root(lattice))) == {}


def test_files_in_the_way_stop_activation(lattice: Lattice) -> None:
    etc = root(lattice) / "etc"
    etc.mkdir(parents=True)
    (etc / "motd").write_text("mine\n")
    with pytest.raises(ActivationFailed, match=r"step 'etc' failed: .*/etc/motd"):
        lattice.switch_system(FIRST)
    assert (etc / "motd").read_text() == "mine\n"
    assert not os.path.lexists(etc / "static")
    assert lattice.generations("system")[0].current is False


def test_accounts_keep_what_lattice_did_not_create(lattice: Lattice, tmp_path: Path) -> None:
    etc = root(lattice) / "etc"
    etc.mkdir(parents=True)
    (etc / "passwd").write_text("sysd:x:1000:1000:sysd:/:/bin/false\n")
    (etc / "group").write_text("sysd:x:1000:\n")
    secrets = root(lattice) / "secrets"
    secrets.mkdir()
    (secrets / "alice").write_text("$6$salt$hash\n")
    with_password = user("alice", password_file="/secrets/alice")
    lattice.switch_system(host(users=(with_password, user("bob", password_file="/secrets/none"))))
    passwd = table(etc / "passwd")
    assert passwd["sysd"][2] == "1000"
    assert (passwd["alice"][2], passwd["bob"][2]) == ("1001", "1002")
    assert table(etc / "group")["staff"][2] == "1001"
    shadow = table(etc / "shadow")
    assert (shadow["alice"][1], shadow["bob"][1]) == ("$6$salt$hash", "!")


def test_fixed_numbers_must_be_free(lattice: Lattice) -> None:
    etc = root(lattice) / "etc"
    etc.mkdir(parents=True)
    (etc / "passwd").write_text("sysd:x:1500:100::/:/bin/false\n")
    with pytest.raises(ActivationFailed, match="uid 1500 of 'alice' is already used by 'sysd'"):
        lattice.switch_system(host(users=(user("alice", uid=1500),)))


def test_activate_boot_with_a_system(lattice: Lattice) -> None:
    built = lattice.switch_system(FIRST, "boot")
    lattice.activate_boot(built.store_path)
    sysroot = root(lattice)
    assert os.readlink(sysroot / "run/booted-system") == built.store_path
    assert os.readlink(sysroot / "run/current-system") == built.store_path
    assert etc_text(lattice, "hostname") == "host\n"
    assert "alice" in table(sysroot / "etc/passwd")
    assert lattice.systems()[0].booted


def test_errors(lattice: Lattice) -> None:
    with pytest.raises(ActivationFailed, match="not a system"):
        lattice.switch_system(SH)
    with pytest.raises(StoreError, match="no earlier generation"):
        lattice.rollback_system()
    lattice.switch("system", SH)
    with pytest.raises(StoreError, match="does not hold a whole system"):
        lattice.rollback_system(1)
    assert lattice.systems() == ()
    with pytest.raises(ActivationFailed, match="is not a system"):
        lattice.activate_boot(SH.store_path)


def test_no_store(tmp_path: Path) -> None:
    settings = Settings.model_validate({"store": {"root": str(tmp_path / "none")}})
    lattice = Lattice(settings, system_manager=OfflineManager())
    assert lattice.systems() == ()
    with pytest.raises(StoreError, match="no store"):
        lattice.rollback_system()


SOURCE = """\
sh = derivation(name="sh", builder="/bin/sh", args=["sh"])
system = machine(
    name="host",
    packages=[sh],
    users=[user(name=config["user"], shell=f"{sh}/bin/sh")],
    host_name=config["host_name"],
)
"""


def test_evaluate_reads_config(lattice: Lattice, tmp_path: Path) -> None:
    description = tmp_path / "etc" / "system.pyb"
    description.parent.mkdir()
    description.write_text(SOURCE)
    assert read_config(description) is None
    with pytest.raises(EvaluationFailed):
        lattice.evaluate(description)
    (description.parent / CONFIG_FILE).write_text(
        'options = {"user": "carol", "host_name": "box"}\n'
    )
    drv = lattice.evaluate(description).derivations["system"]
    spec = system_spec(drv)
    assert (spec.host_name, spec.users[0].name) == ("box", "carol")
    assert spec.users[0].shell.endswith("-sh/bin/sh")
    assert spec.config == {"user": "carol", "host_name": "box"}
    (description.parent / CONFIG_FILE).write_text("options = 1\n")
    with pytest.raises(EvaluationFailed, match="options must be a dictionary"):
        lattice.evaluate(description)


def test_system_settings() -> None:
    assert Settings().system == SystemSettings()
    assert SystemSettings().description == "/etc/pyrixos/system.pyb"
    with pytest.raises(ValidationError, match="normalised absolute path"):
        Settings.model_validate({"system": {"root": "relative"}})


def test_cli(
    tmp_path: Path,
    lattice: Lattice,
    events: list[Event],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "make_api", lambda args: lattice)
    monkeypatch.setattr(cli, "Lattice", lambda settings, on_event: lattice)
    description = tmp_path / "system.pyb"
    description.write_text(SOURCE)
    (tmp_path / CONFIG_FILE).write_text('options = {"user": "carol", "host_name": "box"}\n')
    settings = tmp_path / "settings.toml"
    settings.write_text(f'[system]\ndescription = "{description}"\n')
    assert cli.main(["--settings", str(settings), "switch", "--test"]) == 0
    assert capsys.readouterr().out.startswith("0 host /lattice/store/")
    assert cli.main(["switch", str(description), "system"]) == 0
    assert capsys.readouterr().out.endswith("(current)\n")
    assert cli.main(["systems"]) == 0
    assert capsys.readouterr().out.startswith("1 host /lattice/store/")
    assert cli.main(["switch", "--boot", str(description), "sh"]) == 1
    assert "--boot and --test apply only to a system" in capsys.readouterr().err
    assert cli.main(["switch", "--profile", "per-user/a", str(description), "system"]) == 1
    assert "only be switched in the system profile" in capsys.readouterr().err
    system = lattice.systems()[0].store_path
    events.clear()
    assert cli.main(["activate-boot", "--system", system]) == 0
    assert [e.step for e in events if isinstance(e, ActivationStep)][-1] == "users"


def test_progress_prints_activation(capsys: pytest.CaptureFixture[str]) -> None:
    cli.progress(ActivationStep(step="etc", store_path=SH.store_path))
    cli.progress(ActivationNotice(text="later"))
    assert capsys.readouterr().err == "activating: etc\nnote: later\n"


ADMIN = Identity(name="root", role=Role.ADMIN, transport="unix")
ALICE = Identity(name="alice", role=Role.USER, transport="tls")


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("switch_system", SwitchSystemParams(derivation=FIRST, mode="test")),
        ("rollback_system", RollbackSystemParams(generation=None)),
        ("systems", NoParams()),
    ],
)
def test_system_methods_are_for_admins(method: str, params: BaseModel) -> None:
    authorize(ADMIN, method, params)
    with pytest.raises(PermissionDenied):
        authorize(ALICE, method, params)


@pytest.fixture
def daemon(
    tmp_path: Path, factory: Callable[[EventHandler | None], LatticeAPI]
) -> Iterator[Daemon]:
    settings = Settings.model_validate({"daemon": {"socket": str(tmp_path / "d.sock")}})
    with Daemon(settings, factory) as daemon:
        yield daemon


def test_client_system_methods(daemon: Daemon, tmp_path: Path) -> None:
    client = Client(f"unix://{daemon.socket_path}")
    assert client.switch_system(FIRST, "test").number == 0
    assert client.switch_system(FIRST).current
    assert client.rollback_system(4).number == 4
    assert [s.booted for s in client.systems()] == [True, False]
    description = tmp_path / "system.pyb"
    description.write_text("x = 1\n")
    (tmp_path / CONFIG_FILE).write_text("fail")
    with pytest.raises(EvaluationFailed, match="config.pyb"):
        client.evaluate(description)


def test_systemd_manager_runs_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "log"
    for name in ("systemctl", "mount"):
        program = tmp_path / name
        program.write_text(
            f'#!/bin/sh\necho "{name} $*" >> {log}\n'
            '[ "$3" != fail ] || { echo no >&2; exit 3; }\n'
        )
        program.chmod(0o755)
    monkeypatch.setattr(manager_module, "SEARCH_PATH", str(tmp_path))
    systemd = SystemdManager()
    systemd.daemon_reload()
    systemd.start(["a.service"])
    systemd.stop(["b.service"])
    systemd.restart(["c.service"])
    systemd.start([])
    systemd.mount("/data")
    assert log.read_text().splitlines() == [
        "systemctl daemon-reload",
        "systemctl start -- a.service",
        "systemctl stop -- b.service",
        "systemctl restart -- c.service",
        "mount -- /data",
    ]
    with pytest.raises(ActivationFailed, match="start -- fail: no"):
        systemd.start(["fail"])
    monkeypatch.setattr(manager_module, "SEARCH_PATH", str(tmp_path / "none"))
    with pytest.raises(ActivationFailed, match="systemctl was not found"):
        systemd.daemon_reload()
