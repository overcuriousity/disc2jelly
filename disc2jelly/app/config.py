"""Disc2Jelly configuration: load/save/validate + binary discovery.

JSON at platform path:
  Linux   ~/.config/disc2jelly/config.json
  Windows %APPDATA%/disc2jelly/config.json
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR_NAME = "disc2jelly"
CONFIG_FILENAME = "config.json"


@dataclass
class Config:
    destination_kind: str = "local"  # "local" | "webdav"
    local_path: str = ""          # empty = destination.DEFAULT_LOCAL_ROOT
    webdav_url: str = ""          # e.g. https://nas.example/remote.php/dav/files/me/movies-inbox
    webdav_user: str = ""
    webdav_password: str = ""
    tmdb_api_key: str = ""        # v3 api_key (or v4 read token); empty = baked default
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


def load(path: Path | None = None) -> Config:
    """Load config from disk; missing/corrupt file yields defaults."""
    p = path or config_path()
    cfg = Config()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    known = {f.name: f for f in fields(Config)}
    data: dict = {}
    for key, value in raw.items():
        if key not in known:
            continue
        default = known[key].default
        # Coerce simple scalar types; drop mismatched types silently.
        if isinstance(default, bool):
            if isinstance(value, bool):
                data[key] = value
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                data[key] = value
        else:  # str
            if isinstance(value, str):
                data[key] = value
    return Config(**data)


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
