"""Run a command with Linux Landlock restricting filesystem mutations.

Reads and program execution remain unrestricted, while writes, removals, and
new filesystem objects are permitted only beneath the supplied directory.
This small launcher keeps the Landlock setup out of ``subprocess``'s unsafe
``preexec_fn`` path in the multi-threaded worker process.
"""

import ctypes
import os
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


def restrict_writes(allowed_dir: Path) -> None:
    """Restrict this process and its descendants to writes under *allowed_dir*.

    The operation fails closed when Landlock is unavailable. Callers must not
    run an untrusted extractor after an exception from this function.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _syscall(
        libc,
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )

    access = _WRITE_ACCESS_ABI_1
    if abi >= 2:
        access |= 1 << 13  # REFER (cross-directory rename/link)
    if abi >= 3:
        access |= 1 << 14  # TRUNCATE

    ruleset_attr = _RulesetAttr(access)
    ruleset_fd = _syscall(
        libc,
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    allowed_fd = -1
    try:
        allowed_fd = os.open(allowed_dir, os.O_PATH | os.O_CLOEXEC)
        path_attr = _PathBeneathAttr(access, allowed_fd)
        _syscall(
            libc,
            _LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(path_attr),
            0,
        )
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _syscall(libc, _LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        if allowed_fd >= 0:
            os.close(allowed_fd)
        os.close(ruleset_fd)


def main(argv: list[str]) -> int:
    """Apply the sandbox, then replace this process with the requested tool."""
    if len(argv) < 3:
        print(f"Usage: {argv[0]} ALLOWED_DIR COMMAND [ARG ...]", file=sys.stderr)
        return 2

    allowed_dir = Path(argv[1]).resolve(strict=True)
    try:
        restrict_writes(allowed_dir)
        os.execvp(argv[2], argv[2:])
    except OSError as exc:
        print(f"write sandbox could not run {argv[2]!r}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

# vim: ts=4 sw=4 et
