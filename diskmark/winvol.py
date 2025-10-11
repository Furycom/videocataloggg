from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = [
    "FILE_SUPPORTS_USN_JOURNAL",
    "VolumeInfo",
    "USNJournalInfo",
    "get_volume_info",
    "query_usn_journal",
]

FILE_SUPPORTS_USN_JOURNAL = 0x02000000


@dataclass(slots=True)
class VolumeInfo:
    """Snapshot of a Windows volume identity."""

    root_path: str
    label: Optional[str]
    volume_guid: Optional[str]
    volume_serial_hex: Optional[str]
    filesystem: Optional[str]
    flags: int
    is_network: bool

    @property
    def supports_usn(self) -> bool:
        return bool(self.flags & FILE_SUPPORTS_USN_JOURNAL) and not self.is_network


@dataclass(slots=True)
class USNJournalInfo:
    """Read-only summary of the NTFS Change Journal."""

    journal_id: int
    first_usn: int
    next_usn: int
    timestamp_utc: str


if os.name == "nt":  # pragma: win32-only
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    GetVolumePathNameW = kernel32.GetVolumePathNameW
    GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    GetVolumePathNameW.restype = wintypes.BOOL

    GetVolumeNameForVolumeMountPointW = kernel32.GetVolumeNameForVolumeMountPointW
    GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL

    GetVolumeInformationW = kernel32.GetVolumeInformationW
    GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    GetVolumeInformationW.restype = wintypes.BOOL

    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    DeviceIoControl = kernel32.DeviceIoControl
    DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    DeviceIoControl.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FSCTL_QUERY_USN_JOURNAL = 0x000900f4

    class USN_JOURNAL_DATA_V0(ctypes.Structure):
        _fields_ = [
            ("UsnJournalID", ctypes.c_uint64),
            ("FirstUsn", ctypes.c_int64),
            ("NextUsn", ctypes.c_int64),
            ("LowestValidUsn", ctypes.c_int64),
            ("MaxUsn", ctypes.c_int64),
            ("MaximumSize", ctypes.c_uint64),
            ("AllocationDelta", ctypes.c_uint64),
        ]
else:  # pragma: no cover - non-Windows fallback
    ctypes = None  # type: ignore[assignment]
    INVALID_HANDLE_VALUE = -1


def _ensure_trailing_backslash(path: str) -> str:
    if path.endswith("\\"):
        return path
    return path + "\\"


def _resolve_root(path: Path) -> str:
    resolved = Path(path).resolve()
    anchor = resolved.anchor or str(resolved)
    if not anchor:
        anchor = str(resolved)
    if os.name != "nt":
        return anchor or "/"
    buffer = None
    wide_path = str(resolved)
    if not wide_path:
        wide_path = str(path)
    try:  # pragma: win32-only
        if not wide_path.endswith("\\"):
            wide_path = wide_path + "\\"
        buffer = ctypes.create_unicode_buffer(512)
        if GetVolumePathNameW(wide_path, buffer, len(buffer)):
            return _ensure_trailing_backslash(buffer.value or wide_path)
    except Exception:
        return _ensure_trailing_backslash(anchor or wide_path)  # type: ignore[arg-type]
    return _ensure_trailing_backslash(anchor)


def get_volume_info(path: Path | str) -> VolumeInfo:
    """Return a best-effort :class:`VolumeInfo` for *path*."""

    candidate = Path(path)
    root_path = _resolve_root(candidate)
    is_network = root_path.startswith("\\\\")

    label = None
    volume_guid = None
    volume_serial_hex = None
    filesystem = None
    flags_value = 0

    if os.name == "nt":  # pragma: win32-only
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            if GetVolumeNameForVolumeMountPointW(root_path, buffer, len(buffer)):
                value = buffer.value
                if value:
                    volume_guid = _ensure_trailing_backslash(value)
        except Exception:
            volume_guid = None
        serial = wintypes.DWORD(0)
        max_component = wintypes.DWORD(0)
        flags = wintypes.DWORD(0)
        label_buffer = ctypes.create_unicode_buffer(261)
        fs_buffer = ctypes.create_unicode_buffer(261)
        try:
            success = GetVolumeInformationW(
                root_path,
                label_buffer,
                len(label_buffer),
                ctypes.byref(serial),
                ctypes.byref(max_component),
                ctypes.byref(flags),
                fs_buffer,
                len(fs_buffer),
            )
        except Exception:
            success = False
        if success:
            label = label_buffer.value or None
            filesystem = fs_buffer.value or None
            flags_value = int(flags.value)
            volume_serial_hex = f"{int(serial.value) & 0xFFFFFFFF:08X}"
        else:
            label = None
            filesystem = None
            flags_value = 0
            volume_serial_hex = None
    else:  # pragma: no cover - non-Windows fallback
        label = candidate.anchor or candidate.name or None
        filesystem = None
        volume_serial_hex = None
        flags_value = 0
        volume_guid = root_path

    return VolumeInfo(
        root_path=_ensure_trailing_backslash(root_path) if os.name == "nt" else root_path,
        label=label,
        volume_guid=volume_guid,
        volume_serial_hex=volume_serial_hex,
        filesystem=filesystem,
        flags=flags_value,
        is_network=is_network,
    )


def _open_volume_handle(volume_guid: str) -> Optional[int]:
    if os.name != "nt":  # pragma: no cover - non-Windows fallback
        return None
    path = volume_guid.rstrip("\\")
    handle = CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return None
    return handle


def query_usn_journal(volume_guid: Optional[str]) -> Optional[USNJournalInfo]:
    """Query the NTFS Change Journal for *volume_guid* in read-only mode."""

    if os.name != "nt":  # pragma: no cover - non-Windows fallback
        return None
    if not volume_guid:
        return None
    handle = _open_volume_handle(volume_guid)
    if handle is None:
        return None
    try:
        data = USN_JOURNAL_DATA_V0()
        bytes_returned = wintypes.DWORD(0)
        success = DeviceIoControl(
            handle,
            FSCTL_QUERY_USN_JOURNAL,
            None,
            0,
            ctypes.byref(data),
            ctypes.sizeof(data),
            ctypes.byref(bytes_returned),
            None,
        )
        if not success:
            return None
        return USNJournalInfo(
            journal_id=int(data.UsnJournalID),
            first_usn=int(data.FirstUsn),
            next_usn=int(data.NextUsn),
            timestamp_utc=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    finally:  # pragma: win32-only
        try:
            CloseHandle(handle)
        except Exception:
            pass
