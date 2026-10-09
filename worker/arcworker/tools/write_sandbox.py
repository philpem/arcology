"""Run a command with Linux Landlock restricting filesystem write operations.

Reads and program execution remain unrestricted. File-content writes,
truncation, removal, rename, link and creation are confined to supplied roots;
metadata operations such as chmod are outside this policy.
This small launcher keeps the Landlock setup out of ``subprocess``'s unsafe
``preexec_fn`` path in the multi-threaded worker process.
"""

import argparse
import ctypes
import errno
import os
import platform
import sys
from pathlib import Path

# Landlock uses the generic Linux syscall numbers on all architectures
# supported by the worker image (amd64 and arm64).
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1
_PR_SET_NO_NEW_PRIVS = 38

# Mutation rights introduced in Landlock ABI 1.
_WRITE_ACCESS_ABI_1 = (
    (1 << 1)  # WRITE_FILE
    | (1 << 4)  # REMOVE_DIR
    | (1 << 5)  # REMOVE_FILE
    | (1 << 6)  # MAKE_CHAR
    | (1 << 7)  # MAKE_DIR
    | (1 << 8)  # MAKE_REG
    | (1 << 9)  # MAKE_SOCK
    | (1 << 10)  # MAKE_FIFO
    | (1 << 11)  # MAKE_BLOCK
    | (1 << 12)  # MAKE_SYM
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    """Call a Landlock syscall and raise an OSError with its real errno."""
    result = libc.syscall(number, *args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def restrict_writes(allowed_dirs: tuple[Path, ...], *, allow_dev_null: bool = False) -> None:
    """Restrict this process and descendants to the supplied write directories.

    The operation fails closed when Landlock is unavailable. Callers must not
    run an untrusted extractor after an exception from this function.
    """
    if platform.machine() not in ('x86_64', 'aarch64'):
        raise OSError(errno.ENOSYS, 'Landlock launcher supports only amd64 and arm64')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = _syscall(
        libc,
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )

    if abi < 3:
        raise OSError(errno.ENOSYS, f'Landlock ABI 3 or newer required; kernel provides ABI {abi}')
    access = _WRITE_ACCESS_ABI_1 | (1 << 13) | (1 << 14)  # REFER, TRUNCATE
    # Handle device/FIFO/socket creation so it is denied even inside outputs.
    directory_access = access & ~((1 << 6) | (1 << 9) | (1 << 10) | (1 << 11))

    ruleset_attr = _RulesetAttr(access)
    ruleset_fd = _syscall(
        libc,
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    try:
        paths = [(directory, directory_access) for directory in allowed_dirs]
        if allow_dev_null:
            paths.append((Path('/dev/null'), 1 << 1))  # WRITE_FILE only
        for path, rights in paths:
            allowed_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                path_attr = _PathBeneathAttr(rights, allowed_fd)
                _syscall(
                    libc, _LANDLOCK_ADD_RULE, ruleset_fd,
                    _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(path_attr), 0,
                )
            finally:
                os.close(allowed_fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _syscall(libc, _LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def main(argv: list[str]) -> int:
    """Apply the sandbox, then replace this process with the requested tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-dir', action='append', default=[])
    parser.add_argument('--allow-dev-null', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv[1:])
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('a command is required after --')
    try:
        directories = tuple(Path(path).resolve(strict=True) for path in args.write_dir)
        if any(not path.is_dir() for path in directories):
            raise OSError(errno.ENOTDIR, 'write roots must be directories')
        restrict_writes(directories, allow_dev_null=args.allow_dev_null)
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"write sandbox could not run {command[0]!r}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

# vim: ts=4 sw=4 et
