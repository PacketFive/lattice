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

Phase 1 skeleton: derivations, the standard library, the `Lattice`
library interface and `lattice eval`.
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

From the command line:

```sh
lattice eval hello.pyb
```

From Python, in your own tools:

```python
from lattice import Lattice, LatticeError, Settings

lattice = Lattice(Settings(sandbox_backend="namespaces"), on_event=print)
try:
    result = lattice.evaluate("hello.pyb")
except LatticeError as error:
    print(error)
else:
    for name, drv in result.derivations.items():
        print(name, drv.store_path)
```

The `lattice` command is a thin wrapper around the `Lattice` class, so a
program can do everything the command does. `Lattice` reads no settings
from the machine on its own, keeps no state between calls, returns
immutable result objects and raises subclasses of `LatticeError`. The
optional `on_event` callable receives a progress event for each step, and
the same events are logged to the `lattice` logger. The package ships
type hints (PEP 561). The names in `lattice.__all__` are the public
interface; from release 1.0 they follow semantic versioning, and a name is
removed only after one minor release of `DeprecationWarning`.

Methods for later phases (`build`, `check`, `gc`, `switch`, `push`)
already exist with their final signatures and raise `NotImplementedError`
until their phase is complete.

## Layout

| Module | Role |
| --- | --- |
| `api.py` | The `Lattice` class and its result types |
| `settings.py` | `Settings`, what one machine uses to build and fetch |
| `errors.py` | `LatticeError` and its subclasses |
| `events.py` | Progress events passed to `on_event` and logged |
| `derivation.py` | The Derivation type, canonical JSON, hashes and store paths |
| `stdlib.py` | Standard library functions and the PyBonsai host |
| `cli.py` | The `lattice` command, a wrapper around `Lattice` |

## License

GPL-2.0-only.
