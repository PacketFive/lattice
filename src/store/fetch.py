# SPDX-License-Identifier: GPL-2.0
"""The download behind ``fetch_url`` (RFC 0001 sections 7.6 and 16).

A derivation with the builder ``builtin:fetch_url`` runs here, in Lattice,
not in the sandbox, because it is the one build that needs the network.
Its result is accepted only if its SHA-256 digest is the one fixed in the
configuration, so the download cannot change what any build sees.

Only ``https`` and ``http`` are followed, also on a redirect, so a server
cannot point Lattice at a local file.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any

from ..derivation import Derivation
from ..errors import BuildFailed
from ..models import Model

TIMEOUT = 60
"""Seconds without progress after which a download fails."""

_SCHEMES = ("https", "http")
_CHUNK = 1 << 16


class FetchResult(Model):
    """A finished download: the file, its digest and size, and the time taken."""

    url: str
    path: str
    sha256: str
    size: int
    seconds: float


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,  # noqa: ANN401
        newurl: str,
    ) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme not in _SCHEMES:
            raise urllib.error.HTTPError(newurl, code, "redirect to a refused scheme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_Redirects())


def fetch(drv: Derivation, destination: str) -> FetchResult:
    """Download the URL of ``drv`` to the new file ``destination`` and check it.

    Raises BuildFailed if the download fails or its digest is not the
    expected one; ``destination`` is then removed.
    """
    url, expected = drv.env.get("url"), drv.env.get("sha256")
    if not isinstance(url, str) or not isinstance(expected, str):
        raise BuildFailed(f"{drv.name}: fetch_url needs url and sha256 in env")
    if urllib.parse.urlsplit(url).scheme not in _SCHEMES:
        raise BuildFailed(f"{drv.name}: fetch_url refuses {url!r}")
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        try:
            with OPENER.open(url, timeout=TIMEOUT) as response:
                while chunk := response.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    os.write(fd, chunk)
        except (urllib.error.URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", None) or error
            raise BuildFailed(f"{drv.name}: cannot fetch {url}: {reason}") from error
        finally:
            os.close(fd)
        actual = digest.hexdigest()
        if actual != expected:
            raise BuildFailed(f"{drv.name}: {url} has SHA-256 {actual}, expected {expected}")
    except BaseException:
        os.unlink(destination)
        raise
    return FetchResult(
        url=url, path=destination, sha256=actual, size=size, seconds=time.monotonic() - started
    )
