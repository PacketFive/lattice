# SPDX-License-Identifier: GPL-2.0
"""The lattice-tree-1 serialisation and tree comparison: RFC 0001 section 11."""

import hashlib
import os
from pathlib import Path

import pytest

from lattice import StoreError
from lattice.store import content_hash, diff_trees
from lattice.store.verify import FORMAT, serialise


def n(value: int) -> bytes:
    return value.to_bytes(8, "big")


def tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "run").write_bytes(b"#!x\n")
    (root / "bin" / "run").chmod(0o755)
    (root / "data").write_bytes(b"abc")
    (root / "link").symlink_to("data")
    return root


def test_serialisation(tmp_path: Path) -> None:
    root = tree(tmp_path / "t")
    expected = b"".join(
        [
            FORMAT,
            b"d" + n(3),
            n(3) + b"bin",
            b"d" + n(1),
            n(3) + b"run",
            b"fx" + n(4) + b"#!x\n",
            n(4) + b"data",
            b"f-" + n(3) + b"abc",
            n(4) + b"link",
            b"l" + n(4) + b"data",
        ]
    )
    assert b"".join(serialise(str(root))) == expected
    assert content_hash(str(root)) == (hashlib.sha256(expected).hexdigest(), len(expected))


def test_hash_ignores_times_owners_and_other_bits(tmp_path: Path) -> None:
    root = tree(tmp_path / "t")
    before = content_hash(str(root))
    os.utime(root / "data", (5, 5))
    (root / "data").chmod(0o600)
    (root / "bin" / "run").chmod(0o700)
    assert content_hash(str(root)) == before
    (root / "data").chmod(0o700)
    assert content_hash(str(root)) != before


def test_single_file_and_observe(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 100_000)
    pieces: list[bytes] = []
    digest, size = content_hash(str(path), pieces.append)
    assert b"".join(pieces) == FORMAT + b"f-" + n(100_000) + b"x" * 100_000
    assert size == len(b"".join(pieces))
    assert digest == hashlib.sha256(b"".join(pieces)).hexdigest()


def test_special_file_refused(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(StoreError, match="not a regular file"):
        content_hash(str(tmp_path))


def test_diff_trees(tmp_path: Path) -> None:
    first, second = tree(tmp_path / "a"), tree(tmp_path / "b")
    assert diff_trees(str(first), str(second)) == []
    (second / "data").write_bytes(b"abd")
    (second / "bin" / "run").chmod(0o644)
    (second / "link").unlink()
    (second / "link").symlink_to("other")
    (second / "extra").mkdir()
    (second / "extra" / "x").write_bytes(b"")
    assert diff_trees(str(first), str(second)) == ["bin/run", "data", "extra", "link"]
    assert diff_trees(str(first / "data"), str(second / "data")) == ["."]
