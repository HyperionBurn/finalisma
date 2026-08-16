"""Small, Windows-safe helpers for subprocess-backed integration fixtures."""

from __future__ import annotations

import gc
import subprocess
import time
from typing import Any


def stop_subprocess(process: subprocess.Popen[Any] | None, *, close_stdin: bool = False) -> None:
    """Stop a fixture process and close the parent's pipe handles."""
    if process is None:
        return
    if close_stdin and process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def cleanup_tempdir(directory: Any, *, attempts: int = 20) -> None:
    """Retry Windows temp cleanup while terminated child handles settle."""
    last_error: PermissionError | None = None
    for _ in range(attempts):
        try:
            directory.cleanup()
            return
        except PermissionError as exc:
            last_error = exc
            gc.collect()
            time.sleep(0.1)
    if last_error is not None:
        raise last_error
