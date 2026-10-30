# SPDX-License-Identifier: GPL-2.0
"""Users and groups of a system (RFC 0004 section 8.2).

Activation writes ``/etc/passwd``, ``/etc/group`` and ``/etc/shadow``
under the system root from the records of the description:

- Each file is rewritten through a temporary file and a rename, so a
  reader sees the old file or the new one. ``shadow`` has mode 0600.
- Accounts that activation did not create, such as the ones
  systemd-sysusers adds, are kept as they are. Accounts it created that
  the description no longer lists are removed; their home directories
  and files are kept.
- ``root``, ``nobody`` and the groups ``root``, ``users`` and
  ``nogroup`` are created when they are missing.
- A user or group without a fixed number gets one from a range and keeps
  it: 1000 to 29999 for users and groups, 400 to 999 for system users.
  ``/var/lib/lattice/ids.json`` records the numbers given out and the
  accounts activation manages.
- The password hash of a user is read from its ``password_file`` under
  the root. A user without one, or whose file is missing, has a locked
  password.
- Missing home directories are created with mode 0700 and owned by the
  user, except for system users without an explicit ``home``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable

from pydantic import ValidationError

from ..errors import ActivationFailed
from ..models import Model
from .manager import SystemManager
from .spec import BUILTIN_GROUPS, BUILTIN_USERS, SystemSpec, UserSpec

IDS_FILE = "var/lib/lattice/ids.json"
USER_IDS = (1000, 29999)
SYSTEM_USER_IDS = (400, 999)
GROUP_IDS = (1000, 29999)
NOLOGIN = "/run/current-system/sw/bin/nologin"
ROOT_SHELL = "/run/current-system/sw/bin/sh"

Notice = Callable[[str], None]


class IdState(Model):
    """The content of ``ids.json``."""

    users: dict[str, int] = {}
    groups: dict[str, int] = {}
    managed_users: tuple[str, ...] = ()
    managed_groups: tuple[str, ...] = ()


def _path(root: str, relative: str) -> str:
    return os.path.join(root, relative.lstrip("/"))


def _read_table(path: str, fields: int) -> dict[str, list[str]]:
    """Return the lines of a colon separated file by their first field, in file order."""
    table: dict[str, list[str]] = {}
    try:
        with open(path, encoding="utf-8") as file:
            lines = file.read().splitlines()
    except FileNotFoundError:
        return table
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != fields:
            raise ActivationFailed(f"{path}: line {line.split(':')[0]!r} is not valid")
        table[parts[0]] = parts
    return table


def write_atomically(path: str, text: str, mode: int) -> None:
    """Replace ``path`` with ``text`` through a temporary file and a rename."""
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    temporary = os.path.join(os.path.dirname(path), f".{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.rename(temporary, path)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def _write_table(path: str, table: dict[str, list[str]], mode: int) -> None:
    write_atomically(path, "".join(":".join(parts) + "\n" for parts in table.values()), mode)


def read_ids(root: str) -> IdState:
    """Return the recorded numbers and managed accounts of the system at ``root``."""
    path = _path(root, IDS_FILE)
    try:
        with open(path, "rb") as file:
            return IdState.model_validate_json(file.read())
    except FileNotFoundError:
        return IdState()
    except ValidationError as error:
        raise ActivationFailed(f"{path} is not valid: {error}") from None


def _allocate(what: str, name: str, span: tuple[int, int], used: set[int]) -> int:
    for number in range(span[0], span[1] + 1):
        if number not in used:
            return number
    raise ActivationFailed(f"no free {what} number for {name!r} in {span[0]} to {span[1]}")


def _number(
    what: str,
    name: str,
    fixed: int | None,
    existing: int | None,
    recorded: int | None,
    span: tuple[int, int],
    owners: dict[int, str],
) -> int:
    """Choose the number of account ``name`` and claim it in ``owners``."""
    if fixed is not None:
        number = fixed
    elif existing is not None:
        number = existing
    elif recorded is not None and recorded not in owners:
        number = recorded
    else:
        number = _allocate(what, name, span, set(owners))
    owner = owners.get(number)
    if owner is not None and owner != name:
        raise ActivationFailed(f"{what} {number} of {name!r} is already used by {owner!r}")
    owners[number] = name
    return number


def _password(root: str, user: UserSpec, notice: Notice) -> str:
    if user.password_file is None:
        return "!"
    path = _path(root, user.password_file)
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read().strip()
    except OSError as error:
        notice(f"user {user.name}: cannot read {user.password_file} ({error.strerror}); locked")
        return "!"
    if not text or any(c in text for c in ":\n\r\0"):
        notice(f"user {user.name}: {user.password_file} is not one password hash; locked")
        return "!"
    return text


def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ActivationFailed(f"{what} {value!r} is not a number") from None


def write_accounts(root: str, spec: SystemSpec, manager: SystemManager, notice: Notice) -> None:
    """Bring the accounts under ``root`` in line with ``spec``.

    Raises ActivationFailed if a number is taken or a file is not valid.
    """
    etc = _path(root, "etc")
    passwd = _read_table(os.path.join(etc, "passwd"), 7)
    groups = _read_table(os.path.join(etc, "group"), 4)
    shadow = _read_table(os.path.join(etc, "shadow"), 9)
    state = read_ids(root)
    wanted_users = {u.name for u in spec.users}
    wanted_groups = {g.name for g in spec.groups}
    managed_users = set(state.managed_users) | wanted_users
    for name in set(state.managed_users) - wanted_users:
        passwd.pop(name, None)
        shadow.pop(name, None)
    for name in set(state.managed_groups) - wanted_groups:
        groups.pop(name, None)
    for name, gid in BUILTIN_GROUPS.items():
        groups.setdefault(name, [name, "x", str(gid), ""])
    for name, uid in BUILTIN_USERS.items():
        home, shell = ("/root", ROOT_SHELL) if name == "root" else ("/var/empty", NOLOGIN)
        gid = BUILTIN_GROUPS["root" if name == "root" else "nogroup"]
        passwd.setdefault(name, [name, "x", str(uid), str(gid), name, home, shell])
        shadow.setdefault(name, [name, "!", "", "", "", "", "", "", ""])

    group_owners = {
        _int(parts[2], "gid"): name for name, parts in groups.items() if name not in wanted_groups
    }
    gids: dict[str, int] = {}
    for group in spec.groups:
        existing = groups.get(group.name)
        gid = _number(
            "gid",
            group.name,
            group.gid,
            _int(existing[2], "gid") if existing else None,
            state.groups.get(group.name),
            GROUP_IDS,
            group_owners,
        )
        gids[group.name] = gid
        members = existing[3] if existing else ""
        groups[group.name] = [group.name, "x", str(gid), members]
    for name, parts in groups.items():
        gids.setdefault(name, _int(parts[2], "gid"))

    user_owners = {
        _int(parts[2], "uid"): name for name, parts in passwd.items() if name not in wanted_users
    }
    uids: dict[str, int] = {}
    for user in spec.users:
        existing = passwd.get(user.name)
        uid = _number(
            "uid",
            user.name,
            user.uid,
            _int(existing[2], "uid") if existing else None,
            state.users.get(user.name),
            SYSTEM_USER_IDS if user.system_user else USER_IDS,
            user_owners,
        )
        uids[user.name] = uid
        gecos = existing[4] if existing else user.name
        passwd[user.name] = [
            user.name,
            "x",
            str(uid),
            str(gids[user.group]),
            gecos,
            user.home_directory,
            user.shell,
        ]
        shadow[user.name] = [user.name, _password(root, user, notice), "", "", "", "", "", "", ""]

    for name, parts in groups.items():
        members = [m for m in parts[3].split(",") if m and m not in managed_users]
        members += sorted(u.name for u in spec.users if name in u.groups)
        parts[3] = ",".join(members)

    _write_table(os.path.join(etc, "group"), groups, 0o644)
    _write_table(os.path.join(etc, "passwd"), passwd, 0o644)
    _write_table(os.path.join(etc, "shadow"), shadow, 0o600)
    updated = IdState(
        users={**state.users, **uids},
        groups={**state.groups, **{g.name: gids[g.name] for g in spec.groups}},
        managed_users=tuple(sorted(wanted_users)),
        managed_groups=tuple(sorted(wanted_groups)),
    )
    write_atomically(_path(root, IDS_FILE), updated.model_dump_json(indent=2) + "\n", 0o644)

    for user in spec.users:
        if user.system_user and user.home is None:
            continue
        home = _path(root, user.home_directory)
        if not os.path.lexists(home):
            os.makedirs(os.path.dirname(home), mode=0o755, exist_ok=True)
            os.mkdir(home, 0o700)
            manager.chown(home, uids[user.name], gids[user.group])


def accounts_json(spec: SystemSpec) -> str:
    """Return the JSON of ``pyrixos/system-users.json`` for ``spec``."""
    accounts = {
        "users": [u.model_dump(mode="json") for u in spec.users],
        "groups": [g.model_dump(mode="json") for g in spec.groups],
    }
    return json.dumps(accounts, indent=2, sort_keys=True) + "\n"
