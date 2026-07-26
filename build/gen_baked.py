#!/usr/bin/env python3
"""Generate disc2jelly/app/_baked.py — the installer's built-in defaults.

Reads build_config.toml (gitignored; see build_config.example.toml) and writes
a tiny Python module the app imports with a try/except fallback, so a plain
source checkout keeps working with no baked values at all.

SECURITY: the WebDAV password is NOT written unless --bake-password is passed.
PyInstaller does not obfuscate anything — every string in the generated module
is recoverable from the shipped executable with `strings`. Bake the URL and
username (not secret) and let the Inno Setup wizard collect the password on
the target machine instead.

  python build/gen_baked.py                      # from build_config.toml
  python build/gen_baked.py --bake-password      # private builds only
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "build_config.toml"
OUTPUT_PATH = REPO_ROOT / "disc2jelly" / "app" / "_baked.py"

# (python constant, config key, default)
FIELDS = [
    ("TMDB_API_KEY", "tmdb_api_key", ""),
    ("DESTINATION_KIND", "destination_kind", "local"),
    ("LOCAL_PATH", "local_path", ""),
    ("WEBDAV_URL", "webdav_url", ""),
    ("WEBDAV_USER", "webdav_user", ""),
]

HEADER = '''"""Build-time defaults baked in by build/gen_baked.py. DO NOT EDIT.

Generated file — not committed. A source checkout has no _baked.py at all and
every consumer falls back to an empty default.
"""
'''

PASSWORD_WARNING = '''
# WARNING: this build has a WebDAV password compiled in. PyInstaller performs
# no obfuscation; `strings` on the executable recovers it in seconds. Keep this
# installer off any public release page and use an app password scoped to the
# destination share, never a main account password.
'''


def render_baked(cfg: dict, include_password: bool) -> str:
    """Render the _baked.py source. Values go through repr(), never f-strings,
    so a config value can never inject code into the generated module."""
    lines = [HEADER]
    if include_password:
        lines.append(PASSWORD_WARNING)
    lines.append("")
    for name, key, default in FIELDS:
        value = cfg.get(key, default)
        lines.append(f"{name} = {str(value)!r}")

    password = cfg.get("webdav_password", "") if include_password else ""
    lines.append(f"WEBDAV_PASSWORD = {str(password)!r}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--bake-password", action="store_true",
        help="compile the WebDAV password into the binary (private builds only)",
    )
    args = parser.parse_args(argv)

    if args.config.is_file():
        cfg = tomllib.loads(args.config.read_text(encoding="utf-8"))
    else:
        print(f"note: {args.config} not found — generating empty defaults")
        cfg = {}

    if args.bake_password:
        print("WARNING: baking a WebDAV password into the binary. It is "
              "recoverable with `strings`. Do not publish this installer.")

    args.output.write_text(
        render_baked(cfg, include_password=args.bake_password), encoding="utf-8"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
