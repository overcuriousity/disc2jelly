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
import urllib.error
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
# Re-pin after a version bump: python build/fetch_deps.py --print-hashes
HANDBRAKE_SHA256 = "cc875eda177a4105b99664136719c893db989a4456c4d3a9fc9e8d8742018413"

# GPLv2 licence texts inside the archive. HandBrakeCLI is redistributed by the
# installer, so these have to travel with it. disc2jelly.spec collects them
# from the vendor directory by these names.
LICENCE_MEMBERS = {"doc/COPYING": "COPYING", "doc/LICENSE": "LICENSE"}

TIMEOUT_S = 300


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS context, preferring certifi's CA bundle.

    Python on Windows verifies against the Windows ROOT store, which on a
    fresh or locked-down machine can lack the issuing chain — the download
    then dies with CERTIFICATE_VERIFY_FAILED. certifi arrives with requests
    (requirements.txt) and carries the roots itself. Verification is never
    disabled: the pinned SHA-256 guards content, not transport.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def fetch(url: str) -> bytes:
    print(f"downloading {url}")
    try:
        with urllib.request.urlopen(
            url, timeout=TIMEOUT_S, context=_ssl_context()
        ) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise SystemExit(
                f"TLS verification failed for {url}: {reason}\n"
                f"Install certifi (`python -m pip install certifi`) so this "
                f"script uses its CA bundle. If it is already installed, a "
                f"TLS-inspecting proxy or antivirus is rewriting the "
                f"connection — export SSL_CERT_FILE pointing at its root "
                f"certificate, or download the archive in a browser and pass "
                f"it to this script with --archive <path> (the pinned "
                f"checksum is still enforced)."
            ) from exc
        raise SystemExit(f"download failed for {url}: {reason}") from exc


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
        members = {n.lower(): n for n in zf.namelist()}
        names = [n for n in zf.namelist() if n.lower().endswith("handbrakecli.exe")]
        if not names:
            raise SystemExit("HandBrakeCLI.exe not found in the downloaded archive")
        target = dest / "HandBrakeCLI.exe"
        target.write_bytes(zf.read(names[0]))
        print(f"installed {target}")

        # Shipping a GPLv2 binary without its licence is not an option.
        found = False
        for member, filename in LICENCE_MEMBERS.items():
            actual = members.get(member.lower())
            if actual is None:
                continue
            (dest / filename).write_bytes(zf.read(actual))
            print(f"installed {dest / filename}")
            found = True
        if not found:
            raise SystemExit(
                "no licence file found in the HandBrakeCLI archive; refusing to "
                "redistribute a GPLv2 binary without one"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", type=Path, default=VENDOR_DIR)
    parser.add_argument("--print-hashes", action="store_true",
                        help="download and print digests, install nothing")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="proceed without a pinned checksum (not for release)")
    parser.add_argument("--archive", type=Path,
                        help="use this already-downloaded HandBrakeCLI zip "
                             "instead of downloading (still checksum-verified)")
    args = parser.parse_args(argv)

    if args.archive:
        if not args.archive.is_file():
            raise SystemExit(f"archive not found: {args.archive}")
        print(f"using local archive {args.archive}")
        payload = args.archive.read_bytes()
    else:
        payload = fetch(HANDBRAKE_URL)

    if args.print_hashes:
        print(f"\nHANDBRAKE_SHA256 = \"{hashlib.sha256(payload).hexdigest()}\"")
        return 0

    verify(payload, HANDBRAKE_SHA256, "HandBrakeCLI archive", args.allow_unpinned)
    install_handbrake(payload, args.vendor_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
