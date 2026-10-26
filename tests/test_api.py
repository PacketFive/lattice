# SPDX-License-Identifier: GPL-2.0
"""Library interface tests: RFC 0001 section 6.1."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

import lattice
from lattice import (
    Derivation,
    EvaluationFailed,
    EvaluationFinished,
    EvaluationStarted,
    Event,
    Lattice,
    LatticeAPI,
    LatticeError,
    Settings,
    SettingsError,
)
from lattice.events import emit
from lattice.settings import ClientGrant, DaemonSettings, TokenGrant

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
    with pytest.raises(ValidationError):
        Settings().sandbox_backend = "oci"  # type: ignore[misc]


@pytest.mark.parametrize("backend", ["namespaces", "oci", "microvm"])
def test_settings_accept_rfc_backends(backend: str) -> None:
    assert Settings(sandbox_backend=backend).sandbox_backend == backend  # type: ignore[arg-type]


def test_settings_reject_unknown_backend() -> None:
    with pytest.raises(SettingsError, match="sandbox_backend"):
        Settings(sandbox_backend="docker")  # type: ignore[arg-type]


def test_settings_reject_list_of_substituters() -> None:
    with pytest.raises(SettingsError, match="substituters"):
        Settings(substituters=["https://cache"])  # type: ignore[arg-type]


def test_settings_reject_unknown_field() -> None:
    with pytest.raises(SettingsError, match="backend"):
        Settings(backend="oci")  # type: ignore[call-arg]


def test_remote_api_needs_tls() -> None:
    with pytest.raises(SettingsError, match="TLS only"):
        DaemonSettings(listen="0.0.0.0:7420")


def test_remote_api_needs_a_credential() -> None:
    with pytest.raises(SettingsError, match="clients or tokens"):
        DaemonSettings(listen="[::1]:7420", tls_cert="c.pem", tls_key="k.pem")


def test_clients_need_a_ca() -> None:
    with pytest.raises(SettingsError, match="tls_client_ca"):
        DaemonSettings(clients=(ClientGrant(common_name="ci"),))


def test_token_grant_stores_a_digest() -> None:
    with pytest.raises(SettingsError, match="sha256"):
        TokenGrant(name="ci", sha256="not-a-digest")


def test_settings_error_is_lattice_error() -> None:
    assert issubclass(SettingsError, LatticeError)
    assert issubclass(EvaluationFailed, LatticeError)


def test_lattice_defaults_settings() -> None:
    assert Lattice().settings == Settings()


def test_emit_calls_handler_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Event] = []
    event = EvaluationStarted(filename="x.pyb")
    with caplog.at_level(logging.DEBUG, logger="lattice"):
        emit(seen.append, event)
    assert seen == [event]
    assert "x.pyb" in caplog.text


def test_emit_without_handler_only_logs() -> None:
    emit(None, EvaluationStarted(filename="x.pyb"))


def test_lattice_satisfies_api_protocol() -> None:
    api: LatticeAPI = Lattice()
    assert isinstance(api, LatticeAPI)


def test_evaluate_source_returns_derivations() -> None:
    events: list[Event] = []
    result = Lattice(on_event=events.append).evaluate_source(SOURCE, "t.pyb")
    assert list(result.derivations) == ["a"]
    assert isinstance(result.derivations["a"], Derivation)
    assert events[0] == EvaluationStarted(filename="t.pyb")
    assert isinstance(events[-1], EvaluationFinished)
    assert events[-1].derivations == 1


def test_evaluate_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "t.pyb"
    path.write_text(SOURCE, encoding="utf-8")
    result = Lattice().evaluate(path)
    assert result.filename == str(path)
    assert list(result.derivations) == ["a"]


def test_evaluation_error_is_wrapped() -> None:
    with pytest.raises(EvaluationFailed) as info:
        Lattice().evaluate_source("import os", "t.pyb")
    assert info.value.__cause__ is info.value.error
    assert "t.pyb" in str(info.value)


SETTINGS_TOML = """
sandbox_backend = "oci"
substituters = ["https://cache.example.org"]

[daemon]
listen = "0.0.0.0:7443"
tls_cert = "/etc/lattice/cert.pem"
tls_key = "/etc/lattice/key.pem"

[[daemon.tokens]]
name = "ci"
sha256 = "%s"
role = "admin"
""" % ("a" * 64)


def test_settings_load(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(SETTINGS_TOML, encoding="utf-8")
    settings = Settings.load(path)
    assert settings.sandbox_backend == "oci"
    assert settings.substituters == ("https://cache.example.org",)
    assert settings.daemon.listen == "0.0.0.0:7443"
    assert settings.daemon.tokens[0].name == "ci"


def test_settings_load_empty_file_is_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("", encoding="utf-8")
    assert Settings.load(path) == Settings()


@pytest.mark.parametrize(
    "text",
    [
        "unknown = 1",
        "sandbox_backend = 3",
        "when = 1979-05-27",
        "= not toml",
        '[daemon]\nlisten = "0.0.0.0:7443"',
        '[[daemon.tokens]]\nname = "a"\nsha256 = "x"',
    ],
)
def test_settings_load_rejects_bad_files(tmp_path: Path, text: str) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match="settings.toml"):
        Settings.load(path)


def test_settings_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="missing.toml"):
        Settings.load(tmp_path / "missing.toml")
