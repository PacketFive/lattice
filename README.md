# Lattice

Lattice is the package manager of
[PyrixOS](https://github.com/PacketFive/PyrixOS). It reads package
descriptions written in [PyBonsai](https://github.com/PacketFive/pybonsai),
turns them into derivations (exact build recipes identified by a hash of
their contents), builds them in an isolated sandbox, and stores the results
in an immutable, content-addressed store. RFC 0001 is the specification.

Lattice is a separate package from PyBonsai. PyBonsai is the language and
knows nothing about builds. Lattice embeds it and supplies the standard
library (`derivation`, `fetch_url`, `stack`, `profile`) through a
`pybonsai.Host`.

## Status

Phase 1 skeleton: derivations, the standard library and `lattice eval`.
Function bodies raise `NotImplementedError` and the tests are marked as
expected failures until the code is written. The sandbox, store,
dependency database, garbage collector, profiles and binary caches follow
in later phases (see the development plan in the PyrixOS repository).

## Install

```sh
pip install --config-settings editable_mode=strict -e '../pybonsai[dev]' -e '.[dev]'
pybootstrap check
```

The `dev` extra installs [pybootstrap](https://github.com/packetfive/pybootstrap)
and the tools its gates drive (ruff, pyright, pytest with coverage).

The repository's `src` directory is the package itself, mapped to its
import name in `pyproject.toml`. Strict editable mode gives that mapping a
real `lattice/` directory under `build/`, which pyright needs to find the
package. Tests import the installed package, so install before running
them.

Python 3.12 or later is required. The distribution name is
`pyrixos-lattice`; the import name is `lattice`.

## Use

```sh
lattice eval hello.pyb
```

```python
from pybonsai import evaluate_file
from lattice.stdlib import build_host

names = evaluate_file("hello.pyb", host=build_host())
```

## Layout

| Module | Role |
| --- | --- |
| `derivation.py` | The Derivation type, canonical JSON, hashes and store paths |
| `stdlib.py` | Standard library functions and the PyBonsai host |
| `cli.py` | The `lattice` command |

## License

GPL-2.0-only.
