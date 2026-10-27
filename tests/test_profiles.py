# SPDX-License-Identifier: GPL-2.0
"""Profiles, generations, slots and activation: RFC 0001 section 13."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pybonsai import FrozenMap
from pydantic import BaseModel, ValidationError

from lattice import (
    ActivationDeferred,
    ActivationFailed,
    BuildFailed,
    Client,
    Derivation,
    Event,
    EventHandler,
    Identity,
    KernelModuleLoaded,
    Lattice,
    LatticeAPI,
    PermissionDenied,
    ProfileSwitched,
    Role,
    Settings,
    SlotChanged,
    SlotState,
    StoreError,
    cli,
)
from lattice.derivation import BUILTIN_PROFILE, BUILTIN_STACK
from lattice.procscan import ProcessUse
from lattice.profiles import check_profile_name, slots
from lattice.protocol import (
    DeleteGenerationsParams,
    NoParams,
    ProfileParams,
    RollbackParams,
    SwitchParams,
)
from lattice.server import authorize
from lattice.stdlib import profile, stack
from lattice.store import StoreLayout, database
from lattice.store.combine import (
    PROFILE_MANIFEST,
    STACK_MANIFEST,
    ProfileManifest,
    StackManifest,
    profile_manifest,
    read_manifest,
    stack_manifest,
)
from lattice.store.realise import realise
from lattice.transport import Daemon

if TYPE_CHECKING:
    from conftest import FakeKernel, StoreBackend

MakeModule = Callable[..., bytes]


def package(name: str) -> Derivation:
    return Derivation(name=name, builder="/bin/sh", args=(name,))


TOOLS = package("tools")
EDITOR = package("editor")
DRIVER_560 = package("driver-560")
DRIVER_570 = package("driver-570")
KMOD_560 = package("kmod-560")
KMOD_570 = package("kmod-570")
STACK_560 = stack("nvidia", "nvidia-560", (DRIVER_560,), (KMOD_560,), ("/dev/nvidia*",))
STACK_570 = stack("nvidia", "nvidia-570", (DRIVER_570,), (KMOD_570,), ("/dev/nvidia*",))
# The same modules as STACK_560 with other user-space files.
STACK_560_EDITOR = stack("nvidia", "nvidia-560-editor", (DRIVER_560, EDITOR), (KMOD_560,))
SYSTEM_560 = profile("system", (TOOLS,), FrozenMap({"nvidia": STACK_560}))
SYSTEM_570 = profile("system", (TOOLS,), FrozenMap({"nvidia": STACK_570}))
SYSTEM_560_EDITOR = profile("system", (TOOLS,), FrozenMap({"nvidia": STACK_560_EDITOR}))
SYSTEM_PLAIN = profile("system", (TOOLS,))


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def lattice(
    tmp_path: Path, store_backend: StoreBackend, kernel: FakeKernel, events: list[Event]
) -> Lattice:
    settings = Settings.model_validate(
        {"store": {"root": str(tmp_path / "root"), "stacks": str(tmp_path / "stacks")}}
    )
    return Lattice(settings, backend=store_backend, kernel_modules=kernel, on_event=events.append)


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


@pytest.fixture
def packages(lattice: Lattice, make_module: MakeModule) -> Lattice:
    """Register every package the profiles in this file use."""
    register(lattice, TOOLS, {"bin/tools": b"tools"})
    register(lattice, EDITOR, {"bin/editor": b"editor"})
    for version, driver, kmod in (("560", DRIVER_560, KMOD_560), ("570", DRIVER_570, KMOD_570)):
        register(lattice, driver, {f"lib/libcuda.so.{version}": version.encode()})
        directory = "lib/modules/6.12.0/extra"
        register(
            lattice,
            kmod,
            {
                f"{directory}/nvidia.ko": make_module(
                    {"version": version, "vermagic": "6.12.0 SMP"}
                ),
                f"{directory}/nvidia-uvm.ko": make_module(
                    {"version": version, "vermagic": "6.12.0 SMP", "depends": "nvidia"}
                ),
            },
        )
    return lattice


def slot_target(lattice: Lattice, slot: str = "nvidia") -> str | None:
    path = os.path.join(lattice.layout.stacks, slot)
    return os.readlink(path) if os.path.islink(path) else None


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("system", True),
        ("per-user/alice", True),
        ("per-user/alice/dev", True),
        ("a-1", True),
        ("a-1-linker", True),
        ("", False),
        ("a//b", False),
        ("../system", False),
        (".hidden", False),
        ("a/.b", False),
        ("system-2-link", False),
        ("a-0-link/b", False),
        ("a b", False),
    ],
)
def test_profile_names(name: str, valid: bool) -> None:
    if valid:
        assert check_profile_name(name) == name
    else:
        with pytest.raises(StoreError):
            check_profile_name(name)


def test_profiles_need_a_store(lattice: Lattice) -> None:
    assert lattice.generations("system") == ()
    assert lattice.slots() == ()
    assert lattice.delete_generations("system", 1) == ()
    with pytest.raises(StoreError, match="no store"):
        lattice.rollback("system")
    assert lattice.activate_boot() == ()
    assert not lattice.layout.exists()


def test_switch_rollback_and_delete(packages: Lattice, events: list[Event]) -> None:
    lattice = packages
    first = lattice.switch("per-user/alice", TOOLS)
    assert (first.number, first.store_path, first.current) == (1, TOOLS.store_path, True)
    second = lattice.switch("per-user/alice", EDITOR)
    assert (second.number, second.current) == (2, True)
    link = Path(lattice.layout.profiles) / "per-user" / "alice"
    assert os.readlink(link) == "alice-2-link"
    assert os.readlink(link.parent / "alice-1-link") == TOOLS.store_path
    assert [(g.number, g.current) for g in lattice.generations("per-user/alice")] == [
        (1, False),
        (2, True),
    ]
    assert {r.name: r.path for r in lattice.roots()} == {
        "profile/per-user/alice/1": TOOLS.store_path,
        "profile/per-user/alice/2": EDITOR.store_path,
    }
    switched = [e for e in events if isinstance(e, ProfileSwitched)]
    assert [(e.profile, e.number) for e in switched] == [
        ("per-user/alice", 1),
        ("per-user/alice", 2),
    ]

    assert lattice.rollback("per-user/alice").number == 1
    assert os.readlink(link) == "alice-1-link"
    with pytest.raises(StoreError, match="no earlier generation"):
        lattice.rollback("per-user/alice")
    assert lattice.rollback("per-user/alice", 2).store_path == EDITOR.store_path
    with pytest.raises(StoreError, match="has no generation 5"):
        lattice.rollback("per-user/alice", 5)

    third = lattice.switch("per-user/alice", TOOLS)
    assert third.number == 3
    lattice.rollback("per-user/alice", 2)
    with pytest.raises(StoreError, match="must not be negative"):
        lattice.delete_generations("per-user/alice", -1)
    deleted = lattice.delete_generations("per-user/alice", 1)
    assert [g.number for g in deleted] == [1]
    deleted = lattice.delete_generations("per-user/alice", 0)
    assert [g.number for g in deleted] == [3]
    assert [g.number for g in lattice.generations("per-user/alice")] == [2]
    assert [r.name for r in lattice.roots()] == ["profile/per-user/alice/2"]
    assert lattice.switch("per-user/alice", TOOLS).number == 3


def test_profile_placement(packages: Lattice) -> None:
    lattice = packages
    lattice.switch("a", TOOLS)
    with pytest.raises(StoreError, match="below the profile link"):
        lattice.switch("a/b", TOOLS)
    lattice.switch("x/y", TOOLS)
    with pytest.raises(StoreError, match="directory of other profiles"):
        lattice.switch("x", TOOLS)
    with pytest.raises(StoreError, match="not a valid profile name"):
        lattice.switch("x-1-link", TOOLS)


def test_only_system_assigns_stacks(packages: Lattice) -> None:
    with pytest.raises(ActivationFailed, match="only the system profile"):
        packages.switch("per-user/alice", SYSTEM_560)
    assert packages.generations("per-user/alice") == ()
    # A profile with no stacks may be used by any profile.
    assert packages.switch("per-user/alice", SYSTEM_PLAIN).number == 1


def test_profile_and_stack_outputs(packages: Lattice) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    environment = Path(lattice.layout.physical(SYSTEM_560.store_path))
    assert os.readlink(environment / "bin" / "tools") == f"{TOOLS.store_path}/bin/tools"
    manifest = read_manifest(lattice.layout, SYSTEM_560.store_path, ProfileManifest)
    assert manifest == ProfileManifest(
        name="system", packages=(TOOLS.store_path,), stacks={"nvidia": STACK_560.store_path}
    )
    stack_dir = Path(lattice.layout.physical(STACK_560.store_path))
    assert (stack_dir / STACK_MANIFEST).is_file()
    assert os.readlink(stack_dir / "lib" / "libcuda.so.560") == (
        f"{DRIVER_560.store_path}/lib/libcuda.so.560"
    )
    stack_info = read_manifest(lattice.layout, STACK_560.store_path, StackManifest)
    assert stack_info is not None
    assert stack_info.devices == ("/dev/nvidia*",)
    assert read_manifest(lattice.layout, TOOLS.store_path, StackManifest) is None
    assert (environment / PROFILE_MANIFEST).is_file()


def test_conflicting_packages(packages: Lattice) -> None:
    clash = Derivation(name="clash", builder="/bin/sh", args=("clash",))
    register(packages, clash, {"bin/tools": b"other"})
    with pytest.raises(BuildFailed, match="bin/tools"):
        packages.switch("per-user/alice", profile("clash", (TOOLS, clash)))
    single = Derivation(name="single", builder="/bin/sh", args=("single",))
    connection = database.connect(packages.layout)
    try:
        work = Path(packages.layout.staging) / "single"
        work.write_bytes(b"one file")
        realise(packages.layout, connection, single, {"out": str(work)}, [])
    finally:
        connection.close()
    with pytest.raises(BuildFailed, match="is not a directory"):
        packages.build(profile("single", (single,)))


def test_invalid_manifest(packages: Lattice) -> None:
    broken = Derivation(name="broken", builder="/bin/sh", args=("broken",))
    register(packages, broken, {STACK_MANIFEST: b"{}"})
    with pytest.raises(StoreError, match="is not valid"):
        read_manifest(packages.layout, broken.store_path, StackManifest)


@pytest.mark.parametrize(
    ("drv", "message"),
    [
        (Derivation(name="s", builder=BUILTIN_STACK, env=FrozenMap({"slot": "../x"})), "slot"),
        (Derivation(name="s", builder=BUILTIN_STACK, env=FrozenMap({"other": "x"})), "other"),
        (
            Derivation(
                name="s", builder=BUILTIN_STACK, env=FrozenMap({"slot": "a", "content_1": TOOLS})
            ),
            "numbered from 0",
        ),
        (
            Derivation(
                name="s", builder=BUILTIN_STACK, env=FrozenMap({"slot": "a", "content_0": "x"})
            ),
            "must be a derivation",
        ),
        (
            Derivation(name="s", builder=BUILTIN_STACK, env=FrozenMap({"device_0": "/dev/x"})),
            "slot name",
        ),
    ],
)
def test_bad_stack_derivations(drv: Derivation, message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        stack_manifest(drv)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"stack_nvidia": TOOLS}, "must be a stack"),
        ({"stack_other": STACK_560}, "another slot"),
        (
            {
                "stack_.x": Derivation(
                    name="s", builder=BUILTIN_STACK, env=FrozenMap({"slot": ".x"})
                )
            },
            "slot",
        ),
    ],
)
def test_bad_profile_derivations(env: dict[str, object], message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        profile_manifest(Derivation(name="p", builder=BUILTIN_PROFILE, env=FrozenMap(env)))


def test_system_switch_changes_modules(
    packages: Lattice, kernel: FakeKernel, events: list[Event]
) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    assert kernel.log == ["load nvidia", "load nvidia_uvm"]
    assert slot_target(lattice) == STACK_560.store_path
    assert lattice.slots() == (
        SlotState(slot="nvidia", active=STACK_560.store_path, wanted=STACK_560.store_path),
    )
    kernel.log.clear()
    lattice.switch("system", SYSTEM_570)
    assert kernel.log == ["unload nvidia_uvm", "unload nvidia", "load nvidia", "load nvidia_uvm"]
    assert slot_target(lattice) == STACK_570.store_path
    loaded = [e.file for e in events if isinstance(e, KernelModuleLoaded)]
    assert KMOD_570.store_path.rpartition("/")[2] in loaded[-1]
    changed = [e for e in events if isinstance(e, SlotChanged)]
    assert [(e.old, e.new) for e in changed] == [
        (None, STACK_560.store_path),
        (STACK_560.store_path, STACK_570.store_path),
    ]
    kernel.log.clear()
    lattice.switch("system", SYSTEM_PLAIN)
    assert kernel.log == ["unload nvidia_uvm", "unload nvidia"]
    assert slot_target(lattice) is None
    assert lattice.slots() == ()
    kernel.log.clear()
    lattice.rollback("system", 2)
    assert kernel.log == ["load nvidia", "load nvidia_uvm"]
    assert slot_target(lattice) == STACK_570.store_path


def test_same_modules_are_not_reloaded(packages: Lattice, kernel: FakeKernel) -> None:
    packages.switch("system", SYSTEM_560)
    kernel.log.clear()
    packages.switch("system", SYSTEM_560_EDITOR)
    assert kernel.log == []
    assert slot_target(packages) == STACK_560_EDITOR.store_path


@pytest.mark.parametrize("blocker", ["device", "in use", "kernel"])
def test_change_is_deferred(
    packages: Lattice, kernel: FakeKernel, events: list[Event], blocker: str
) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    if blocker == "device":
        kernel.devices.append(ProcessUse(pid=7, path="/dev/nvidia0"))
        reason = "process 7 has /dev/nvidia0 open"
    elif blocker == "in use":
        used = kernel.modules["nvidia_uvm"]
        kernel.modules["nvidia_uvm"] = used.model_copy(update={"refcount": 1})
        reason = "module nvidia_uvm is in use"
    else:
        kernel.kernel_release = "6.13.0"
        reason = "built for kernel 6.12.0, not 6.13.0"
    kernel.log.clear()
    generation = lattice.switch("system", SYSTEM_570)
    assert generation.current
    assert kernel.log == []
    assert slot_target(lattice) == STACK_560.store_path
    deferred = [e for e in events if isinstance(e, ActivationDeferred)]
    assert deferred[0].slot == "nvidia"
    assert reason in deferred[0].reason
    assert lattice.slots() == (
        SlotState(slot="nvidia", active=STACK_560.store_path, wanted=STACK_570.store_path),
    )


def test_boot_after_deferral(packages: Lattice, kernel: FakeKernel) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    kernel.devices.append(ProcessUse(pid=7, path="/dev/nvidia0"))
    lattice.switch("system", SYSTEM_570)
    # Reboot: no modules loaded and no slots.
    kernel.modules.clear()
    kernel.devices.clear()
    kernel.log.clear()
    shutil.rmtree(lattice.layout.stacks)
    states = lattice.activate_boot()
    assert kernel.log == ["load nvidia", "load nvidia_uvm"]
    assert states == (
        SlotState(slot="nvidia", active=STACK_570.store_path, wanted=STACK_570.store_path),
    )
    kernel.log.clear()
    assert lattice.activate_boot() == states
    assert kernel.log == []


def test_boot_reports_failures(packages: Lattice, kernel: FakeKernel) -> None:
    packages.switch("system", SYSTEM_560)
    kernel.modules.clear()
    kernel.failing.add(KMOD_560.store_path.rpartition("/")[2])
    with pytest.raises(ActivationFailed, match="slot nvidia: cannot load"):
        packages.activate_boot()
    assert slot_target(packages) == STACK_560.store_path


def test_failed_load_is_undone(packages: Lattice, kernel: FakeKernel) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    kernel.failing.add(
        f"{KMOD_570.store_path.rpartition('/')[2]}/lib/modules/6.12.0/extra/nvidia-uvm"
    )
    kernel.log.clear()
    with pytest.raises(ActivationFailed, match="cannot load"):
        lattice.switch("system", SYSTEM_570)
    assert kernel.log == [
        "unload nvidia_uvm",
        "unload nvidia",
        "load nvidia",
        "unload nvidia",
        "load nvidia",
        "load nvidia_uvm",
    ]
    assert slot_target(lattice) == STACK_560.store_path
    assert [(g.number, g.current) for g in lattice.generations("system")] == [(1, True), (2, False)]


def test_delete_keeping_more_than_exist(packages: Lattice) -> None:
    packages.switch("per-user/alice", TOOLS)
    packages.switch("per-user/alice", EDITOR)
    packages.rollback("per-user/alice")
    assert packages.delete_generations("per-user/alice", 5) == ()
    assert [g.number for g in packages.generations("per-user/alice")] == [1, 2]


def two_slot_system(slot: str, kmod: Derivation, driver: Derivation) -> Derivation:
    moved = stack(slot, f"{slot}-stack", (driver,), (kmod,), ("/dev/nvidia*",))
    return profile("system", (TOOLS,), FrozenMap({slot: moved}))


def test_modules_moving_between_slots(packages: Lattice, kernel: FakeKernel) -> None:
    lattice = packages
    lattice.switch("system", two_slot_system("z-gpu", KMOD_560, DRIVER_560))
    kernel.log.clear()
    lattice.switch("system", two_slot_system("a-gpu", KMOD_560, DRIVER_560))
    assert kernel.log == []
    assert set(kernel.modules) == {"nvidia", "nvidia_uvm"}
    assert slot_target(lattice, "z-gpu") is None
    assert slot_target(lattice, "a-gpu") is not None
    lattice.switch("system", two_slot_system("z-gpu", KMOD_570, DRIVER_570))
    assert kernel.log == ["unload nvidia_uvm", "unload nvidia", "load nvidia", "load nvidia_uvm"]
    assert set(kernel.modules) == {"nvidia", "nvidia_uvm"}


def test_failed_slot_change_is_undone(
    packages: Lattice, kernel: FakeKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    extra = stack("zz", "zz-stack", (EDITOR,), (), ())
    system = profile("system", (TOOLS,), FrozenMap({"nvidia": STACK_570, "zz": extra}))
    real = slots.set_slot

    def set_slot(layout: StoreLayout, slot: str, target: str | None) -> None:
        if slot == "zz" and target is not None:
            raise StoreError("cannot set slot zz: read-only file system")
        real(layout, slot, target)

    monkeypatch.setattr("lattice.profiles.activation.set_slot", set_slot)
    kernel.log.clear()
    with pytest.raises(ActivationFailed, match="cannot set slot zz"):
        lattice.switch("system", system)
    assert slot_target(lattice) == STACK_560.store_path
    assert slot_target(lattice, "zz") is None
    assert kernel.log[-2:] == ["load nvidia", "load nvidia_uvm"]
    assert [(g.number, g.current) for g in lattice.generations("system")] == [(1, True), (2, False)]


def test_slot_removal_error(packages: Lattice, monkeypatch: pytest.MonkeyPatch) -> None:
    slots.set_slot(packages.layout, "nvidia", STACK_560.store_path)

    def unlink(path: str) -> None:
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(StoreError, match="cannot remove slot nvidia"):
        slots.set_slot(packages.layout, "nvidia", None)


def test_failed_undo_is_reported(packages: Lattice, kernel: FakeKernel) -> None:
    packages.switch("system", SYSTEM_560)
    kernel.failing.add("/lib/modules/6.12.0/extra/nvidia-uvm")
    with pytest.raises(ActivationFailed, match="undoing also failed"):
        packages.switch("system", SYSTEM_570)


def test_collection_keeps_active_stacks(packages: Lattice, kernel: FakeKernel) -> None:
    lattice = packages
    lattice.switch("system", SYSTEM_560)
    kernel.devices.append(ProcessUse(pid=7, path="/dev/nvidia0"))
    lattice.switch("system", SYSTEM_570)
    assert [g.number for g in lattice.delete_generations("system", 0)] == [1]
    removed = lattice.gc().removed
    assert STACK_560.store_path not in removed
    assert KMOD_560.store_path not in removed
    assert SYSTEM_560.store_path in removed
    assert os.path.isdir(lattice.layout.physical(STACK_560.store_path))


def test_cli(
    tmp_path: Path,
    packages: Lattice,
    kernel: FakeKernel,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lattice = packages
    monkeypatch.setattr(cli, "make_api", lambda args: lattice)
    monkeypatch.setattr(cli, "Lattice", lambda settings, on_event: lattice)
    source = tmp_path / "system.pyb"
    source.write_text(
        "tools = derivation(name='tools', builder='/bin/sh', args=['tools'])\n"
        "system = profile(name='system', packages=[tools])\n",
        encoding="utf-8",
    )
    assert cli.main(["switch", str(source), "system", "--profile", "per-user/alice"]) == 0
    assert capsys.readouterr().out == f"1 {SYSTEM_PLAIN.store_path} (current)\n"
    assert cli.main(["switch", "--profile", "per-user/alice", str(source), "tools"]) == 0
    capsys.readouterr()
    assert cli.main(["generations", "--profile", "per-user/alice"]) == 0
    assert capsys.readouterr().out == (
        f"1 {SYSTEM_PLAIN.store_path}\n2 {TOOLS.store_path} (current)\n"
    )
    assert cli.main(["rollback", "--profile", "per-user/alice"]) == 0
    assert capsys.readouterr().out == f"1 {SYSTEM_PLAIN.store_path} (current)\n"
    assert cli.main(["rollback", "--profile", "per-user/alice", "2"]) == 0
    capsys.readouterr()
    assert cli.main(["delete-generations", "--profile", "per-user/alice", "--keep", "0"]) == 0
    assert capsys.readouterr().out == f"1 {SYSTEM_PLAIN.store_path}\n"
    assert cli.main(["rollback", "--profile", "per-user/alice"]) == 1
    assert "no earlier generation" in capsys.readouterr().err

    lattice.on_event = cli.progress
    assert cli.main(["slots"]) == 0
    assert capsys.readouterr().out == ""
    lattice.switch("system", SYSTEM_560)
    assert "slot nvidia: /lattice/store/" in capsys.readouterr().err
    assert cli.main(["slots"]) == 0
    assert capsys.readouterr().out == f"nvidia: active {STACK_560.store_path}\n"
    kernel.devices.append(ProcessUse(pid=7, path="/dev/nvidia0"))
    lattice.switch("system", SYSTEM_PLAIN)
    assert "slot nvidia changes at the next boot: process 7" in capsys.readouterr().err
    assert cli.main(["slots"]) == 0
    assert capsys.readouterr().out == f"nvidia: active {STACK_560.store_path}, next boot none\n"
    assert cli.main(["activate-boot"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "slot nvidia: removed" in captured.err
    with pytest.raises(SystemExit):
        cli.main(["--daemon", "activate-boot"])


ALICE = Identity(name="alice", role=Role.USER, transport="tls")
ADMIN = Identity(name="root", role=Role.ADMIN, transport="unix")


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("switch", SwitchParams(profile="system", derivation=TOOLS)),
        ("rollback", RollbackParams(profile="system")),
        ("delete_generations", DeleteGenerationsParams(profile="system", keep=1)),
    ],
)
def test_profile_permissions(method: str, params: BaseModel) -> None:
    authorize(ADMIN, method, params)
    with pytest.raises(PermissionDenied, match="may not change profile 'system'"):
        authorize(ALICE, method, params)
    own = params.model_copy(update={"profile": "per-user/alice/dev"})
    authorize(ALICE, method, own)
    authorize(ALICE, "generations", ProfileParams(profile="system"))
    authorize(ALICE, "slots", NoParams())


@pytest.fixture
def daemon(
    tmp_path: Path, factory: Callable[[EventHandler | None], LatticeAPI]
) -> Iterator[Daemon]:
    settings = Settings.model_validate({"daemon": {"socket": str(tmp_path / "d.sock")}})
    with Daemon(settings, factory) as daemon:
        yield daemon


def test_client_profile_methods(daemon: Daemon) -> None:
    seen: list[Event] = []
    client = Client(f"unix://{daemon.socket_path}", on_event=seen.append)
    generation = client.switch("system", TOOLS)
    assert (generation.number, generation.store_path, generation.current) == (
        2,
        TOOLS.store_path,
        True,
    )
    assert seen == [ProfileSwitched(profile="system", number=2, store_path=TOOLS.store_path)]
    assert client.rollback("system").number == 1
    assert client.rollback("system", 4).number == 4
    assert [g.current for g in client.generations("system")] == [False, True]
    assert [g.number for g in client.delete_generations("system", 0)] == [1]
    assert client.delete_generations("system", 3) == ()
    assert client.slots() == (
        SlotState(slot="nvidia", active="/lattice/store/a-s", wanted="/lattice/store/b-s"),
    )
    with pytest.raises(ValidationError):
        client.delete_generations("system", -1)


@pytest.mark.parametrize("stacks", ["run/stacks", "/run//stacks", "/run/stacks/"])
def test_stacks_setting_must_be_normalised(stacks: str) -> None:
    with pytest.raises(ValidationError, match="store stacks must be"):
        Settings.model_validate({"store": {"stacks": stacks}})
    assert Settings().store.stacks == "/run/lattice/stacks"
