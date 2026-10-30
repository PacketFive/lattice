# SPDX-License-Identifier: GPL-2.0
"""Machine settings for the Lattice library and daemon (RFC 0001 sections 6.1 and 6.2).

Settings are passed to Lattice explicitly. Nothing is read from the
machine unless the program calls Settings.load. Evaluation limits are not
settings: section 7.7 fixes them per release so that a file evaluates the
same way on every machine.

An invalid value raises SettingsError, never a Pydantic error, so a
program only has to catch LatticeError.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import tomllib
import urllib.parse
from typing import TYPE_CHECKING, Any, Literal, Self, get_args

from pydantic import Field, ValidationError, model_validator

from .errors import SettingsError
from .models import Model, Role, parse

SandboxBackend = Literal["namespaces", "oci", "microvm"]
SANDBOX_BACKENDS: tuple[str, ...] = get_args(SandboxBackend)

DEFAULT_SOCKET = "/run/lattice/daemon.sock"
DEFAULT_SETTINGS_PATH = "/etc/lattice/settings.toml"
DEFAULT_STORE_ROOT = "/lattice"
DEFAULT_STACKS = "/run/lattice/stacks"
DEFAULT_DESCRIPTION = "/etc/pyrixos/system.pyb"
DEFAULT_PACKAGE_SET = "/etc/pyrixos/pkgs"


def _settings_error(error: ValidationError) -> SettingsError:
    parts = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        parts.append(f"{where}: {item['msg']}" if where else item["msg"])
    return SettingsError("; ".join(parts))


class _SettingsModel(Model):
    if not TYPE_CHECKING:

        def __init__(self, **data: Any) -> None:
            try:
                super().__init__(**data)
            except ValidationError as error:
                raise _settings_error(error) from None

        # Marks __init__ as Pydantic's own, so a nested settings model is
        # validated in the mode of the outer call (JSON for a file) instead
        # of through this method in Python mode, which refuses lists.
        __init__.__pydantic_base_init__ = True


class ClientGrant(_SettingsModel):
    """A TLS client certificate the remote API accepts, by its subject common name."""

    common_name: str = Field(min_length=1)
    role: Role = Role.USER


class TokenGrant(_SettingsModel):
    """A bearer token the remote API accepts.

    Only the SHA-256 digest of the token is stored, as 64 lowercase hex
    characters, so the settings never hold a usable secret.
    """

    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Role = Role.USER


class DaemonSettings(_SettingsModel):
    """How ``lattice daemon`` listens and whom it accepts (section 6.2).

    The daemon always listens on the Unix socket ``socket``. A local caller
    is an admin if it is root, the daemon's own user or a member of
    ``admin_group``, and a user if it is a member of ``user_group``.

    When ``listen`` is set to ``host:port``, the daemon also serves the
    remote API over TLS 1.3 with ``tls_cert`` and ``tls_key``. Remote
    callers authenticate with a client certificate signed by
    ``tls_client_ca`` and listed in ``clients``, or with a bearer token
    listed in ``tokens``. Requests larger than ``max_request_bytes`` are
    refused.
    """

    socket: str = DEFAULT_SOCKET
    user_group: str = "lattice-users"
    admin_group: str = "lattice-admins"
    listen: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_client_ca: str | None = None
    clients: tuple[ClientGrant, ...] = ()
    tokens: tuple[TokenGrant, ...] = ()
    max_request_bytes: int = Field(default=16 * 1024 * 1024, gt=0)

    @model_validator(mode="after")
    def _check_remote(self) -> Self:
        if self.clients and self.tls_client_ca is None:
            raise ValueError("clients need tls_client_ca to verify their certificates")
        if self.listen is None:
            return self
        if self.tls_cert is None or self.tls_key is None:
            raise ValueError("listen needs tls_cert and tls_key; the remote API is TLS only")
        if not self.clients and not self.tokens:
            raise ValueError("listen needs at least one entry in clients or tokens")
        return self


def _absolute(value: str | None, what: str) -> None:
    if value is not None and (not os.path.isabs(value) or os.path.normpath(value) != value):
        raise ValueError(f"{what} must be a normalised absolute path, not {value!r}")


class MicroVMSettings(_SettingsModel):
    """How the microVM backend boots its virtual machines (section 9.7).

    ``kernel`` is an uncompressed guest kernel and ``init`` a static
    build of Cloche's guest init. Each is a file in a store path, such as
    ``/lattice/store/<hash>-linux/vmlinux``, or a file elsewhere on the
    host. ``vmm`` is the virtual machine monitor, found in PATH unless
    ``vmm_path`` names it. ``memory_mib`` and ``cpus`` size the machine,
    and ``output_mib`` is the size of the device the outputs are written
    to, which caps their size.
    """

    vmm: Literal["firecracker", "cloud-hypervisor", "openvmm"] = "firecracker"
    vmm_path: str | None = None
    kernel: str | None = None
    init: str | None = None
    memory_mib: int = Field(default=2048, ge=128)
    cpus: int = Field(default=2, ge=1, le=64)
    output_mib: int = Field(default=4096, ge=1)
    boot_timeout: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _check_paths(self) -> Self:
        for name in ("vmm_path", "kernel", "init"):
            _absolute(getattr(self, name), f"microvm {name}")
        return self


class SandboxSettings(_SettingsModel):
    """Limits the build sandbox applies to every build (section 9.4).

    ``timeout`` ends a build that runs longer than that many seconds, and
    ``max_silent_time`` one that writes no log output for that many
    seconds. None means no limit. ``max_open_files`` is the builder's
    ``RLIMIT_NOFILE``. ``max_memory_mib``, ``max_cpu`` (CPUs, such as
    0.5) and ``max_processes`` limit each build through its own cgroup,
    and apply only when Lattice runs in a delegated cgroup version 2
    subtree; None means no limit. None of these is part of a derivation,
    so changing them changes no hash. ``runtime`` is the OCI runtime of the oci
    backend, a name in PATH or an absolute path, and the first of crun,
    runc and youki when unset. ``microvm`` configures the microVM
    backend.
    """

    timeout: int | None = Field(default=None, gt=0)
    max_silent_time: int | None = Field(default=None, gt=0)
    max_open_files: int = Field(default=1024, ge=64, le=1048576)
    max_memory_mib: int | None = Field(default=None, ge=1)
    max_cpu: float | None = Field(default=None, ge=0.01)
    max_processes: int | None = Field(default=None, ge=8)
    runtime: str | None = Field(default=None, min_length=1)
    microvm: MicroVMSettings = MicroVMSettings()

    @model_validator(mode="after")
    def _check_runtime(self) -> Self:
        if self.runtime is not None and "/" in self.runtime:
            _absolute(self.runtime, "runtime")
        return self


class StoreSettings(_SettingsModel):
    """Where the store is kept and how many builds run at once (sections 10 to 12).

    ``root`` holds ``store``, ``staging`` and ``var``. Store paths keep
    their names under ``/lattice/store`` wherever ``root`` is, so a store
    under another root is for testing and for preparing a store that is
    later moved into place. ``max_jobs`` is the number of builds that may
    run in parallel. ``stacks`` is the directory of slot symlinks
    (section 13.2).
    """

    root: str = DEFAULT_STORE_ROOT
    max_jobs: int = Field(default=1, ge=1, le=256)
    stacks: str = DEFAULT_STACKS

    @model_validator(mode="after")
    def _check_root(self) -> Self:
        for name, value in (("root", self.root), ("stacks", self.stacks)):
            if not os.path.isabs(value) or os.path.normpath(value) != value:
                raise ValueError(f"store {name} must be a normalised absolute path, not {value!r}")
        return self


KEY_NAME = re.compile(r"[A-Za-z0-9._+-]+")
_CVMFS_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
CACHE_SCHEMES = ("https", "http", "file", "cvmfs")


def check_public_key(text: str) -> str:
    """Return ``text`` if it is ``<name>:<base64 of 32 bytes>``, an Ed25519 public key.

    Raises ValueError otherwise. The name says which key signed an entry.
    """
    name, colon, encoded = text.partition(":")
    try:
        key = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        key = b""
    if not colon or not KEY_NAME.fullmatch(name) or len(key) != 32:
        raise ValueError(f"{text!r} is not a key of the form <name>:<base64 of 32 bytes>")
    return text


def check_cache_url(url: str) -> str:
    """Return ``url`` if it names a cache Lattice can read, and raise ValueError if not.

    A cache is ``https://``, ``http://``, ``file:///<directory>`` or
    ``cvmfs://<repository>`` (section 14).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in CACHE_SCHEMES or parts.query or parts.fragment:
        raise ValueError(f"{url!r} is not an https, http, file or cvmfs cache URL")
    if parts.scheme == "cvmfs":
        if not _CVMFS_REPO.fullmatch(parts.netloc) or parts.path not in ("", "/"):
            raise ValueError(f"{url!r} must be cvmfs://<repository>")
    elif parts.scheme == "file":
        if parts.netloc or not os.path.isabs(parts.path):
            raise ValueError(f"{url!r} must be file:///<absolute directory>")
    elif not parts.netloc:
        raise ValueError(f"{url!r} has no host")
    return url


