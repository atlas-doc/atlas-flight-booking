"""Cross-platform durable atomic file replacement."""

from __future__ import annotations

import os
from pathlib import Path

MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8


def durable_replace(source: Path, destination: Path) -> None:
    """Atomically replace destination and durably record the directory update."""
    if os.name == "nt":
        _replace_windows(source, destination)
    else:
        _replace_posix(source, destination)


def _replace_posix(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _replace_windows(source: Path, destination: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file_ex.restype = ctypes.c_int
    flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    if not move_file_ex(str(source), str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
