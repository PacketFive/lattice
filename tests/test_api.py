# SPDX-License-Identifier: GPL-2.0
"""Library interface tests: RFC 0001 section 6.1."""

import dataclasses
import logging
from pathlib import Path

import pytest

import lattice
from lattice import (
    Derivation,
    EvaluationFailed,
    EvaluationFinished,
    EvaluationStarted,
    Event,
    Lattice,
    LatticeError,
    Settings,
    SettingsError,
)
from lattice.events import emit

not_implemented = pytest.mark.xfail(raises=NotImplementedError, reason="not implemented yet")

SOURCE = "a = derivation(name='a', builder='/bin/sh')\nn = 1"


def test_all_names_are_exported() -> None:
    for name in lattice.__all__:
        assert hasattr(lattice, name), name


def test_package_is_typed() -> None:
    assert (Path(lattice.__file__).parent / "py.typed").is_file()


def test_settings_defaults() -> None:
    s = Settings()
    assert s.sandbox_backend == "namespaces"
    assert s.substituters == ()
    assert s.trusted_keys == ()
    assert s.require_provenance is False


def test_settings_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().sandbox_backend = "oci"  # type: ignore[misc]


@pytest.mark.parametrize("backend", ["namespaces", "oci", "microvm"])
def test_settings_accept_rfc_backends(backend: str) -> None:
    assert Settings(sandbox_backend=backend).sandbox_backend == backend  # type: ignore[arg-type]


def test_settings_reject_unknown_backend() -> None:
    with pytest.raises(SettingsError, match="unknown sandbox backend 'docker'"):
        Settings(sandbox_backend="docker")  # type: ignore[arg-type]


def test_settings_reject_list_of_substituters() -> None:
    with pytest.raises(SettingsError, match="substituters must be a tuple"):
        Settings(substituters=["https://cache"])  # type: ignore[arg-type]


def test_settings_error_is_lattice_error() -> None:
    assert issubclass(SettingsError, LatticeError)
    assert issubclass(EvaluationFailed, LatticeError)


def test_lattice_defaults_settings() -> None:
    assert Lattice().settings == Settings()


def test_emit_calls_handler_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Event] = []
    event = EvaluationStarted("x.pyb")
    with caplog.at_level(logging.DEBUG, logger="lattice"):
        emit(seen.append, event)
    assert seen == [event]
    assert "x.pyb" in caplog.text


def test_emit_without_handler_only_logs() -> None:
    emit(None, EvaluationStarted("x.pyb"))


@not_implemented
def test_evaluate_source_returns_derivations() -> None:
    events: list[Event] = []
    result = Lattice(on_event=events.append).evaluate_source(SOURCE, "t.pyb")
    assert list(result.derivations) == ["a"]
    assert isinstance(result.derivations["a"], Derivation)
    assert result.names["n"] == 1
    assert events[0] == EvaluationStarted("t.pyb")
    assert isinstance(events[-1], EvaluationFinished)
    assert events[-1].derivations == 1


@not_implemented
def test_evaluate_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "t.pyb"
    path.write_text(SOURCE, encoding="utf-8")
    result = Lattice().evaluate(path)
    assert result.filename == str(path)
    assert list(result.derivations) == ["a"]


@not_implemented
def test_evaluation_error_is_wrapped() -> None:
    with pytest.raises(EvaluationFailed) as info:
        Lattice().evaluate_source("import os", "t.pyb")
    assert info.value.__cause__ is info.value.error
    assert "t.pyb" in str(info.value)


@not_implemented
def test_settings_load() -> None:
    Settings.load("lattice.toml")
