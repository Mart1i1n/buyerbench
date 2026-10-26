"""Atomic result persistence and process locking."""
import json
import os
import tempfile
from pathlib import Path


def write_result_atomic(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(row, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def acquire_lock(lock: Path) -> bool:
    """Acquire the task lock and clear stale locks left by crashed processes."""
    for _ in range(2):
        try:
            with lock.open("x", encoding="ascii") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                owner = int(lock.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                owner = None
            if owner is not None and process_exists(owner):
                return False
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
    return False


def process_exists(pid: int) -> bool:
    """Probe the PID without sending a terminating Windows signal."""
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

