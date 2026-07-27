"""Tests for disc2jelly.app.drives (OS-level optical drive detection).

Windows uses a fake kernel32 object; Linux uses a temp-dir fake of /dev,
/sys/block and /dev/disk/by-label. No MakeMKV, no subprocess.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from app import drives

DRIVE_CDROM = 5
DRIVE_FIXED = 3


# --- Linux -----------------------------------------------------------------


def _linux_tree(
    tmp_path: Path, devices: dict[str, tuple[int, str | None]]
) -> tuple[Path, Path, Path]:
    """Build a fake /dev, /sys/block and /dev/disk/by-label.

    ``devices`` maps "sr0" -> (sysfs size in 512-byte sectors, volume label).
    A size of 0 means an empty tray; a label of None means unlabelled media.
    """
    dev_root = tmp_path / "dev"
    sys_block = tmp_path / "sys" / "block"
    by_label = tmp_path / "dev" / "disk" / "by-label"
    for d in (dev_root, sys_block, by_label):
        d.mkdir(parents=True, exist_ok=True)

    for name, (size, label) in devices.items():
        node = dev_root / name
        node.touch()
        (sys_block / name).mkdir()
        (sys_block / name / "size").write_text(f"{size}\n")
        if label is not None:
            (by_label / label).symlink_to(node)
    return dev_root, sys_block, by_label


def test_linux_lists_drive_with_disc_and_label(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {"sr0": (4_600_000, "THE_MATRIX_16X9")})
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert len(found) == 1
    assert found[0].device == str(dev / "sr0")
    assert found[0].label == "THE_MATRIX_16X9"
    assert found[0].has_disc is True


def test_linux_empty_tray_has_no_disc_and_no_label(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {"sr0": (0, None)})
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert len(found) == 1
    assert found[0].has_disc is False
    assert found[0].label == ""


def test_linux_unlabelled_disc_still_reports_present(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {"sr0": (4_600_000, None)})
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert found[0].has_disc is True
    assert found[0].label == ""


def test_linux_multiple_drives_sorted_by_name(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(
        tmp_path,
        {"sr1": (4_600_000, "DISC_B"), "sr0": (0, None), "sr10": (100, "DISC_C")},
    )
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert [Path(d.device).name for d in found] == ["sr0", "sr1", "sr10"]


def test_linux_no_optical_drives_returns_empty(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {})
    assert drives._list_drives_linux(dev, sysb, bylabel) == []


def test_linux_missing_sysfs_size_is_treated_as_empty(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {"sr0": (4_600_000, "X")})
    (sysb / "sr0" / "size").unlink()
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert found[0].has_disc is False


def test_linux_by_label_pointing_elsewhere_is_ignored(tmp_path: Path) -> None:
    dev, sysb, bylabel = _linux_tree(tmp_path, {"sr0": (4_600_000, None)})
    other = dev / "sda1"
    other.touch()
    (bylabel / "MY_USB_STICK").symlink_to(other)
    found = drives._list_drives_linux(dev, sysb, bylabel)
    assert found[0].label == ""


# --- Windows ---------------------------------------------------------------


class _FakeKernel32:
    """Minimal stand-in for ctypes.windll.kernel32."""

    def __init__(self, drive_bits: int, types: dict[str, int], labels: dict[str, str]):
        self._bits = drive_bits
        self._types = types
        self._labels = labels
        self.volume_calls: list[str] = []

    def GetLogicalDrives(self) -> int:  # noqa: N802 - mirrors the Win32 name
        return self._bits

    def GetDriveTypeW(self, root: str) -> int:  # noqa: N802
        return self._types.get(root, 0)

    def GetVolumeInformationW(  # noqa: N802, PLR0913
        self, root, name_buf, name_size, serial, max_comp, flags, fs_buf, fs_size
    ) -> int:
        self.volume_calls.append(root)
        label = self._labels.get(root)
        if label is None:
            return 0
        name_buf.value = label
        return 1


def _bits(*letters: str) -> int:
    return sum(1 << (ord(c) - ord("A")) for c in letters)


def test_windows_lists_only_cdrom_drives() -> None:
    k32 = _FakeKernel32(
        drive_bits=_bits("C", "D"),
        types={"C:\\": DRIVE_FIXED, "D:\\": DRIVE_CDROM},
        labels={"D:\\": "BREAKINGBADS1"},
    )
    found = drives._list_drives_windows(k32)
    assert len(found) == 1
    assert found[0].device == "D:\\"
    assert found[0].label == "BREAKINGBADS1"
    assert found[0].has_disc is True


def test_windows_empty_tray_reports_no_disc() -> None:
    k32 = _FakeKernel32(
        drive_bits=_bits("E"), types={"E:\\": DRIVE_CDROM}, labels={}
    )
    found = drives._list_drives_windows(k32)
    assert len(found) == 1
    assert found[0].has_disc is False
    assert found[0].label == ""


def test_windows_no_optical_drives_returns_empty() -> None:
    k32 = _FakeKernel32(drive_bits=_bits("C"), types={"C:\\": DRIVE_FIXED}, labels={})
    assert drives._list_drives_windows(k32) == []


def test_windows_passes_a_real_unicode_buffer() -> None:
    """Regression guard: the label buffer must be a ctypes buffer, not a str."""
    seen: list[object] = []

    class _Recording(_FakeKernel32):
        def GetVolumeInformationW(self, root, name_buf, *a):  # noqa: N802
            seen.append(name_buf)
            name_buf.value = "OK"
            return 1

    k32 = _Recording(_bits("D"), {"D:\\": DRIVE_CDROM}, {"D:\\": "OK"})
    drives._list_drives_windows(k32)
    assert isinstance(seen[0], ctypes.Array)


# --- dispatch ---------------------------------------------------------------


def test_list_drives_dispatches_to_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = [drives.Drive(device="D:\\", label="X", has_disc=True)]
    monkeypatch.setattr(drives.sys, "platform", "win32")
    monkeypatch.setattr(drives, "_windows_drives", lambda: sentinel)
    assert drives.list_drives() == sentinel


def test_list_drives_dispatches_to_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = [drives.Drive(device="/dev/sr0", label="X", has_disc=True)]
    monkeypatch.setattr(drives.sys, "platform", "linux")
    monkeypatch.setattr(drives, "_list_drives_linux", lambda *a: sentinel)
    assert drives.list_drives() == sentinel


def test_list_drives_never_raises_on_a_broken_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> list[drives.Drive]:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(drives.sys, "platform", "win32")
    monkeypatch.setattr(drives, "_windows_drives", boom)
    assert drives.list_drives() == []
