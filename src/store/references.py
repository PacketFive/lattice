# SPDX-License-Identifier: GPL-2.0
"""Scanning new store paths for references (RFC 0001 section 10.3).

A build can only refer to store paths it could see: its inputs and its
own outputs. ReferenceScanner looks for their hashes in the serialisation
of each output (verify.py), so file contents, symbolic link targets and
file names are all searched.

The outputs of one derivation share its hash, and ``<hash>-hello`` is the
start of ``<hash>-hello-dev``. After a hash, the longest output name that
follows is therefore the path referred to. A hash followed by none of
them, as when a build assembles a path from parts, is counted as a
reference to every output of that derivation, so collection keeps too
much rather than too little.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..derivation import STORE_HASH_LEN


class ReferenceScanner:
    """Finds which of ``candidates``, store paths, occur in the data fed to it."""

    def __init__(self, candidates: Iterable[str]) -> None:
        """Prepare one pattern that matches the hash of any candidate."""
        self._paths: dict[bytes, list[tuple[bytes, str]]] = {}
        for path in set(candidates):
            name = path.rpartition("/")[2].encode()
            self._paths.setdefault(name[:STORE_HASH_LEN], []).append((name, path))
        for names in self._paths.values():
            names.sort(key=lambda item: len(item[0]), reverse=True)
        hashes = b"|".join(re.escape(digest) for digest in sorted(self._paths))
        # A lookahead finds overlapping matches as well.
        self._pattern = re.compile(b"(?=(" + hashes + b"))") if self._paths else None
        longest = max((len(n) for names in self._paths.values() for n, _ in names), default=1)
        self._keep = longest - 1
        self._tail = b""
        self._found: set[str] = set()

    def _scan(self, buffer: bytes, *, final: bool) -> None:
        assert self._pattern is not None  # noqa: S101
        limit = len(buffer) if final else max(len(buffer) - self._keep, 0)
        for match in self._pattern.finditer(buffer, 0, len(buffer)):
            start = match.start()
            if start >= limit:
                break
            names = self._paths[match.group(1)]
            for name, path in names:
                if buffer.startswith(name, start):
                    self._found.add(path)
                    break
            else:
                self._found.update(path for _, path in names)
        self._tail = buffer[limit:]

    def feed(self, data: bytes) -> None:
        """Scan the next part of the data."""
        if self._pattern is not None:
            self._scan(self._tail + data, final=False)

    def finish(self) -> frozenset[str]:
        """Scan what is left and return the candidates that were found."""
        if self._pattern is not None:
            self._scan(self._tail, final=True)
            self._tail = b""
        return frozenset(self._found)
