# SPDX-License-Identifier: GPL-2.0
"""Shared fixtures: a stand-in backend for daemon and client tests.

FakeAPI satisfies LatticeAPI without evaluating or building anything, so
the daemon, protocol and client are tested before the operations behind
them exist.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

import pytest

from lattice import (
    BuildResult,
    Derivation,
    Evaluation,
    EvaluationFailed,
    EvaluationStarted,
    EventHandler,
    GCResult,
    Generation,
    LatticeAPI,
)


class FakeAPI:
    def __init__(self, on_event: EventHandler | None) -> None:
        self.on_event = on_event

    def evaluate(self, path: str | os.PathLike[str]) -> Evaluation:
        raise AssertionError("the daemon never evaluates a path")

    def evaluate_source(self, source: str, filename: str = "<string>") -> Evaluation:
        if self.on_event is not None:
            self.on_event(EvaluationStarted(filename=filename))
        if source == "fail":
            raise EvaluationFailed(f"{filename}:1:1: rejected")
        if source == "crash":
            raise RuntimeError("secret detail of the daemon machine")
        return Evaluation(filename=filename, derivations={}, seconds=0.25)

    def build(self, drv: Derivation) -> BuildResult:
        raise NotImplementedError

    def check(self, drv: Derivation) -> BuildResult:
        raise NotImplementedError

    def gc(self) -> GCResult:
        return GCResult(removed=("/lattice/store/a-x",), freed_bytes=3, seconds=0.5)

    def switch(self, profile: str, drv: Derivation) -> Generation:
        raise NotImplementedError

    def push(self, paths: Iterable[str], cache: str) -> None:
        return None


@pytest.fixture
def factory() -> Callable[[EventHandler | None], LatticeAPI]:
    return FakeAPI
