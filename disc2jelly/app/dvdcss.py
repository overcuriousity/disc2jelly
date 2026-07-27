"""Detection of libdvdcss, the CSS decryption library.

HandBrake reads a CSS-protected DVD only when libdvdcss is available. This
module never installs it — it only reports whether it is there and, if not,
tells the user exactly what to do.

Not bundling it is deliberate: distributing a circumvention library is a
different act, legally, from using one (§95a UrhG in Germany targets the
distribution side most directly). Downloading it on the user's behalf is not
an option either, because there is nothing to download — VideoLAN publishes
libdvdcss as source tarballs only, and VLC's own Windows build links it
statically into libdvdread_plugin.dll rather than shipping a standalone DLL.
An earlier version of this module fetched
``.../pub/libdvdcss/<ver>/win64/libdvdcss-2.dll``, a path that has never
existed and answered 404 on every call.

Windows: libdvdcss-2.dll must sit next to HandBrakeCLI.exe.
Linux:   the distribution package provides it; check via ctypes.
"""

from __future__ import annotations

import sys
from ctypes.util import find_library
from pathlib import Path

DLL_NAME = "libdvdcss-2.dll"

LINUX_HINT = (
    "libdvdcss is not installed. Encrypted DVDs cannot be read without it. "
    "Install your distribution's package (Debian/Ubuntu: libdvdcss2 via "
    "libdvd-pkg; Fedora: libdvdcss from RPM Fusion) and restart Disc2Jelly."
)

WINDOWS_HINT = (
    "libdvdcss is not installed. Encrypted DVDs — which means almost every "
    "commercial disc — cannot be read without it.\n\n"
    "Put {dll} in this folder:\n    {folder}\n\n"
    "then restart Disc2Jelly. It is not shipped with this program and there "
    "is no official download: build it from the source release at "
    "https://download.videolan.org/pub/libdvdcss/ or copy the file from an "
    "existing player installation that includes it."
)


class DvdCssError(Exception):
    """Raised when libdvdcss is unavailable."""


def _find_system_library() -> str | None:
    return find_library("dvdcss")


def is_available(bundled_dir: Path | str | None = None) -> bool:
    """Is CSS decryption available? Never raises."""
    try:
        if sys.platform.startswith("win"):
            if bundled_dir is None:
                return False
            return (Path(bundled_dir) / DLL_NAME).is_file()
        return _find_system_library() is not None
    except Exception:
        return False


def hint(bundled_dir: Path | str | None = None) -> str:
    """What the user has to do to get libdvdcss, for this platform."""
    if not sys.platform.startswith("win"):
        return LINUX_HINT
    folder = str(bundled_dir) if bundled_dir is not None else "the Disc2Jelly folder"
    return WINDOWS_HINT.format(dll=DLL_NAME, folder=folder)


def require(bundled_dir: Path | str | None = None) -> None:
    """Raise DvdCssError with actionable guidance if libdvdcss is missing."""
    if not is_available(bundled_dir):
        raise DvdCssError(hint(bundled_dir))
