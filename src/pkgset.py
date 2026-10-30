# SPDX-License-Identifier: GPL-2.0
"""Package sets: directories of PyBonsai layers (RFC 0005 section 5).

Each ``.pyb`` file of the directory is a layer. Layers are evaluated in
the byte order of their names, each with the standard library and every
public top-level name of the layers before it. A layer may not define a
name that an earlier layer or the standard library already has, and a
name starting with ``_`` stays private to its layer. A file evaluated
with the set sees its public names as if they were standard library
names. Lattice and Client read the layers; the daemon receives their
text and never reads caller files.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import pybonsai
from pybonsai import PyBonsaiError

from .errors import EvaluationFailed
from .models import SourceFile
from .stdlib import build_host

LAYER_SUFFIX = ".pyb"


def read_package_set(directory: str | os.PathLike[str]) -> tuple[SourceFile, ...]:
    """Return the layers of the package set in ``directory``, in evaluation order.

    Every regular file whose name ends in ``.pyb`` is a layer; names are
    compared as bytes. Raises EvaluationFailed if there is none and
    OSError if the directory or a layer cannot be read.
    """
    root = os.fspath(directory)
    names = sorted((n for n in os.listdir(root) if n.endswith(LAYER_SUFFIX)), key=os.fsencode)
    layers = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as file:
            layers.append(SourceFile(filename=path, source=file.read()))
    if not layers:
        raise EvaluationFailed(f"package set {root} holds no {LAYER_SUFFIX} layers")
    return tuple(layers)


def evaluate_layers(layers: Sequence[SourceFile]) -> dict[str, object]:
    """Evaluate ``layers`` in order and return the public names of the set.

    Raises EvaluationFailed if a layer is rejected or fails, or if it
    defines a name that the standard library or an earlier layer has,
    naming both places.
    """
    base = build_host()
    names: dict[str, object] = {}
    origin: dict[str, str] = {}
    for layer in layers:
        host = build_host(extra=names) if names else base
        try:
            result = pybonsai.evaluate_source(layer.source, layer.filename, host=host)
        except PyBonsaiError as error:
            raise EvaluationFailed(error) from error
        for key, value in result.items():
            name = str(key)
            if name.startswith("_"):
                continue
            if name in origin:
                raise EvaluationFailed(
                    f"{layer.filename}: {name} is already defined in {origin[name]}"
                )
            if name in base.names:
                raise EvaluationFailed(f"{layer.filename}: {name} is a standard library name")
            names[name] = value
            origin[name] = layer.filename
    return names
