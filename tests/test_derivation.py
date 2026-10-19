# SPDX-License-Identifier: GPL-2.0
"""Derivation hashing tests: RFC 0001 section 8."""

import json
import re

import pytest
from pybonsai import FrozenMap

from lattice.derivation import (
    Derivation,
    canonical_json,
    derivation_hash,
    store_path,
)

pytestmark = pytest.mark.xfail(raises=NotImplementedError, reason="not implemented yet")


def make(name: str = "a-1", env: FrozenMap | None = None) -> Derivation:
    return Derivation(
        name=name,
        builder="/bin/sh",
        args=("-c", "true"),
        env=env if env is not None else FrozenMap(),
        outputs=("out",),
    )


def test_canonical_json_is_sorted_and_compact() -> None:
    data = canonical_json(make())
    assert b" " not in data.replace(b"/bin/sh", b"")
    parsed = json.loads(data)
    assert list(parsed) == sorted(parsed)


def test_same_fields_same_hash() -> None:
    assert derivation_hash(make()) == derivation_hash(make())


def test_hash_is_hex_sha256() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", derivation_hash(make()))


def test_dependency_change_changes_hash() -> None:
    dep_a = make("dep-1")
    dep_b = make("dep-2")
    top_a = make(env=FrozenMap({"dep": dep_a}))
    top_b = make(env=FrozenMap({"dep": dep_b}))
    assert derivation_hash(top_a) != derivation_hash(top_b)


def test_env_order_does_not_change_hash() -> None:
    one = make(env=FrozenMap({"a": "1", "b": "2"}))
    two = make(env=FrozenMap({"b": "2", "a": "1"}))
    assert derivation_hash(one) == derivation_hash(two)


def test_store_path_format() -> None:
    assert re.fullmatch(r"/lattice/store/[a-z2-7]{32}-a-1", store_path(make()))
