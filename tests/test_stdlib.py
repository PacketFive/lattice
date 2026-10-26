# SPDX-License-Identifier: GPL-2.0
"""Standard library tests: RFC 0001 sections 7.5, 7.6 and 7.8."""

import pytest
from pybonsai import EvaluationError, RejectedSyntax, evaluate_source

from lattice.derivation import BUILTIN_FETCH_URL, Derivation
from lattice.stdlib import build_host

SHA = "0" * 64


def test_host_exposes_rfc_names() -> None:
    names = build_host().names
    for name in ("derivation", "fetch_url", "stack", "profile", "len", "range"):
        assert name in names


def test_rfc_example_produces_derivations() -> None:
    source = (
        "src = fetch_url(url='https://ftp.gnu.org/gnu/hello/hello-2.12.1.tar.gz',\n"
        "                sha256='0' * 64)\n"
        "hello = derivation(name='hello-2.12.1', builder='/bin/sh',\n"
        "                   args=['-c', 'make install'], env={'src': src},\n"
        "                   outputs=['out'])"
    )
    result = evaluate_source(source, host=build_host())
    hello = result["hello"]
    assert isinstance(hello, Derivation)
    assert hello.args == ("-c", "make install")
    assert hello.env["src"] is result["src"]


def test_derivation_attributes_are_readable() -> None:
    source = "d = derivation(name='a', builder='/bin/sh')\nn = d.name"
    assert evaluate_source(source, host=build_host())["n"] == "a"


def test_unlisted_derivation_attribute_is_rejected() -> None:
    source = "d = derivation(name='a', builder='/bin/sh')\np = d.store_path"
    with pytest.raises(RejectedSyntax):
        evaluate_source(source, host=build_host())


def test_derivation_interpolates_as_store_path() -> None:
    source = "d = derivation(name='a', builder='/bin/sh')\np = f'{d}/bin'"
    result = evaluate_source(source, host=build_host())
    d = result["d"]
    assert isinstance(d, Derivation)
    assert result["p"] == d.store_path + "/bin"


def test_fetch_url_declares_fixed_output() -> None:
    source = f"s = fetch_url('https://example.org/dl/hello-1.tar.gz', '{SHA}')"
    s = evaluate_source(source, host=build_host())["s"]
    assert isinstance(s, Derivation)
    assert s.name == "hello-1.tar.gz"
    assert s.builder == BUILTIN_FETCH_URL
    assert dict(s.env) == {"url": "https://example.org/dl/hello-1.tar.gz", "sha256": SHA}


@pytest.mark.parametrize(
    ("url", "sha256"),
    [
        ("file:///etc/passwd", SHA),
        ("ftp://example.org/a.tar", SHA),
        ("https:///a.tar", SHA),
        ("https://example.org/", SHA),
        ("https://example.org/a.tar", "0" * 63),
        ("https://example.org/a.tar", "A" * 64),
    ],
)
def test_fetch_url_rejects_bad_input(url: str, sha256: str) -> None:
    source = f"s = fetch_url({url!r}, {sha256!r})"
    with pytest.raises(EvaluationError):
        evaluate_source(source, host=build_host())


@pytest.mark.parametrize(
    "call",
    [
        "derivation(name='a/b', builder='/bin/sh')",
        "derivation(name='a', builder='')",
        "derivation(name='a', builder='/bin/sh', args=(1,))",
        "derivation(name='a', builder='/bin/sh', env={'k': 1})",
        "derivation(name='a', builder='/bin/sh', outputs=())",
    ],
)
def test_derivation_rejects_bad_fields(call: str) -> None:
    with pytest.raises(EvaluationError):
        evaluate_source(f"d = {call}", host=build_host())


def test_derivation_is_a_set_member() -> None:
    source = "d = derivation(name='a', builder='/bin/sh')\nn = len({d, d})"
    assert evaluate_source(source, host=build_host())["n"] == 1
