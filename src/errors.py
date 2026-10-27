# SPDX-License-Identifier: GPL-2.0
"""Exceptions raised by the Lattice library (RFC 0001 section 6.1).

Every error Lattice raises on purpose is a LatticeError, so a program that
embeds Lattice can catch one type. Errors from PyBonsai are wrapped in
EvaluationFailed, with the original kept as ``error`` and as the cause.

A Client raises the same classes for errors the daemon reports, so a
program handles errors the same way whether it uses Lattice or Client.
"""

from __future__ import annotations

from pybonsai import PyBonsaiError


class LatticeError(Exception):
    """Base class for every error the Lattice library raises on purpose."""


class SettingsError(LatticeError):
    """A Settings value is invalid, for example an unknown sandbox backend."""


class EvaluationFailed(LatticeError):
    """A PyBonsai file was rejected or failed to evaluate.

    ``error`` is the PyBonsaiError raised by PyBonsai: RejectedSyntax,
    EvaluationError or LimitExceeded. Its message, including the file,
    line and column, is the message of this exception. When the error was
    raised by the daemon and received by a Client, only the message
    crosses the connection and ``error`` is None.
    """

    def __init__(self, error: PyBonsaiError | str) -> None:
        """Wrap ``error``, or take a message received from the daemon."""
        super().__init__(str(error))
        self.error = error if isinstance(error, PyBonsaiError) else None


class BuildFailed(LatticeError):
    """A build did not produce its outputs (section 9). Phase 2."""


class BackendUnavailable(LatticeError):
    """The chosen sandbox backend cannot run on this machine (section 9.7).

    Lattice never falls back to a weaker backend, so the build stops.
    """


class StoreError(LatticeError):
    """The store or its database is in a state that prevents the operation. Phase 3."""


class ActivationFailed(LatticeError):
    """A profile, slot or kernel module could not be changed (section 13). Phase 4.

    Whatever was changed before the failure has been put back.
    """


class SubstitutionFailed(LatticeError):
    """A cached path was missing, unsigned or did not match its hashes (section 14). Phase 5."""


class AuthenticationFailed(LatticeError):
    """The daemon could not establish who the caller is (section 6.2)."""


class PermissionDenied(LatticeError):
    """The caller is known to the daemon but may not run this operation (section 6.2)."""


class ProtocolError(LatticeError):
    """A request or reply did not follow the daemon protocol (section 6.2)."""
