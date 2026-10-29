# SPDX-License-Identifier: GPL-2.0
"""Command line tests: the ``lattice`` entry point."""

import json
import sys
from pathlib import Path

import pytest
from pybonsai import FrozenMap

from lattice import cli
from lattice.client import DEFAULT_URL, Client
from lattice.derivation import Derivation
from lattice.settings import Settings
from lattice.transport import Daemon

SOURCE = (
    "a = derivation(name='a', builder='/bin/sh')\n"
    "b = derivation(name='b', builder='/bin/sh', env={'a': a})\n"
    "n = 1\n"
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_eval_prints_store_path_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, "t.pyb", SOURCE)
    assert cli.main(["eval", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    a = Derivation(name="a", builder="/bin/sh")
    assert [line.split(" ", 1)[0] for line in lines] == [
        a.store_path,
        Derivation(name="b", builder="/bin/sh", env=FrozenMap({"a": a})).store_path,
    ]
    assert json.loads(lines[1].split(" ", 1)[1])["env"] == {"a": a.hash}


def test_eval_error_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "t.pyb", "x = 1 / 0\n")
    assert cli.main(["eval", str(path)]) == 1
    assert "t.pyb:1" in capsys.readouterr().err


def test_eval_missing_file_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["eval", str(tmp_path / "none.pyb")]) == 1
    assert "none.pyb" in capsys.readouterr().err


def test_eval_reads_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "t.pyb", SOURCE)
    good = write(tmp_path, "good.toml", 'sandbox_backend = "oci"\n')
    bad = write(tmp_path, "bad.toml", "unknown = 1\n")
    assert cli.main(["--settings", str(good), "eval", str(path)]) == 0
    assert cli.main(["--settings", str(bad), "eval", str(path)]) == 1
    assert "bad.toml" in capsys.readouterr().err


def test_missing_default_settings_are_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS_PATH", str(tmp_path / "none.toml"))
    assert cli.load_settings(None) == Settings()


def test_existing_default_settings_are_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write(tmp_path, "settings.toml", 'sandbox_backend = "oci"\n')
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS_PATH", str(path))
    assert cli.load_settings(None).sandbox_backend == "oci"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--daemon", "--remote", "https://h", "eval", "t.pyb"],
        ["--ca", "ca.pem", "eval", "t.pyb"],
        ["--daemon", "--settings", "s.toml", "eval", "t.pyb"],
        ["--daemon", "daemon"],
        ["--remote", "https://h", "daemon"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2


def test_remote_client_takes_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.TOKEN_VARIABLE, "secret")
    args = cli.build_parser().parse_args(["--remote", "https://h:7443", "eval", "t.pyb"])
    api = cli.make_api(args)
    assert isinstance(api, Client)
    assert api.url == "https://h:7443"
    assert api._token == "secret"


def test_daemon_flag_uses_local_socket() -> None:
    api = cli.make_api(cli.build_parser().parse_args(["--daemon", "eval", "t.pyb"]))
    assert isinstance(api, Client)
    assert api.url == DEFAULT_URL


def test_unreachable_daemon_names_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "t.pyb", SOURCE)
    assert cli.main(["--remote", "https://127.0.0.1:1", "eval", str(path)]) == 1
    assert "https://127.0.0.1:1" in capsys.readouterr().err


def test_bad_remote_url_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "t.pyb", SOURCE)
    assert cli.main(["--remote", "http://h", "eval", str(path)]) == 1
    assert "https://" in capsys.readouterr().err


def test_daemon_command_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    served: list[object] = []
    monkeypatch.setattr(Daemon, "serve_forever", lambda self: served.append(self.settings))
    settings = write(tmp_path, "s.toml", 'sandbox_backend = "oci"\n')
    assert cli.main(["--settings", str(settings), "daemon"]) == 0
    assert served == [Settings(sandbox_backend="oci")]


def test_config_runs_trellis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("trellis")
    kconfig = write(tmp_path, "Kconfig", 'config NET\n\tbool "net"\n\tdefault y\n')
    out = tmp_path / "config.pyb"
    argv = ["config", "--kconfig", str(kconfig), "--config", str(out), "olddefconfig"]
    assert cli.main(argv) == 0
    assert '"NET": True' in out.read_text()
    with pytest.raises(SystemExit):
        cli.main(["--daemon", *argv])
    assert "always runs locally" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["eval", "--kconfig", "x"])
    assert "unrecognized arguments: --kconfig" in capsys.readouterr().err
    monkeypatch.setitem(sys.modules, "trellis.cli", None)
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "Trellis is not installed" in capsys.readouterr().err
