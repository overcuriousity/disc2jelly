"""Optical drive detection using OS facilities only — no external binary.

Replaces the MakeMKV ``DRV`` enumeration that used to live in disc.py.

Windows: kernel32 ``GetLogicalDrives`` / ``GetDriveTypeW`` (DRIVE_CDROM = 5) /
``GetVolumeInformationW``. The volume label doubles as the disc label, which is
what feeds the TMDb query.

Linux: ``/dev/sr*`` for the nodes, ``/sys/block/<name>/size`` for media presence
(0 sectors = empty tray), ``/dev/disk/by-label/`` symlinks for the label.

``list_drives`` never raises — a broken backend yields [] and the caller emits
the error event, matching the old disc.py contract.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path

DRIVE_CDROM = 5
_LABEL_BUF_LEN = 261  # MAX_PATH + 1, per GetVolumeInformationW docs

DEV_ROOT = Path("/dev")
SYS_BLOCK = Path("/sys/block")
BY_LABEL = Path("/dev/disk/by-label")


@dataclass
class Drive:
    device: str    # "D:\\" on Windows, "/dev/sr0" on Linux — passed to HandBrake -i
    label: str     # volume label of the inserted disc, "" if none
    has_disc: bool


# --- Linux -----------------------------------------------------------------


def _linux_labels(by_label: Path, devices: set[str]) -> dict[str, str]:
    """Reverse the by-label symlink farm into {resolved device path: label}."""
    out: dict[str, str] = {}
    try:
        entries = list(by_label.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            target = str(entry.resolve())
        except OSError:
            continue
        if target in devices:
            out[target] = entry.name
    return out


def _linux_has_media(sys_block: Path, name: str) -> bool:
    """A CD-ROM block device reports size 0 while the tray is empty."""
    try:
        return int((sys_block / name / "size").read_text().strip()) > 0
    except (OSError, ValueError):
        return False


def _list_drives_linux(
    dev_root: Path = DEV_ROOT,
    sys_block: Path = SYS_BLOCK,
    by_label: Path = BY_LABEL,
) -> list[Drive]:
    try:
        nodes = [p for p in dev_root.glob("sr*") if p.name[2:].isdigit()]
    except OSError:
        return []
    nodes.sort(key=lambda p: int(p.name[2:]))

    labels = _linux_labels(by_label, {str(p) for p in nodes})
    found: list[Drive] = []
    for node in nodes:
        has_disc = _linux_has_media(sys_block, node.name)
        found.append(
            Drive(
                device=str(node),
                label=labels.get(str(node), "") if has_disc else "",
                has_disc=has_disc,
            )
        )
    return found


# --- Windows ---------------------------------------------------------------


def _list_drives_windows(kernel32: object) -> list[Drive]:
    bits = kernel32.GetLogicalDrives()
    found: list[Drive] = []
    for i in range(26):
        if not bits & (1 << i):
            continue
        root = f"{chr(ord('A') + i)}:\\"
        if kernel32.GetDriveTypeW(root) != DRIVE_CDROM:
            continue
        name_buf = ctypes.create_unicode_buffer(_LABEL_BUF_LEN)
        fs_buf = ctypes.create_unicode_buffer(_LABEL_BUF_LEN)
        # Fails with ERROR_NOT_READY when the tray is empty — that is the
        # media-presence probe, not an error worth reporting.
        ok = kernel32.GetVolumeInformationW(
            root,
            name_buf,
            _LABEL_BUF_LEN,
            None,
            None,
            None,
            fs_buf,
            _LABEL_BUF_LEN,
        )
        found.append(
            Drive(
                device=root,
                label=name_buf.value if ok else "",
                has_disc=bool(ok),
            )
        )
    return found


def _windows_drives() -> list[Drive]:
    return _list_drives_windows(ctypes.windll.kernel32)  # type: ignore[attr-defined]


# --- dispatch ---------------------------------------------------------------


def list_drives() -> list[Drive]:
    """Enumerate optical drives. Returns [] rather than raising."""
    try:
        if sys.platform == "win32":
            return _windows_drives()
        return _list_drives_linux()
    except OSError:
        return []
