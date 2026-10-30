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

Phase 2: the build sandbox. Each build runs in fresh Linux user, mount,
network, PID, UTS and IPC namespaces, with no capabilities, no new
privileges, a seccomp filter and a fixed environment, and sees only its
declared inputs, read-only. The sandbox itself is
[Cloche](https://github.com/PacketFive/cloche), a separate package that
runs any program this way; Lattice turns each derivation into a Cloche
command. `lattice sandbox-config` prints the sandbox of a derivation as an
OCI runtime configuration, and `lattice backend-status` reports which
backends this machine can run.

Phase 3: the store. `lattice build` builds a derivation and its inputs,
up to `max_jobs` at once, publishes each output atomically and read-only,
and records it with its run-time references in an SQLite database.
`fetch_url` derivations are downloaded and checked against their SHA-256
digest. `lattice check` builds a registered derivation again and reports
the files that differ, and `lattice gc` removes every path that no root and
no running process uses.

Phase 4: profiles and stacks. `lattice switch` builds an environment and
makes it the next generation of a profile, `lattice rollback` makes an
earlier one current again, and every generation is a garbage collection
root. `stack` combines packages, kernel modules and device patterns into
one version of a stack, and the `system` profile assigns one stack to each
slot. A switch changes the kernel modules of a slot only while their
devices are closed and the modules are unused, and otherwise defers the
change to the next boot.

Phase 5: binary caches. `lattice push` signs store paths built on this
machine and publishes them, their closures and their SLSA provenance to an
HTTP cache, a directory or a CernVM-FS repository. A build fetches every
path a trusted cache holds instead of building it, and checks its
signature and hashes first. Paths from CernVM-FS can be shallow paths,
symlinks into `/cvmfs` that are fetched file by file, and `lattice
materialise` turns one into a full copy.

Phase 6: container and microVM backends. `sandbox_backend` may be
`namespaces`, `oci` or `microvm`, and Lattice never falls back to a
weaker one. The OCI backend runs builds under a rootless runtime, and the
microVM backend boots a virtual machine with its own kernel and no
network device under Firecracker, Cloud Hypervisor or OpenVMM. The guest
kernel and init are named in `[sandbox.microvm]`, usually as files in
store paths, and are recorded in the provenance of every path built on
that backend. `lattice check --backend B` rebuilds a path on backend B to
confirm the outputs match those of the backend that built it.

Phase 10: system configuration (RFC 0004). `machine` and the records
`user`, `group`, `file_system`, `service` and `etc_file` describe a whole
machine, which `builtin:system` builds into one store path. `lattice
switch` makes it a generation of the `system` profile with a
systemd-boot entry and activates it: `/etc` through `/etc/static`,
`passwd`, `group` and `shadow`, new mounts, slots and systemd units.
`--boot` defers it to the next boot and `--test` activates it without a
generation. A `config.pyb` written by Trellis next to the description is
read as `config`. With a delegated cgroup, builds also get memory, CPU
and process limits.

## Install

```sh
pip install pyrixos-lattice            # or 'pyrixos-lattice[config]' for lattice config
```

Lattice stays below 1.0 until PyrixOS 1.0, so its interface may still change
between minor versions. To work on Lattice itself, next to checkouts of
the components:

```sh
python -m pip install --upgrade "pip>=25.1"
pip install --group dev
pip install --config-settings editable_mode=strict -e ../pybonsai -e ../cloche -e ../trellis -e .
pybootstrap check
```

The `dev` dependency group installs [pybootstrap](https://github.com/packetfive/pybootstrap)
and the tools its gates drive (ruff, pyright, pytest with coverage).

The repository's `src` directory is the package itself, mapped to its
import name in `pyproject.toml`. Strict editable mode gives that mapping a
real `lattice/` directory under `build/`, which pyright needs to find the
package. Tests import the installed package, so install before running
them.

Python 3.12 or later is required. The only runtime dependencies are
PyBonsai, Cloche, [Pydantic](https://docs.pydantic.dev/) 2 and
[cryptography](https://cryptography.io/) for Ed25519 signatures. The distribution name is
`pyrixos-lattice`; the import name is `lattice`. `lattice config` also needs
[Trellis](https://github.com/PacketFive/trellis), which
the `config` extra adds.

Releases are made by pushing a tag `vX.Y.Z` that matches `__version__`
and has a section in `CHANGELOG.md`. The release workflow runs the gates,
builds the package, publishes it to PyPI through trusted publishing with
attestations, and creates the GitHub release.

## Use

From the command line:

```sh
lattice eval hello.pyb
lattice --daemon eval hello.pyb
lattice --remote https://builder.example:7420 --ca ca.pem eval hello.pyb
```

`eval` prints one line per derivation: its store path and its canonical
JSON.

```sh
lattice plan hello.pyb hello        # what a build would reuse, fetch or build
lattice build hello.pyb hello       # prints the store path of each output
lattice check hello.pyb hello       # or: lattice build --check hello.pyb hello
lattice check --backend microvm hello.pyb hello   # rebuild on another backend
lattice add-root per-user/me/hello /lattice/store/<hash>-hello
lattice roots
lattice path-info /lattice/store/<hash>-hello
lattice verify /lattice/store/<hash>-hello   # exits with 1 if the contents changed
lattice remove-root per-user/me/hello
lattice gc
```

Profiles and stacks:

```sh
lattice switch system.pyb system              # profile "system" by default
lattice switch --profile per-user/me env.pyb  # a user's own profile
lattice generations                           # "(current)" marks the current one
lattice rollback                              # or: lattice rollback 41
lattice delete-generations --keep 5
lattice slots                                 # active stack, and the next boot's if it differs
lattice activate-boot                         # run once at boot, before services
```

A whole machine (RFC 0004), as an admin:

```sh
lattice switch                                # /etc/pyrixos/system.pyb, name "system"
lattice switch --test my-system.pyb           # activate now, no generation or boot entry
lattice switch --boot                         # the next boot uses it
lattice systems                               # generations, "(current, booted)"
lattice rollback --profile system             # back to the previous system
```

```python
# /etc/pyrixos/system.pyb
alice = user(name="alice", groups=["wheel"], password_file="/etc/pyrixos/secrets/alice.hash")
system = machine(
    name="lab-1",
    kernel=linux,
    initrd=linux_initrd,
    packages=[bash, coreutils, python3] + ([htop] if config.get("TOOLS_HTOP", False) else []),
    users=[alice],
    groups=[group(name="wheel", gid=10)],
    file_systems=[file_system(mount_point="/", device="/dev/disk/by-label/root", fs_type="ext4")],
    services=[service(name="web", exec_start=f"{python3}/bin/python3 -m http.server")],
)
```

Progress is printed as `activating: <step>`, and changes that wait for
the next boot as `note: ...`.

Binary caches:

```sh
lattice generate-key cache.example.org-1 /etc/lattice/cache.key  # prints the public key
lattice push --to https://cache.example.org /lattice/store/<hash>-hello
lattice push --to cvmfs://sw.example.org /lattice/store/<hash>-nvidia-570
lattice substitute /lattice/store/<hash>-hello     # fetch one path and its references
lattice substitute --shallow /lattice/store/<hash>-cuda-12.8
lattice materialise /lattice/store/<hash>-cuda-12.8
```

A cache URL is `https://`, `http://`, `file:///<directory>` or
`cvmfs://<repository>`. Pushing to HTTP uses `PUT`, so the server must
accept uploads from the build machine.

```python
# system.pyb
nvidia_570 = stack(
    slot="nvidia",
    name="nvidia-570",
    contents=[nvidia_driver_570, cuda_12_8],
    kernel_modules=[nvidia_kmod_570],
    devices=["/dev/nvidia*"],
)
system = profile(name="system", packages=[slurm], stacks={"nvidia": nvidia_570})
```

System options, with [Trellis](https://github.com/PacketFive/trellis)
installed through the `config` extra:

```sh
lattice config menu                  # the same as: trellis menu
lattice config set NET=y WIFI=m
lattice config explain WIFI n
```

`lattice config` passes its arguments to the `trellis` command and always
runs locally.

Without `NAME`, a command that takes `FILE [NAME]` uses the only derivation
the file defines. Build progress is printed on standard error. A build adds
no root, so its outputs are kept by the next `gc` only if a root names them. `lattice sandbox-config FILE [NAME]` prints the OCI configuration of
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
require_provenance = false   # true rejects paths without signed provenance

[store]
root = "/lattice"     # store, staging and database live below this
max_jobs = 4          # derivations built at once, 1 by default
stacks = "/run/lattice/stacks"  # slot symlinks, this by default

[sandbox]
timeout = 3600
max_silent_time = 600
max_open_files = 1024
max_memory_mib = 8192 # these three need a delegated cgroup (Delegate=yes)
max_cpu = 4.0
max_processes = 4096
runtime = "crun"      # OCI runtime; the first of crun, runc, youki by default

[sandbox.microvm]
vmm = "firecracker"   # or cloud-hypervisor, openvmm
kernel = "/lattice/store/<hash>-linux-guest/vmlinux"
init = "/lattice/store/<hash>-cloche-guest-init/init"
memory_mib = 2048
cpus = 2
output_mib = 4096     # size of the output device, which caps the outputs

[system]
description = "/etc/pyrixos/system.pyb"  # what lattice switch uses without FILE
root = "/"            # a mounted target when an installer prepares a disk

[cache]
secret_key_file = "/etc/lattice/cache.key"  # on a machine that pushes
builder_id = "ci-1"         # names this machine in provenance, host name by default
cvmfs_root = "/cvmfs"       # where CernVM-FS repositories are mounted
cvmfs_server = "cvmfs_server"

[cache.shallow]
slots = ["nvidia"]          # stacks in these slots, and their contents, may be shallow
packages = ["cuda-*"]       # and so may derivations with these names

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
remote = Client(
    "https://builder.example:7420", ca_file="ca.pem", cert_file="me.pem", key_file="me.key"
)
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
role in the daemon settings. Users may evaluate, plan, build, check, read
path information, verify and list roots, and may add, remove and switch
only roots and profiles under `per-user/<name>`; admins may also collect
garbage, push and change any root or profile. RFC 0001 section 6.2 is the specification.

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
| `sandbox.py` | Turns a derivation into a Cloche command, chooses and configures the backend, and runs the build in the sandbox |
| `store/` | The store: layout, locks, content hashes, reference scanning, the database, publication, downloads, the builder and the garbage collector |
| `procscan.py` | Finds the store paths that running processes use |
| `system/` | System configuration: the records, `builtin:system`, accounts, boot entries, activation and the system manager |

## License

GPL-2.0-only. See [LICENSE](LICENSE).
