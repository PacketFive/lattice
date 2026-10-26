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

Phase 1: derivations, the standard library, the `Lattice` library
interface, the settings file, the daemon with its JSON-RPC and REST
interface, `Client` and the `lattice eval` and `lattice daemon` commands.
All of these work and are tested, the daemon over a Unix socket and TLS.
`stack` and `profile` are declared and belong to Phase 4.

Phase 2: the build sandbox. Each build runs in fresh Linux user, mount,
network, PID, UTS and IPC namespaces, with no capabilities, no new
privileges, a seccomp filter and a fixed environment, and sees only its
declared inputs, read-only. `lattice sandbox-config` prints the sandbox of
a derivation as an OCI runtime configuration, and `lattice backend-status`
reports which backends this machine can run. The OCI and microVM backends,
the store, dependency database, garbage collector, profiles and binary
caches follow in later phases (see the development plan in the PyrixOS
repository).

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

Python 3.12 or later is required. The only runtime dependencies are
PyBonsai and [Pydantic](https://docs.pydantic.dev/) 2. The distribution name is
`pyrixos-lattice`; the import name is `lattice`.

## Use

From the command line:

```sh
lattice eval hello.pyb
lattice --daemon eval hello.pyb
lattice --remote https://builder.example:7420 --ca ca.pem eval hello.pyb
```

`eval` prints one line per derivation: its store path and its canonical
JSON. `lattice sandbox-config FILE [NAME]` prints the OCI configuration of
one derivation's sandbox, and `lattice backend-status` reports each
sandbox backend and, if one cannot run, why. It exits with 1 and a message on standard error if the file is
rejected or fails. `--remote` reads a bearer token from the `LATTICE_TOKEN`
environment variable, never from the command line.

Settings are read from `/etc/lattice/settings.toml` if it exists, or from
the file given with `--settings`. Without a file the defaults apply.

```toml
sandbox_backend = "namespaces"
substituters = ["https://cache.example.org"]
trusted_keys = ["cache.example.org-1:..."]

[sandbox]
timeout = 3600
max_silent_time = 600
max_open_files = 1024

[daemon]
listen = "0.0.0.0:7420"
tls_cert = "/etc/lattice/tls/cert.pem"
tls_key = "/etc/lattice/tls/key.pem"

[[daemon.tokens]]
name = "ci"
sha256 = "<SHA-256 of the token, 64 hex characters>"
role = "user"
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
immutable Pydantic data models and raises subclasses of `LatticeError`. The
optional `on_event` callable receives a progress event for each step, and
the same events are logged to the `lattice` logger. The package ships
type hints (PEP 561). The names in `lattice.__all__` are the public
interface; from release 1.0 they follow semantic versioning, and a name is
removed only after one minor release of `DeprecationWarning`.

Through the daemon, from the same machine or another one:

```python
from lattice import Client

local = Client()  # unix:///run/lattice/daemon.sock
remote = Client("https://builder.example:7420", ca_file="ca.pem",
                cert_file="me.pem", key_file="me.key")
result = remote.evaluate("hello.pyb")
```

`Client` has the same methods, results and errors as `Lattice`, and both
satisfy the `LatticeAPI` protocol. `Client.evaluate` reads the file
locally and sends its text, because the daemon never opens a path named by
a caller. A bearer token can be passed as `token=` instead of a client
certificate.

`lattice daemon` serves one HTTP interface on its Unix socket and,
when `Settings.daemon.listen` is set, over TLS 1.3:

| Request | Style |
| --- | --- |
| `POST /v1/rpc` | JSON-RPC 2.0, with batches, notifications and progress events as NDJSON when the client sends `Accept: application/x-ndjson` |
| `POST /v1/<method>` | REST, with the parameters as the JSON body |
| `GET /v1/info` | Versions and method names |

```sh
curl --unix-socket /run/lattice/daemon.sock http://localhost/v1/info
curl --cacert ca.pem -H "Authorization: Bearer $LATTICE_TOKEN" \
     -d '{"source": "...", "filename": "hello.pyb"}' \
     https://builder.example:7420/v1/evaluate_source
```

On the socket, root, the daemon's user and members of `lattice-admins`
are admins, and members of `lattice-users` are users. Remotely, a client
certificate common name or the SHA-256 digest of a token is granted a
role in the daemon settings. Users may evaluate, build, check and switch
`per-user/<name>` profiles; admins may also collect garbage, push and
switch any profile. RFC 0001 section 6.2 is the specification.

Methods for later phases (`build`, `check`, `gc`, `switch`, `push`)
already exist with their final signatures and raise `NotImplementedError`
until their phase is complete.

## Layout

| Module | Role |
| --- | --- |
| `api.py` | The `Lattice` class |
| `models.py` | Pydantic result models and roles |
| `settings.py` | `Settings` and `DaemonSettings`, what one machine uses to build, fetch and serve |
| `errors.py` | `LatticeError` and its subclasses |
| `events.py` | Progress events passed to `on_event` and logged |
| `derivation.py` | The Pydantic Derivation model, canonical JSON, hashes and store paths |
| `stdlib.py` | Standard library functions and the PyBonsai host |
| `protocol.py` | JSON-RPC 2.0 and REST messages, parameters and error codes |
| `server.py` | Method table, permission checks and request dispatch |
| `transport.py` | `Daemon`: the HTTP server on the Unix socket and TLS, and caller identification |
| `client.py` | `Client` and the `LatticeAPI` protocol |
| `cli.py` | The `lattice` command, a wrapper around `Lattice` and `Client` |
| `sandbox/` | The build sandbox: its description, seccomp filter, environment, namespace backend, build runner and backend probe |

## License

GPL-2.0-only.
