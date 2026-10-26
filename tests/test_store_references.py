# SPDX-License-Identifier: GPL-2.0
"""Finding references to store paths: RFC 0001 section 10.3."""

from lattice.store.references import ReferenceScanner

A = "a" * 32
B = "b" * 32
HELLO = f"/lattice/store/{A}-hello"
HELLO_DEV = f"/lattice/store/{A}-hello-dev"
LIB = f"/lattice/store/{B}-lib"


def scan(*pieces: bytes, candidates: tuple[str, ...] = (HELLO, HELLO_DEV, LIB)) -> frozenset[str]:
    scanner = ReferenceScanner(candidates)
    for piece in pieces:
        scanner.feed(piece)
    return scanner.finish()


def test_finds_paths() -> None:
    assert scan(f"x {LIB}/lib/libc.so y".encode()) == {LIB}
    assert scan(b"nothing here") == frozenset()


def test_longest_output_name_wins() -> None:
    assert scan(f"{HELLO_DEV}/include".encode()) == {HELLO_DEV}
    assert scan(f"{HELLO}/bin".encode()) == {HELLO}


def test_bare_hash_refers_to_every_output() -> None:
    assert scan(A.encode()) == {HELLO, HELLO_DEV}


def test_match_split_across_pieces() -> None:
    data = f"..{HELLO_DEV}..{LIB}".encode()
    for cut in range(len(data) + 1):
        assert scan(data[:cut], data[cut:]) == {HELLO_DEV, LIB}, cut
    assert scan(*(bytes([b]) for b in data)) == {HELLO_DEV, LIB}


def test_overlapping_hashes() -> None:
    other = f"/lattice/store/{'a' * 31}c-x"
    data = ("a" * 32 + "c-x").encode()
    assert scan(data, candidates=(HELLO, HELLO_DEV, other)) == {HELLO, HELLO_DEV, other}


def test_no_candidates() -> None:
    assert scan(HELLO.encode(), candidates=()) == frozenset()