class ShallowRules(_SettingsModel):
    """Which store paths may be shallow paths into CernVM-FS (section 14.6).

    ``slots`` names slots whose stack contents may be shallow and
    ``packages`` holds glob patterns matched against derivation names.
    Kernel modules are always local copies, whatever these say.
    """

    slots: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()


class CacheSettings(_SettingsModel):
    """How this machine publishes to and reads from caches (section 14).

    ``secret_key_file`` holds the Ed25519 key ``lattice push`` signs with,
    written by ``lattice generate-key``. ``builder_id`` names this machine
    in provenance; empty means the host name. ``cvmfs_root`` is where
    CernVM-FS repositories are mounted and ``cvmfs_server`` the program
    that publishes to one. ``shallow`` says which paths may be shallow.
    """

    secret_key_file: str | None = None
    builder_id: str = ""
    cvmfs_root: str = "/cvmfs"
    cvmfs_server: str = "cvmfs_server"
    shallow: ShallowRules = ShallowRules()

    @model_validator(mode="after")
    def _check_paths(self) -> Self:
        if (
            not os.path.isabs(self.cvmfs_root)
            or os.path.normpath(self.cvmfs_root) != self.cvmfs_root
        ):
            raise ValueError(
                f"cvmfs_root must be a normalised absolute path, not {self.cvmfs_root!r}"
            )
        return self


