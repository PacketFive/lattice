# SPDX-License-Identifier: GPL-2.0
"""Store layout, store path names and locks: RFC 0001 sections 10 and 12."""

import fcntl
import os
import stat
from pathlib import Path

import pytest

from lattice import StoreError
from lattice.store import StoreLayout, check_store_path, lock

HASH = "0123456789abcdefghijklmnopqrstuv"


@pytest.mark.parametrize(
    "path",
    [f"/lattice/store/{HASH}-hello", f"/lattice/store/{HASH}-a.b+c_d?e=f-2"],
)
def test_store_path_accepted(path: str) -> None:
    assert check_store_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        f"/lattice/store/{HASH}-hello/bin",
        f"/nix/store/{HASH}-hello",
        f"/lattice/store/{HASH[:-1]}-hello",
        f"/lattice/store/{HASH.upper()}-hello",
        f"/lattice/store/{HASH}-",
        f"/lattice/store/{HASH}-..",
        "/lattice/store/..",
        f"lattice/store/{HASH}-hello",
    ],
)
def test_store_path_refused(path: str) -> None:
    with pytest.raises(StoreError, match="not a store path"):
        check_store_path(path)


def test_layout(tmp_path: Path) -> None:
    layout = StoreLayout(root=str(tmp_path))
    assert not layout.exists()
    layout.create()
    assert stat.S_IMODE(os.stat(layout.store).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(layout.staging).st_mode) == 0o700
    assert layout.physical(f"/lattice/store/{HASH}-x") == f"{tmp_path}/store/{HASH}-x"
    assert layout.database == f"{tmp_path}/var/db.sqlite"
    layout.create()


def _try(path: str, operation: int) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def test_locks(tmp_path: Path) -> None:
    layout = StoreLayout(root=str(tmp_path))
    layout.create()
    with lock.shared(layout):
        assert _try(layout.lock, fcntl.LOCK_SH)
        assert not _try(layout.lock, fcntl.LOCK_EX)
    with lock.exclusive(layout):
        assert not _try(layout.lock, fcntl.LOCK_SH)
    assert _try(layout.lock, fcntl.LOCK_EX)
    with lock.derivation(layout, "abc"):
        assert not _try(f"{layout.staging}/abc.lock", fcntl.LOCK_EX)
