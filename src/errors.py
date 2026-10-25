# SPDX-License-Identifier: GPL-2.0
"""Exceptions raised by the Lattice library (RFC 0001 section 6.1).

Every error Lattice raises on purpose is a LatticeError, so a program that
embeds Lattice can catch one type. Errors from PyBonsai are wrapped in
EvaluationFailed, with the original kept as ``error`` and as the cause.
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
    line and column, is the message of this exception.
    """

    def __init__(self, error: PyBonsaiError) -> None:
        """Wrap ``error`` and use its text as the message."""
        super().__init__(str(error))
        self.error = error


class BuildFailed(LatticeError):
    """A build did not produce its outputs (section 9). Phase 2."""


class BackendUnavailable(LatticeError):
    """The chosen sandbox backend cannot run on this machine (section 9.7).

    Lattice never falls back to a weaker backend, so the build stops.
    """


class StoreError(LatticeError):
    """The store or its database is in a state that prevents the operation. Phase 3."""


class SubstitutionFailed(LatticeError):
    """A cached path was missing, unsigned or did not match its hashes (section 14). Phase 5."""
