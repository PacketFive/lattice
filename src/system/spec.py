# SPDX-License-Identifier: GPL-2.0
"""The records of a system description (RFC 0004 section 5.1).

The standard library functions ``user``, ``group``, ``file_system``,
``service`` and ``etc_file`` validate their arguments with the models below
and return the result as a read-only mapping. ``machine`` validates the
whole description as a SystemSpec, which checks that the records fit
together, and stores it as canonical JSON in the derivation it returns.
``builtin:system`` and activation read the same models back, so the
description, the builder and activation agree on every field.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Self

from pydantic import Field, ValidationError, field_validator, model_validator

from ..derivation import STORE_DIR
from ..models import Model

LOGIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
UNIT_NAME = re.compile(r"[A-Za-z0-9_.:@-]+")
UNIT_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*")
RESTART = re.compile(r"[a-z]+(-[a-z]+)*")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
TIME_ZONE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")
STORE_REFERENCE = re.compile(re.escape(STORE_DIR) + r"/[a-z2-7]{32}-[A-Za-z0-9+._?=-]+")
ETC_MODES = ("0444", "0555")

BUILTIN_USERS = {"root": 0, "nobody": 65534}
"""Accounts activation creates if they are missing, with their uid."""
BUILTIN_GROUPS = {"root": 0, "users": 100, "nogroup": 65534}
"""Groups activation creates if they are missing, with their gid."""

GENERATED_ETC = (
    "fstab",
    "hostname",
    "localtime",
    "locale.conf",
    "profile",
    "ld.so.conf.d/lattice-stacks.conf",
    "pyrixos/system-users.json",
)
"""Files under /etc that builtin:system writes itself (section 7)."""
RESERVED_ETC = ("passwd", "group", "shadow", "static", "systemd/system")
"""Paths under /etc that a description may not define with etc_file."""


def _one_line(value: str, what: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"{what} must be one line")
    return value


def _absolute(value: str, what: str) -> str:
    if not value.startswith("/") or posixpath.normpath(value) != value:
        raise ValueError(f"{what} {value!r} must be a normalised absolute path")
    return _one_line(value, what)


def _outside_store(value: str, what: str) -> str:
    if value == STORE_DIR or value.startswith(STORE_DIR + "/"):
        raise ValueError(f"{what} {value!r} must not be in the store, which every user can read")
    return value


def _login_name(value: str, what: str) -> str:
    if not LOGIN_NAME.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid {what} name")
    return value


class UserSpec(Model):
    """A user account (``user``)."""

    kind: str = "user"
    name: str
    uid: int | None = Field(default=None, ge=0, le=65534)
    group: str = "users"
    groups: tuple[str, ...] = ()
    home: str | None = None
    shell: str = "/run/current-system/sw/bin/sh"
    password_file: str | None = None
    system_user: bool = False

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _login_name(value, "user")

    @field_validator("group")
    @classmethod
    def _check_group(cls, value: str) -> str:
        return _login_name(value, "group")

    @field_validator("groups")
    @classmethod
    def _check_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _login_name(name, "group")
        if len(set(value)) != len(value):
            raise ValueError("groups must not repeat")
        return value

    @field_validator("shell")
    @classmethod
    def _check_shell(cls, value: str) -> str:
        return _absolute(value, "shell")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind != "user":
            raise ValueError("kind must be 'user'")
        if self.home is not None:
            _absolute(self.home, "home")
        if self.password_file is not None:
            _outside_store(_absolute(self.password_file, "password_file"), "password_file")
        return self

    @property
    def home_directory(self) -> str:
        """``home``, or ``/home/<name>``, or ``/var/empty`` for a system user."""
        if self.home is not None:
            return self.home
        return "/var/empty" if self.system_user else f"/home/{self.name}"


class GroupSpec(Model):
    """A group (``group``)."""

    kind: str = "group"
    name: str
    gid: int | None = Field(default=None, ge=0, le=65534)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind != "group":
            raise ValueError("kind must be 'group'")
        _login_name(self.name, "group")
        return self


class FileSystemSpec(Model):
    """One mount (``file_system``)."""

    kind: str = "file_system"
    mount_point: str
    device: str
    fs_type: str = "auto"
    options: tuple[str, ...] = ("defaults",)
    needed_for_boot: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind != "file_system":
            raise ValueError("kind must be 'file_system'")
        _absolute(self.mount_point, "mount_point")
        for what, value in (("device", self.device), ("fs_type", self.fs_type)):
            if not value or any(c.isspace() for c in value) or "\0" in value:
                raise ValueError(f"{what} {value!r} must be non-empty without spaces")
        if not self.options:
            raise ValueError("options must not be empty")
        for option in self.options:
            if not option or "," in option or any(c.isspace() for c in option):
                raise ValueError(f"option {option!r} must be non-empty without ',' or spaces")
        return self

    @property
    def fstab_line(self) -> str:
        """The line of ``/etc/fstab`` for this mount."""
        check = 1 if self.mount_point == "/" else 2 if self.fs_type.startswith("ext") else 0
        options = ",".join(self.options)
        return f"{self.device} {self.mount_point} {self.fs_type} {options} 0 {check}"


def _unit_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


class ServiceSpec(Model):
    """A systemd service (``service``)."""

    kind: str = "service"
    name: str
    exec_start: str
    user: str = "root"
    after: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    environment: dict[str, str] = {}
    restart: str = "on-failure"
    unit: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind != "service":
            raise ValueError("kind must be 'service'")
        for unit in (self.name, *self.after, *self.wants):
            if not UNIT_NAME.fullmatch(unit):
                raise ValueError(f"{unit!r} is not a valid unit name")
        _login_name(self.user, "user")
        _one_line(self.exec_start, "exec_start")
        if not STORE_REFERENCE.match(self.exec_start):
            raise ValueError(f"exec_start must start with a store path, not {self.exec_start!r}")
        if not RESTART.fullmatch(self.restart):
            raise ValueError(f"restart {self.restart!r} is not a restart policy")
        for key, value in self.environment.items():
            if not ENV_NAME.fullmatch(key):
                raise ValueError(f"environment name {key!r} is not valid")
            _one_line(value, f"environment {key}")
        for key, value in self.unit.items():
            if not UNIT_KEY.fullmatch(key):
                raise ValueError(f"unit key {key!r} is not valid")
            _one_line(value, f"unit {key}")
        return self

    @property
    def unit_name(self) -> str:
        """The name of the unit file, ``<name>.service``."""
        return f"{self.name}.service"

    def unit_text(self) -> str:
        """Return the text of the unit file."""
        lines = ["[Unit]", f"Description={self.name}"]
        lines += [f"After={unit}" for unit in self.after]
        lines += [f"Wants={unit}" for unit in self.wants]
        lines += ["", "[Service]", f"ExecStart={self.exec_start}", f"User={self.user}"]
        lines.append(f"Restart={self.restart}")
        lines += [
            f'Environment="{key}={_unit_quote(value)}"'
            for key, value in sorted(self.environment.items())
        ]
        lines += [f"{key}={value}" for key, value in sorted(self.unit.items())]
        lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
        return "\n".join(lines)


class EtcFileSpec(Model):
    """One file under ``/etc`` (``etc_file``).

    ``source`` is the store path of a derivation's output.
    """

    kind: str = "etc_file"
    path: str
    text: str | None = None
    source: str | None = None
    mode: str = "0444"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind != "etc_file":
            raise ValueError("kind must be 'etc_file'")
        path = self.path
        if (
            not path
            or path.startswith("/")
            or posixpath.normpath(path) != path
            or path.split("/")[0] == ".."
            or "\0" in path
            or "\n" in path
        ):
            raise ValueError(f"etc_file path {path!r} must be a normalised path inside /etc")
        if path in GENERATED_ETC or any(
            path == reserved or path.startswith(reserved + "/") for reserved in RESERVED_ETC
        ):
            raise ValueError(f"/etc/{path} is written by Lattice")
        if (self.text is None) == (self.source is None):
            raise ValueError(f"etc_file {path!r} needs either text or source")
        if self.source is not None and not STORE_REFERENCE.fullmatch(self.source):
            raise ValueError(f"etc_file {path!r} source must be a derivation")
        if self.mode not in ETC_MODES:
            raise ValueError(f"etc_file mode must be one of {', '.join(ETC_MODES)}")
        return self


def _unique(what: str, values: list[object]) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{what} {value!r} is defined twice")
        seen.add(value)


def _check_layered_etc(paths: list[str]) -> None:
    files = set(paths)
    for path in paths:
        parent = posixpath.dirname(path)
        while parent:
            if parent in files:
                raise ValueError(f"etc_file {parent!r} is also a directory of {path!r}")
            parent = posixpath.dirname(parent)


class SystemSpec(Model):
    """A whole system description, as ``machine`` records it.

    ``kernel``, ``initrd`` and ``sw`` are store paths of the derivations
    ``machine`` received or made. ``config`` is the ``options`` mapping of
    the ``config.pyb`` next to the description, so changing an option
    changes the system.
    """

    name: str
    host_name: str = "pyrixos"
    time_zone: str = "UTC"
    kernel: str | None = None
    initrd: str | None = None
    kernel_params: tuple[str, ...] = ()
    sw: str
    users: tuple[UserSpec, ...] = ()
    groups: tuple[GroupSpec, ...] = ()
    file_systems: tuple[FileSystemSpec, ...] = ()
    services: tuple[ServiceSpec, ...] = ()
    etc: tuple[EtcFileSpec, ...] = ()
    config: dict[str, object] = {}

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not HOST_NAME.fullmatch(self.host_name):
            raise ValueError(f"host_name {self.host_name!r} is not a valid host name")
        if not TIME_ZONE.fullmatch(self.time_zone):
            raise ValueError(f"time_zone {self.time_zone!r} is not a zone name")
        for param in self.kernel_params:
            if not param or any(c.isspace() for c in param):
                raise ValueError(f"kernel parameter {param!r} must be non-empty without spaces")
        _unique("user", [u.name for u in self.users])
        _unique("uid", [u.uid for u in self.users if u.uid is not None])
        _unique("group", [g.name for g in self.groups])
        _unique("gid", [g.gid for g in self.groups if g.gid is not None])
        _unique("mount point", [f.mount_point for f in self.file_systems])
        _unique("service", [s.name for s in self.services])
        _unique("etc_file", [e.path for e in self.etc])
        for user in self.users:
            if user.name in BUILTIN_USERS:
                raise ValueError(f"user {user.name!r} is created by Lattice")
        for group in self.groups:
            if group.name in BUILTIN_GROUPS:
                raise ValueError(f"group {group.name!r} is created by Lattice")
        known_groups = {g.name for g in self.groups} | set(BUILTIN_GROUPS)
        for user in self.users:
            for name in (user.group, *user.groups):
                if name not in known_groups:
                    raise ValueError(
                        f"user {user.name!r} is in group {name!r}, which is not defined"
                    )
        known_users = {u.name for u in self.users} | set(BUILTIN_USERS)
        for service in self.services:
            if service.user not in known_users:
                raise ValueError(
                    f"service {service.name!r} runs as {service.user!r}, who is not defined"
                )
        _check_layered_etc([e.path for e in self.etc])
        return self

    def canonical_json(self) -> str:
        """Serialise with sorted keys and no whitespace, as ``machine`` stores it."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def root_file_system(self) -> FileSystemSpec | None:
        """The file system mounted at ``/``, if the description has one."""
        return next((f for f in self.file_systems if f.mount_point == "/"), None)


def describe(error: ValidationError) -> str:
    """Return the first problem of ``error`` as one short sentence."""
    first = error.errors()[0]
    message = str(first.get("msg", "is not valid")).removeprefix("Value error, ")
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {message}" if location else message
