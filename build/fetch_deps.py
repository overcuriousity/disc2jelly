#!/usr/bin/env python3
"""Download the third-party binaries the Windows installer bundles.

Currently just HandBrakeCLI: GPLv2, redistributable, and invoked as a separate
process so there is no linking concern with Disc2Jelly's own GPL-3.0. Its
COPYING file is shipped alongside.

libdvdcss is deliberately NOT fetched here, and cannot be: VideoLAN publishes
it as source only. The user installs it themselves; the app detects it and
explains how (see disc2jelly/app/dvdcss.py).

Downloads are refused unless the archive matches a pinned SHA-256. To pin a
new release, bump VERSION and run:

    python build/fetch_deps.py --print-hashes

then paste the printed digest into HANDBRAKE_SHA256 below.
"""

from __future__ import annotations

import argparse
import hashlib
import ssl
import sys
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "disc2jelly" / "vendor"

HANDBRAKE_VERSION = "1.9.2"
HANDBRAKE_URL = (
    f"https://github.com/HandBrake/HandBrake/releases/download/"
    f"{HANDBRAKE_VERSION}/HandBrakeCLI-{HANDBRAKE_VERSION}-win-x86_64.zip"
)
# Pin before shipping: python build/fetch_deps.py --print-hashes
HANDBRAKE_SHA256 = ""

TIMEOUT_S = 300


def fetch(url: str) -> bytes:
    print(f"downloading {url}")
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=TIMEOUT_S, context=context) as resp:
        return resp.read()


def verify(payload: bytes, expected: str, what: str, allow_unpinned: bool) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    if not expected:
        message = (
            f"{what} is not pinned to a checksum. Run "
            f"`python build/fetch_deps.py --print-hashes` and paste the digest "
            f"into fetch_deps.py, or pass --allow-unpinned to skip this check."
        )
        if not allow_unpinned:
            raise SystemExit(f"refusing to build: {message}")
        print(f"WARNING: {message}")
        return
    if digest != expected:
        raise SystemExit(
            f"{what} checksum mismatch:\n  expected {expected}\n  got      {digest}"
        )
    print(f"{what} checksum OK")


def install_handbrake(payload: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith("handbrakecli.exe")]
        if not names:
            raise SystemExit("HandBrakeCLI.exe not found in the downloaded archive")
        target = dest / "HandBrakeCLI.exe"
        target.write_bytes(zf.read(names[0]))
    print(f"installed {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", type=Path, default=VENDOR_DIR)
    parser.add_argument("--print-hashes", action="store_true",
                        help="download and print digests, install nothing")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="proceed without a pinned checksum (not for release)")
    args = parser.parse_args(argv)

    payload = fetch(HANDBRAKE_URL)

    if args.print_hashes:
        print(f"\nHANDBRAKE_SHA256 = \"{hashlib.sha256(payload).hexdigest()}\"")
        return 0

    verify(payload, HANDBRAKE_SHA256, "HandBrakeCLI archive", args.allow_unpinned)
    install_handbrake(payload, args.vendor_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
