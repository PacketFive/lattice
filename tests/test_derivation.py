# SPDX-License-Identifier: GPL-2.0
"""Derivation hashing tests: RFC 0001 section 8."""

import json
import re
import warnings

import pytest
from pybonsai import FrozenMap
from pydantic import ValidationError

from lattice.derivation import (
    Derivation,
    canonical_json,
    derivation_hash,
    store_path,
)


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


def test_store_path_is_str() -> None:
    drv = make()
    assert str(drv) == drv.store_path == store_path(drv)
    assert drv.hash == derivation_hash(drv)


def test_dependency_is_hashed_by_its_hash() -> None:
    dep = make("dep-1")
    top = make(env=FrozenMap({"dep": dep}))
    assert json.loads(canonical_json(top))["env"]["dep"] == dep.hash


def test_lists_are_stored_as_tuples() -> None:
    fields = {"name": "a", "builder": "/bin/sh", "args": ["-c", "x"], "outputs": ["out", "dev"]}
    drv = Derivation.model_validate(fields)
    assert drv.args == ("-c", "x")
    assert drv.outputs == ("out", "dev")


def test_json_round_trip_keeps_nested_inputs() -> None:
    top = make(env=FrozenMap({"dep": make("dep-1"), "x": "1"}))
    data = top.model_dump_json()
    assert json.loads(data)["store_path"] == top.store_path
    back = Derivation.model_validate_json(data)
    assert back == top
    assert isinstance(back.env["dep"], Derivation)
    assert back.env["x"] == "1"


def test_json_round_trip_emits_no_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make(env=FrozenMap({"dep": make("dep-1")})).model_dump(mode="json")


def test_claimed_hash_must_match() -> None:
    data = json.loads(make().model_dump_json())
    data["hash"] = "0" * 64
    with pytest.raises(ValidationError, match="hash"):
        Derivation.model_validate(data)


def test_claimed_store_path_must_match() -> None:
    data = json.loads(make().model_dump_json())
    data["store_path"] = "/lattice/store/x-a-1"
    with pytest.raises(ValidationError, match="store_path"):
        Derivation.model_validate(data)


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a/b"},
        {"name": ".a"},
        {"name": ""},
        {"name": 1},
        {"builder": ""},
        {"args": [1]},
        {"args": "ab"},
        {"env": []},
        {"env": {"k": 1}},
        {"env": {"": "v"}},
        {"outputs": []},
        {"outputs": ["out", "out"]},
        {"outputs": ["a/b"]},
        {"extra": 1},
    ],
)
def test_invalid_fields_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Derivation.model_validate({"name": "a", "builder": "/bin/sh"} | fields)


def test_derivation_is_frozen() -> None:
    drv = make()
    with pytest.raises(ValidationError):
        drv.name = "b"  # type: ignore[misc]


def test_equal_derivations_hash_equal() -> None:
    assert make() == make()
    assert {make(), make()} == {make()}
    assert make("a-1") != make("a-2")
    assert make() != make().store_path
