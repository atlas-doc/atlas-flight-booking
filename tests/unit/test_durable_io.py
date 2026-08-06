import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlas_cli.durable_io import (
    MOVEFILE_REPLACE_EXISTING,
    MOVEFILE_WRITE_THROUGH,
    durable_replace,
)


def test_posix_replace_is_atomic_and_directory_durable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    events: list[str] = []
    directory_fd = 17

    monkeypatch.setattr("atlas_cli.durable_io.os.name", "posix")

    def replace(actual_source: Path, actual_destination: Path) -> None:
        assert (actual_source, actual_destination) == (source, destination)
        events.append("replace")

    def open_directory(directory: Path, flags: int) -> int:
        assert directory == destination.parent
        assert flags == os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        events.append("open-directory")
        return directory_fd

    def fsync_directory(actual_fd: int) -> None:
        assert actual_fd == directory_fd
        events.append("fsync-directory")

    def close_directory(actual_fd: int) -> None:
        assert actual_fd == directory_fd
        events.append("close-directory")

    monkeypatch.setattr("atlas_cli.durable_io.os.replace", replace)
    monkeypatch.setattr("atlas_cli.durable_io.os.open", open_directory)
    monkeypatch.setattr("atlas_cli.durable_io.os.fsync", fsync_directory)
    monkeypatch.setattr("atlas_cli.durable_io.os.close", close_directory)

    durable_replace(source, destination)

    assert events == ["replace", "open-directory", "fsync-directory", "close-directory"]


def test_posix_replace_closes_directory_when_fsync_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []
    directory_fd = 23
    monkeypatch.setattr("atlas_cli.durable_io.os.name", "posix")
    monkeypatch.setattr("atlas_cli.durable_io.os.replace", lambda source, destination: events.append("replace"))
    monkeypatch.setattr("atlas_cli.durable_io.os.open", lambda directory, flags: events.append("open") or directory_fd)

    def fail_fsync(actual_fd: int) -> None:
        assert actual_fd == directory_fd
        events.append("fsync")
        raise OSError("private-fsync-failure")

    def close_directory(actual_fd: int) -> None:
        assert actual_fd == directory_fd
        events.append("close")

    monkeypatch.setattr("atlas_cli.durable_io.os.fsync", fail_fsync)
    monkeypatch.setattr("atlas_cli.durable_io.os.close", close_directory)

    with pytest.raises(OSError, match="private-fsync-failure"):
        durable_replace(tmp_path / "state.tmp", tmp_path / "state.json")

    assert events == ["replace", "open", "fsync", "close"]


def test_posix_replace_moves_real_file_contents(tmp_path: Path) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new-state", encoding="utf-8")
    destination.write_text("old-state", encoding="utf-8")

    durable_replace(source, destination)

    assert destination.read_text(encoding="utf-8") == "new-state"
    assert not source.exists()


def test_windows_replace_uses_replace_existing_and_write_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    seen_flags: list[int] = []
    dll_calls: list[tuple[str, bool]] = []
    c_wchar_p = object()
    c_uint32 = object()
    c_int = object()

    class MoveFileExRecorder:
        argtypes: list[object] | None = None
        restype: object | None = None

        def __call__(self, actual_source: str, actual_destination: str, flags: int) -> bool:
            assert (actual_source, actual_destination) == (str(source), str(destination))
            assert self.argtypes == [c_wchar_p, c_wchar_p, c_uint32]
            assert self.restype is c_int
            seen_flags.append(flags)
            return True

    move_file_ex = MoveFileExRecorder()

    def load_dll(library: str, *, use_last_error: bool) -> SimpleNamespace:
        dll_calls.append((library, use_last_error))
        return SimpleNamespace(MoveFileExW=move_file_ex)

    fake_ctypes = SimpleNamespace(
        WinDLL=load_dll,
        c_wchar_p=c_wchar_p,
        c_uint32=c_uint32,
        c_int=c_int,
        get_last_error=lambda: 0,
        WinError=lambda error_code: OSError(error_code, "replacement failed"),
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr("atlas_cli.durable_io.os.name", "nt")

    durable_replace(source, destination)

    assert dll_calls == [("kernel32", True)]
    assert seen_flags == [MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH]


def test_windows_replace_failure_raises_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    dll_calls: list[tuple[str, bool]] = []
    win_error_codes: list[int] = []
    captured_error_code = 1234

    class FailedMoveFileEx:
        argtypes: list[object] | None = None
        restype: object | None = None

        def __call__(self, actual_source: str, actual_destination: str, flags: int) -> bool:
            assert (actual_source, actual_destination) == (str(source), str(destination))
            assert flags == MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            return False

    failed_move_file_ex = FailedMoveFileEx()

    def load_dll(library: str, *, use_last_error: bool) -> SimpleNamespace:
        dll_calls.append((library, use_last_error))
        return SimpleNamespace(MoveFileExW=failed_move_file_ex)

    def make_windows_error(error_code: int) -> OSError:
        win_error_codes.append(error_code)
        return OSError(error_code, "replacement failed")

    fake_ctypes: Any = SimpleNamespace(
        WinDLL=load_dll,
        c_wchar_p=object(),
        c_uint32=object(),
        c_int=object(),
        get_last_error=lambda: captured_error_code,
        WinError=make_windows_error,
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr("atlas_cli.durable_io.os.name", "nt")

    with pytest.raises(OSError, match="replacement failed"):
        durable_replace(source, destination)

    assert dll_calls == [("kernel32", True)]
    assert win_error_codes == [captured_error_code]
