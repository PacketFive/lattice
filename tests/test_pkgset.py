# SPDX-License-Identifier: GPL-2.0
"""Package sets of RFC 0005 section 5: layers, names and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice import EvaluationFailed, Lattice, Settings, SourceFile, SystemSettings, cli
from lattice.pkgset import evaluate_layers, read_package_set
from lattice.system.config import CONFIG_FILE

BASE = """
def tool(name, text="hi"):
    return derivation(name=name, builder="/bin/sh", args=("-c", text))

def broken(x):
    return x["missing"]

_secret = "kept in this layer"
sh = tool("sh", _secret)
"""

APPS = """
hello = tool("hello")
"""


def write_set(directory: Path, layers: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in layers.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def test_layers_are_read_in_byte_order(tmp_path: Path) -> None:
    pkgs = write_set(
        tmp_path / "pkgs",
        {"20-b.pyb": "", "10-a.pyb": "", "1Z.pyb": "", "README": "", "notes.txt": ""},
    )
    (pkgs / "15-dir.pyb").mkdir()
    layers = read_package_set(pkgs)
    assert [Path(layer.filename).name for layer in layers] == ["10-a.pyb", "1Z.pyb", "20-b.pyb"]
    with pytest.raises(EvaluationFailed, match="holds no .pyb layers"):
        read_package_set(write_set(tmp_path / "empty", {"README": ""}))
    with pytest.raises(FileNotFoundError):
        read_package_set(tmp_path / "missing")


def test_later_layers_see_earlier_names() -> None:
    names = evaluate_layers(
        [
            SourceFile(filename="10-base.pyb", source=BASE),
            SourceFile(filename="20.pyb", source=APPS),
        ]
    )
    assert set(names) == {"tool", "broken", "sh", "hello"}
    with pytest.raises(EvaluationFailed, match="name '_secret' is not defined"):
        evaluate_layers(
            [
                SourceFile(filename="a.pyb", source=BASE),
                SourceFile(filename="b.pyb", source="x = _secret"),
            ]
        )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("sh = 1", "b.pyb: sh is already defined in a.pyb"),
        ("def tool():\n    return 1", "b.pyb: tool is already defined in a.pyb"),
        ("derivation = 1", "b.pyb: derivation is a standard library name"),
        ("len = 1", "b.pyb: len is a standard library name"),
        ("config = 1", "b.pyb: config is a standard library name"),
    ],
)
def test_a_layer_cannot_repeat_a_name(source: str, message: str) -> None:
    layers = [
        SourceFile(filename="a.pyb", source=BASE),
        SourceFile(filename="b.pyb", source=source),
    ]
    with pytest.raises(EvaluationFailed, match=message):
        evaluate_layers(layers)


def test_private_names_may_repeat() -> None:
    names = evaluate_layers(
        [
            SourceFile(filename="a.pyb", source="_x = 1\na = _x"),
            SourceFile(filename="b.pyb", source="_x = 2\nb = _x + a"),
        ]
    )
    assert names == {"a": 1, "b": 3}


def test_errors_name_the_defining_layer(tmp_path: Path) -> None:
    pkgs = write_set(tmp_path / "pkgs", {"10-base.pyb": BASE, "20-apps.pyb": "x = broken({})\n"})
    with pytest.raises(EvaluationFailed, match=r"10-base\.pyb:6:"):
        read_and_evaluate(pkgs)


def read_and_evaluate(pkgs: Path) -> dict[str, object]:
    return evaluate_layers(read_package_set(pkgs))


def test_evaluate_with_a_package_set(tmp_path: Path) -> None:
    lattice = Lattice(Settings())
    pkgs = write_set(tmp_path / "pkgs", {"10-base.pyb": BASE, "20-apps.pyb": APPS})
    description = tmp_path / "use.pyb"
    description.write_text("mine = tool(config['name'])\nshell = sh\n")
    (tmp_path / CONFIG_FILE).write_text("options = {'name': 'me'}\n")
    evaluation = lattice.evaluate(description, pkgs)
    assert list(evaluation.derivations) == ["mine", "shell"]
    assert evaluation.derivations["mine"].name == "me"
    reference = lattice.evaluate_source(
        "x = tool('sh', 'kept in this layer')", "r.pyb", None, read_package_set(pkgs)
    )
    assert reference.derivations["x"] == evaluation.derivations["shell"]
    with pytest.raises(EvaluationFailed, match="name 'tool' is not defined"):
        lattice.evaluate(description)


def test_layers_are_evaluated_once_per_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = evaluate_layers

    def counting(layers: tuple[SourceFile, ...]) -> dict[str, object]:
        calls.append(len(layers))
        return real(layers)

    monkeypatch.setattr("lattice.api.evaluate_layers", counting)
    lattice = Lattice(Settings())
    first = (SourceFile(filename="a.pyb", source=BASE),)
    second = (SourceFile(filename="a.pyb", source=BASE + "\nmore = 1\n"),)
    lattice.evaluate_source("x = sh", "x.pyb", None, first)
    lattice.evaluate_source("y = sh", "y.pyb", None, first)
    lattice.evaluate_source("z = more", "z.pyb", None, second)
    assert calls == [1, 1]


def test_a_description_may_shadow_a_set_name(tmp_path: Path) -> None:
    pkgs = write_set(tmp_path / "pkgs", {"10-base.pyb": BASE})
    evaluation = Lattice(Settings()).evaluate_source(
        "sh = tool('other')", "d.pyb", None, read_package_set(pkgs)
    )
    assert evaluation.derivations["sh"].name == "other"


def test_package_set_setting() -> None:
    assert SystemSettings().package_set == "/etc/pyrixos/pkgs"
    with pytest.raises(ValueError, match="normalised absolute path"):
        Settings.model_validate({"system": {"package_set": "pkgs"}})


def test_cli_package_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pkgs = write_set(tmp_path / "pkgs", {"10-base.pyb": BASE, "20-apps.pyb": APPS})
    description = tmp_path / "use.pyb"
    description.write_text("app = hello\n")
    assert cli.main(["eval", "--package-set", str(pkgs), str(description)]) == 0
    assert "-hello " in capsys.readouterr().out
    assert cli.main(["eval", str(description)]) == 1
    assert "name 'hello' is not defined" in capsys.readouterr().err
    lattice = Lattice(Settings.model_validate({"store": {"root": str(tmp_path / "store")}}))
    monkeypatch.setattr(cli, "make_api", lambda args: lattice)
    assert cli.main(["plan", "--package-set", str(pkgs), str(description)]) == 0
    assert capsys.readouterr().out.startswith("build    ")


def test_switch_uses_the_package_set_of_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    def select(api: object, file: str, name: str | None, package_set: str | None = None) -> None:
        seen.append((file, name, package_set))
        raise EvaluationFailed("stop")

    monkeypatch.setattr(cli, "select", select)
    monkeypatch.setattr(cli, "make_api", lambda args: None)
    pkgs = tmp_path / "pkgs"
    settings = tmp_path / "settings.toml"
    settings.write_text(f'[system]\ndescription = "/d.pyb"\npackage_set = "{pkgs}"\n')
    base = ["--settings", str(settings), "switch"]
    assert cli.main(base) == 1
    pkgs.mkdir()
    assert cli.main(base) == 1
    assert cli.main([*base, "--package-set", "/other", "/e.pyb", "x"]) == 1
    assert seen == [
        ("/d.pyb", "system", None),
        ("/d.pyb", "system", str(pkgs)),
        ("/e.pyb", "x", "/other"),
    ]
