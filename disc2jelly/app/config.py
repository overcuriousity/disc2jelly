"""Disc2Jelly configuration: load/save/validate + binary discovery.

Settings come from four layers, each overriding the one before it:

  1. the field defaults below
  2. app/_baked.py         — compiled in by build/gen_baked.py, absent in a checkout
  3. install_defaults.json — written by the Inno Setup wizard on the target machine
  4. config.json           — the user's own settings, the only file save() writes

The installer deliberately does not touch config.json. Reinstalling or upgrading
rewrites layer 3 only, so a user's own settings can never be destroyed by it.

JSON at platform path:
  Linux   ~/.config/disc2jelly/{config,install_defaults}.json
  Windows %APPDATA%/disc2jelly/{config,install_defaults}.json
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

APP_DIR_NAME = "disc2jelly"
CONFIG_FILENAME = "config.json"
INSTALL_DEFAULTS_FILENAME = "install_defaults.json"

try:  # generated at build time by build/gen_baked.py; absent in a source checkout
    from . import _baked
except ImportError:  # pragma: no cover - exercised by the absence of the module
    _baked = None  # type: ignore[assignment]


def baked_default(name: str, fallback: str = "") -> str:
    """A build-time default, or `fallback` when nothing was baked in."""
    value = getattr(_baked, name, "") if _baked is not None else ""
    return (value or "").strip() or fallback


@dataclass
class Config:
    # The string fields the installer can pre-seed read their defaults from
    # _baked.py, so a build with baked values works before any file exists.
    destination_kind: str = field(  # "local" | "webdav"
        default_factory=lambda: baked_default("DESTINATION_KIND", "local"))
    local_path: str = field(       # empty = destination.DEFAULT_LOCAL_ROOT
        default_factory=lambda: baked_default("LOCAL_PATH"))
    # e.g. https://nas.example/remote.php/dav/files/me/movies-inbox
    webdav_url: str = field(default_factory=lambda: baked_default("WEBDAV_URL"))
    webdav_user: str = field(default_factory=lambda: baked_default("WEBDAV_USER"))
    webdav_password: str = field(
        default_factory=lambda: baked_default("WEBDAV_PASSWORD"))
    # v3 api_key (or v4 read token)
    tmdb_api_key: str = field(default_factory=lambda: baked_default("TMDB_API_KEY"))
    temp_dir: str = ""            # default: <config dir>/work
    encoder: str = "hevc"         # "hevc" | "h264"
    hevc_quality: int = 22        # RF/CRF
    h264_quality: int = 20
    handbrake_path: str = ""      # empty = auto-detect
    min_title_seconds: int = 600  # filter junk titles


def config_dir() -> Path:
    """Platform config directory (created on demand by save())."""
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_DIR_NAME
        return Path.home() / "AppData" / "Roaming" / APP_DIR_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def install_defaults_path() -> Path:
    """Defaults written by the installer wizard. Never written by the app."""
    return config_dir() / INSTALL_DEFAULTS_FILENAME


def _read_json(path: Path) -> dict:
    """Decode a settings file; missing, unreadable or corrupt yields {}."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _apply(cfg: Config, raw: dict) -> Config:
    """Overlay `raw` onto `cfg`, dropping unknown keys and mismatched types."""
    known = {f.name for f in fields(Config)}
    data: dict = {}
    for key, value in raw.items():
        if key not in known:
            continue
        # Compare against the live value, not the field's declared default:
        # default_factory fields report dataclasses.MISSING for .default.
        current = getattr(cfg, key)
        if isinstance(current, bool):
            if isinstance(value, bool):
                data[key] = value
        elif isinstance(current, int):
            if isinstance(value, int) and not isinstance(value, bool):
                data[key] = value
        else:  # str
            if isinstance(value, str):
                data[key] = value
    return replace(cfg, **data) if data else cfg


def load(path: Path | None = None, defaults_path: Path | None = None) -> Config:
    """Load settings, layering installer defaults under the user's own file.

    An explicit `path` implies its sibling install_defaults.json unless
    `defaults_path` says otherwise, so the two layers stay together.
    """
    user_path = path or config_path()
    if defaults_path is None:
        defaults_path = user_path.parent / INSTALL_DEFAULTS_FILENAME
    cfg = _apply(Config(), _read_json(defaults_path))
    return _apply(cfg, _read_json(user_path))


def save(cfg: Config, path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def find_binary(name: str, configured: str, os_candidates: list[str]) -> str | None:
    """Locate an external binary.

    Order: configured path if it exists → shutil.which(name) → probe
    candidate absolute paths → None.
    """
    if configured:
        p = Path(os.path.expandvars(configured)).expanduser()
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    for cand in os_candidates:
        p = Path(os.path.expandvars(cand)).expanduser()
        if p.is_file():
            return str(p)
    return None


def bundled_dir() -> Path:
    """Where the installer puts HandBrakeCLI and libdvdcss.

    Frozen (PyInstaller): alongside the executable. Source checkout: ./vendor
    next to the app package, so a dev can drop binaries there too.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent / "vendor"


def handbrake_candidates() -> list[str]:
    """Bundled copy first — the installer ships one and it is the known-good
    version — then the usual system install locations."""
    exe = "HandBrakeCLI.exe" if sys.platform.startswith("win") else "HandBrakeCLI"
    cands = [str(bundled_dir() / exe)]
    if sys.platform.startswith("win"):
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in dict.fromkeys((pf, pfx)):
            cands.append(str(Path(base) / "HandBrake" / "HandBrakeCLI.exe"))
    else:
        cands += ["/usr/bin/HandBrakeCLI", "/usr/local/bin/HandBrakeCLI"]
    return cands


def resolve_binaries(cfg: Config) -> str | None:
    """Locate HandBrakeCLI, honoring a configured override. None if absent."""
    hb_name = "HandBrakeCLI.exe" if sys.platform.startswith("win") else "HandBrakeCLI"
    return find_binary(hb_name, cfg.handbrake_path, handbrake_candidates())
