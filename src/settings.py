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

import os
import tomllib
from typing import TYPE_CHECKING, Any, Literal, Self, get_args

from pydantic import Field, ValidationError, model_validator

from .errors import SettingsError
from .models import Model, Role, parse

SandboxBackend = Literal["namespaces", "oci", "microvm"]
SANDBOX_BACKENDS: tuple[str, ...] = get_args(SandboxBackend)

DEFAULT_SOCKET = "/run/lattice/daemon.sock"
DEFAULT_SETTINGS_PATH = "/etc/lattice/settings.toml"
DEFAULT_STORE_ROOT = "/lattice"


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


class SandboxSettings(_SettingsModel):
    """Limits the build sandbox applies to every build (section 9.4).

    ``timeout`` ends a build that runs longer than that many seconds, and
    ``max_silent_time`` one that writes no log output for that many
    seconds. None means no limit. ``max_open_files`` is the builder's
    ``RLIMIT_NOFILE``. None of these is part of a derivation, so changing
    them changes no hash.
    """

    timeout: int | None = Field(default=None, gt=0)
    max_silent_time: int | None = Field(default=None, gt=0)
    max_open_files: int = Field(default=1024, ge=64, le=1048576)


class StoreSettings(_SettingsModel):
    """Where the store is kept and how many builds run at once (sections 10 to 12).

    ``root`` holds ``store``, ``staging`` and ``var``. Store paths keep
    their names under ``/lattice/store`` wherever ``root`` is, so a store
    under another root is for testing and for preparing a store that is
    later moved into place. ``max_jobs`` is the number of builds that may
    run in parallel.
    """

    root: str = DEFAULT_STORE_ROOT
    max_jobs: int = Field(default=1, ge=1, le=256)

    @model_validator(mode="after")
    def _check_root(self) -> Self:
        if not os.path.isabs(self.root) or os.path.normpath(self.root) != self.root:
            raise ValueError(f"store root must be a normalised absolute path, not {self.root!r}")
        return self


class Settings(_SettingsModel):
    """What one machine uses to build and fetch store paths.

    ``sandbox_backend`` selects a backend from section 9.7.
    ``substituters`` lists binary cache URLs in the order they are tried and
    ``trusted_keys`` the public keys their signatures must come from
    (section 14.3). ``require_provenance`` rejects substituted paths without
    valid signed provenance (section 14.9). ``daemon`` is read only by
    ``lattice daemon``. ``sandbox`` holds the limits of every build and
    ``store`` the location of the store.
    """

    sandbox_backend: SandboxBackend = "namespaces"
    substituters: tuple[str, ...] = ()
    trusted_keys: tuple[str, ...] = ()
    require_provenance: bool = False
    sandbox: SandboxSettings = SandboxSettings()
    store: StoreSettings = StoreSettings()
    daemon: DaemonSettings = DaemonSettings()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Settings:
        """Read the settings of a machine from the TOML file at ``path``.

        Top-level keys are the fields of Settings, the ``[sandbox]`` table
        holds SandboxSettings, the ``[store]`` table StoreSettings and the
        ``[daemon]`` table DaemonSettings.
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
