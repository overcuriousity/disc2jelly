"""First-run acquisition of libdvdcss, the CSS decryption library.

HandBrake reads a CSS-protected DVD only when libdvdcss is available. It is
not bundled with the installer: distributing a circumvention library is a
different act, legally, from using one (§95a UrhG in Germany targets the
distribution side most directly), so the user's own machine fetches it from
VideoLAN on first run and the UI says so out loud.

Windows: download libdvdcss-2.dll next to HandBrakeCLI.exe.
Linux:   detect the distro package; never download, just tell the user the
         package name.

Set EXPECTED_SHA256 to pin the download. Leave it empty and the transfer is
trusted on HTTPS alone, which the caller is warned about.
"""

from __future__ import annotations

import hashlib
import ssl
import sys
import time
import urllib.request
from ctypes.util import find_library
from pathlib import Path
from typing import Callable

DLL_NAME = "libdvdcss-2.dll"
LIBDVDCSS_VERSION = "1.4.3"
DOWNLOAD_URL = (
    f"https://download.videolan.org/pub/libdvdcss/{LIBDVDCSS_VERSION}/win64/{DLL_NAME}"
)

# Pin the release you ship against: build/fetch_deps.py --print-hashes prints it.
# Empty means the download is trusted on HTTPS alone and the user is warned.
EXPECTED_SHA256 = ""

DOWNLOAD_TIMEOUT_S = 60

LINUX_HINT = (
    "libdvdcss is not installed. Encrypted DVDs cannot be read without it. "
    "Install your distribution's package (Debian/Ubuntu: libdvdcss2 via "
    "libdvd-pkg; Fedora: libdvdcss from RPM Fusion) and restart Disc2Jelly."
)


class DvdCssError(Exception):
    """Raised when libdvdcss is unavailable and cannot be installed."""


def _event(status: str, detail: str) -> dict:
    return {
        "stage": "APP",
        "status": status,
        "percent": None,
        "detail": detail,
        "ts": time.time(),
    }


def _find_system_library() -> str | None:
    return find_library("dvdcss")


def _https_fetch(url: str) -> bytes:
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S, context=context) as resp:
        return resp.read()


def ensure_libdvdcss(
    dest_dir: Path | str | None,
    emit: Callable[[dict], None],
    fetch: Callable[[str], bytes] = _https_fetch,
) -> Path | None:
    """Make libdvdcss available, downloading it on Windows if needed.

    Returns the installed DLL path on Windows, or None on Linux where the
    library is provided by the system. Raises DvdCssError if it cannot be
    made available.
    """
    if not sys.platform.startswith("win"):
        if _find_system_library():
            return None
        raise DvdCssError(LINUX_HINT)

    dest = Path(dest_dir) if dest_dir is not None else Path.cwd()
    target = dest / DLL_NAME
    if target.is_file():
        return target

    emit(_event(
        "running",
        f"Fetching libdvdcss {LIBDVDCSS_VERSION} from VideoLAN — needed once, "
        "to read encrypted DVDs",
    ))
    if not EXPECTED_SHA256:
        emit(_event(
            "running",
            "Note: this download is not pinned to a checksum; it is trusted "
            "over HTTPS from download.videolan.org only",
        ))

    try:
        payload = fetch(DOWNLOAD_URL)
    except Exception as exc:
        emit(_event("error", f"Could not download libdvdcss: {exc}"))
        raise DvdCssError(f"Could not download libdvdcss: {exc}") from exc

    if EXPECTED_SHA256:
        digest = hashlib.sha256(payload).hexdigest()
        if digest != EXPECTED_SHA256:
            emit(_event("error", "libdvdcss checksum mismatch — refusing to install"))
            raise DvdCssError(
                f"libdvdcss checksum mismatch: expected {EXPECTED_SHA256}, got {digest}"
            )

    try:
        dest.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(payload)
        tmp.replace(target)
    except OSError as exc:
        target.unlink(missing_ok=True)
        emit(_event("error", f"Could not install libdvdcss: {exc}"))
        raise DvdCssError(f"Could not install libdvdcss: {exc}") from exc

    emit(_event("done", "libdvdcss installed — encrypted DVDs can now be read"))
    return target
