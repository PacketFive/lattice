# SPDX-License-Identifier: GPL-2.0
"""Kernel module files and the Linux kernel interface: RFC 0001 section 13.3."""

from __future__ import annotations

import gzip
import lzma
import os
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from lattice import ActivationFailed
from lattice.kmod import LinuxModules, ModuleInfo, find_modules, load_order, read_info, syscalls
from lattice.kmod.modinfo import parse_info, section
from lattice.procscan import ProcessUse

MakeModule = Callable[..., bytes]


def info(name: str, *depends: str) -> ModuleInfo:
    return ModuleInfo(name=name, file=f"/m/{name}.ko", release="6.12.0", depends=depends)


@pytest.mark.parametrize("order", ["<", ">"])
def test_parse_info(make_module: MakeModule, order: str) -> None:
    image = make_module(
        {
            "vermagic": "6.12.0 SMP preempt mod_unload",
            "depends": "nvidia,drm-kms",
            "license": "GPL",
        },
        order=order,
    )
    parsed = parse_info("/m/nvidia-uvm.ko", image)
    assert parsed == ModuleInfo(
        name="nvidia_uvm",
        file="/m/nvidia-uvm.ko",
        release="6.12.0",
        depends=("nvidia", "drm_kms"),
    )
    named = parse_info("/m/x.ko.xz", make_module({"name": "real-name"}))
    assert (named.name, named.release, named.depends) == ("real_name", "", ())


@pytest.mark.parametrize(
    ("suffix", "compress"),
    [(".ko", lambda data: data), (".ko.xz", lzma.compress), (".ko.gz", gzip.compress)],
)
def test_read_info_compressed(
    tmp_path: Path, make_module: MakeModule, suffix: str, compress: Callable[[bytes], bytes]
) -> None:
    path = tmp_path / f"m{suffix}"
    path.write_bytes(compress(make_module({"vermagic": "6.1.0 SMP"})))
    assert read_info(str(path)).release == "6.1.0"


def test_bad_module_files(tmp_path: Path, make_module: MakeModule) -> None:
    good = make_module({"vermagic": "6.1.0"})
    with pytest.raises(ActivationFailed, match="not an ELF64"):
        section(b"\x7fELF" + bytes(60), ".modinfo")
    with pytest.raises(ActivationFailed, match="byte order"):
        section(good[:5] + b"\x03" + good[6:], ".modinfo")
    with pytest.raises(ActivationFailed, match="section table"):
        section(good[:-8], ".modinfo")
    (table,) = struct.unpack_from("<Q", good, 0x28)
    oversized = bytearray(good)
    struct.pack_into("<Q", oversized, table + 64 + 32, len(good))
    with pytest.raises(ActivationFailed, match="outside the file"):
        section(bytes(oversized), ".modinfo")
    with pytest.raises(ActivationFailed, match="no .modinfo"):
        parse_info("/m/x.ko", good.replace(b".modinfo", b".modinfx"))
    with pytest.raises(ActivationFailed, match="/m/x.ko: not an ELF64"):
        parse_info("/m/x.ko", b"plain text")
    broken = tmp_path / "broken.ko.xz"
    broken.write_bytes(b"not xz")
    with pytest.raises(ActivationFailed, match="cannot read"):
        read_info(str(broken))


def test_find_modules(tmp_path: Path) -> None:
    (tmp_path / "lib/modules/6.1/extra").mkdir(parents=True)
    for name in ("b.ko", "a.ko.xz", "c.ko.gz", "readme.txt"):
        (tmp_path / "lib/modules/6.1/extra" / name).write_bytes(b"")
    (tmp_path / "z.ko").write_bytes(b"")
    found = [os.path.relpath(path, tmp_path) for path in find_modules(str(tmp_path))]
    assert found == [
        "lib/modules/6.1/extra/a.ko.xz",
        "lib/modules/6.1/extra/b.ko",
        "lib/modules/6.1/extra/c.ko.gz",
        "z.ko",
    ]
    assert find_modules(str(tmp_path / "missing")) == ()


def test_load_order() -> None:
    modules = (info("uvm", "nvidia"), info("nvidia", "drm"), info("drm"), info("extra"))
    assert [m.name for m in load_order(modules)] == ["drm", "extra", "nvidia", "uvm"]
    with pytest.raises(ActivationFailed, match="are both drm"):
        load_order((info("drm"), info("drm")))
    with pytest.raises(ActivationFailed, match="depends on itself"):
        load_order((info("a", "b"), info("b", "a")))


def test_linux_loaded(tmp_path: Path) -> None:
    kernel = LinuxModules(str(tmp_path))
    assert kernel.loaded() == {}
    (tmp_path / "modules").write_text(
        "nvidia_uvm 1576960 0 - Live 0x0000000000000000 (POE)\n"
        "nvidia 54001664 3 nvidia_uvm,nvidia_modeset, Live 0x0000000000000000 (POE)\n"
        "broken line\n"
        "odd 1 x - Live 0x0\n",
        encoding="utf-8",
    )
    loaded = kernel.loaded()
    assert sorted(loaded) == ["nvidia", "nvidia_uvm"]
    assert loaded["nvidia"].refcount == 3
    assert loaded["nvidia"].holders == ("nvidia_uvm", "nvidia_modeset")
    assert loaded["nvidia_uvm"].holders == ()
    assert kernel.release() == os.uname().release


def test_linux_open_devices(tmp_path: Path) -> None:
    fd = tmp_path / "proc" / "42" / "fd"
    fd.mkdir(parents=True)
    os.symlink("/dev/nvidia0", fd / "3")
    os.symlink("/dev/null", fd / "4")
    kernel = LinuxModules(str(tmp_path / "proc"))
    assert kernel.open_devices(("/dev/nvidia*",)) == (ProcessUse(pid=42, path="/dev/nvidia0"),)
    assert kernel.open_devices(()) == ()


@pytest.mark.parametrize("name", ["x.ko", "x.ko.gz"])
def test_linux_load_refuses_bad_modules(tmp_path: Path, make_module: MakeModule, name: str) -> None:
    # Not a loadable module, so this fails whether or not the tests run as root.
    data = make_module({"vermagic": "0.0.0"})
    (tmp_path / name).write_bytes(gzip.compress(data) if name.endswith(".gz") else data)
    with pytest.raises(ActivationFailed, match="cannot load"):
        LinuxModules().load(str(tmp_path / name))


def test_linux_unload_missing_module() -> None:
    with pytest.raises(ActivationFailed, match="cannot unload lattice_test_absent"):
        LinuxModules().unload("lattice_test_absent")


def test_module_system_calls_are_known() -> None:
    if syscalls.machine() not in {"x86_64", "aarch64"}:
        pytest.skip("system call numbers are only listed for x86_64 and aarch64")
    with pytest.raises(OSError):
        syscalls.delete_module("lattice_test_absent")