class SystemSettings(_SettingsModel):
    """Where system configuration (RFC 0004) reads its description and acts.

    ``description`` is the system description ``lattice switch`` uses
    without a file argument, and ``package_set`` the package set
    directory (RFC 0005 section 5.2) it evaluates a system description
    with when that directory exists. ``root`` is the directory activation treats
    as ``/``: the running machine by default, or a mounted target when an
    installer prepares one, in which case services are not started and
    file systems are not mounted.
    """

    description: str = DEFAULT_DESCRIPTION
    package_set: str = DEFAULT_PACKAGE_SET
    root: str = "/"

    @model_validator(mode="after")
    def _check_paths(self) -> Self:
        paths = (
            ("description", self.description),
            ("package_set", self.package_set),
            ("root", self.root),
        )
        for name, value in paths:
            if not os.path.isabs(value) or os.path.normpath(value) != value:
                raise ValueError(f"system {name} must be a normalised absolute path, not {value!r}")
        return self


class Settings(_SettingsModel):
    """What one machine uses to build and fetch store paths.

    ``sandbox_backend`` selects a backend from section 9.7.
    ``substituters`` lists binary cache URLs in the order they are tried and
    ``trusted_keys`` the public keys their signatures must come from
    (section 14.3). ``require_provenance`` rejects substituted paths without
    valid signed provenance (section 14.9). ``daemon`` is read only by
    ``lattice daemon``. ``sandbox`` holds the limits of every build and
    ``store`` the location of the store and ``cache`` how caches are
    published and read. ``system`` says where system configuration
    reads its description and which root it activates.
    """

    sandbox_backend: SandboxBackend = "namespaces"
    substituters: tuple[str, ...] = ()
    trusted_keys: tuple[str, ...] = ()
    require_provenance: bool = False
    sandbox: SandboxSettings = SandboxSettings()
    store: StoreSettings = StoreSettings()
    daemon: DaemonSettings = DaemonSettings()
    cache: CacheSettings = CacheSettings()
    system: SystemSettings = SystemSettings()

    @model_validator(mode="after")
    def _check_caches(self) -> Self:
        for url in self.substituters:
            check_cache_url(url)
        for key in self.trusted_keys:
            check_public_key(key)
        return self

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Settings:
        """Read the settings of a machine from the TOML file at ``path``.

        Top-level keys are the fields of Settings, the ``[sandbox]`` table
        holds SandboxSettings, with ``[sandbox.microvm]`` for
        MicroVMSettings, the ``[store]`` table StoreSettings and the
        ``[daemon]`` table DaemonSettings and the ``[cache]`` table
        CacheSettings, with ``[cache.shallow]`` for ShallowRules, and
        the ``[system]`` table SystemSettings.
        Grants are arrays of tables, written ``[[daemon.clients]]`` and
        ``[[daemon.tokens]]``. A key the model
        does not know is an error. A file that cannot be read, is not valid
        TOML or holds an invalid value raises SettingsError.
        """
        try:
            with open(path, "rb") as file:
                data = tomllib.load(file)
        except OSError as error:
            raise SettingsError(f"{os.fspath(path)}: {error.strerror or error}") from None
        except tomllib.TOMLDecodeError as error:
            raise SettingsError(f"{os.fspath(path)}: {error}") from None
        try:
            return parse(cls, data)
        except TypeError:
            # json.dumps refuses TOML dates and times, which no field accepts.
            raise SettingsError(f"{os.fspath(path)}: dates and times are not settings") from None
        except ValidationError as error:
            raise SettingsError(f"{os.fspath(path)}: {_settings_error(error)}") from None
