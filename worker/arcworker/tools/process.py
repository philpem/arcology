"""The worker's single subprocess launch point, with mandatory Landlock policy."""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path


@contextlib.contextmanager
def sandboxed_process(
    cmd: list[str], *, write_dirs: Sequence[Path], cwd: str | Path | None = None,
) -> Iterator[subprocess.Popen]:
    """Launch a confined tool with private scratch; stop its group before cleanup.

    ``write_dirs=()`` permits only scratch and /dev/null writes. Output paths
    are trusted caller decisions, independent of the executable and cwd.
    Resolving the executable before launch preserves FileNotFoundError fallbacks.
    """
    executable = shutil.which(cmd[0])
    if executable is None:
        raise FileNotFoundError(2, 'External tool not found', cmd[0])
    # Relative executable paths must retain their meaning when cwd changes.
    executable = str(Path(executable).absolute())
    directories = tuple(Path(path).resolve(strict=True) for path in write_dirs)
    if any(not path.is_dir() for path in directories):
        raise NotADirectoryError('Sandbox write roots must be directories')
    launcher = Path(__file__).with_name('write_sandbox.py')
    with tempfile.TemporaryDirectory(prefix='arcology-tool-') as scratch:
        env = os.environ.copy()
        env.update({
            'TMPDIR': scratch, 'TMP': scratch, 'TEMP': scratch, 'HOME': scratch,
            'XDG_CACHE_HOME': scratch, 'XDG_CONFIG_HOME': scratch,
            'XDG_DATA_HOME': scratch, 'XDG_RUNTIME_DIR': scratch,
            'MAGICK_TEMPORARY_PATH': scratch, 'PYTHONDONTWRITEBYTECODE': '1',
        })
        # Java does not honour TMPDIR. Avoid hsperfdata in the shared /tmp.
        env['JAVA_TOOL_OPTIONS'] = (
            env.get('JAVA_TOOL_OPTIONS', '') + f' -Djava.io.tmpdir={scratch} -XX:-UsePerfData'
        ).strip()
        wrapped = [sys.executable, str(launcher), '--allow-dev-null']
        for directory in (*directories, Path(scratch)):
            wrapped.extend(['--write-dir', str(directory)])
        wrapped.extend(['--', executable, *cmd[1:]])
        proc = subprocess.Popen(
            wrapped, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
            env=env, start_new_session=True,
        )
        try:
            yield proc
        finally:
            # A shell launcher or delegate may survive the immediate child.
            # Kill the process group on success, failure, cancellation and timeout.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()


def check_sandbox() -> None:
    """Fail at worker startup if the kernel/seccomp cannot enforce the policy."""
    with sandboxed_process([sys.executable, '-c', 'pass'], write_dirs=()) as proc:
        _, stderr = proc.communicate(timeout=10)
        if proc.returncode:
            raise RuntimeError(
                'Worker requires Linux Landlock ABI >= 3 and permitted Landlock syscalls: '
                + stderr.decode(errors='replace').strip()
            )
