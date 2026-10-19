# SPDX-License-Identifier: GPL-2.0
"""Standard library tests: RFC 0001 sections 7.5, 7.6 and 7.8."""

import pytest
from pybonsai import RejectedSyntax, evaluate_source

from lattice.derivation import Derivation
from lattice.stdlib import build_host

pytestmark = pytest.mark.xfail(raises=NotImplementedError, reason="not implemented yet")


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
