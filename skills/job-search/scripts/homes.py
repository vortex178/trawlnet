"""Data folders set up on this machine, one path per line in $XDG_CONFIG_HOME/trawlnet/homes.

Stdlib only, nothing at import time: the MCP server reads the list before it may run a folder's .venv python (it
starts in every session, so a cloned folder must not get its binary run), and setup.py link writes it.
"""
from __future__ import annotations

import os
from pathlib import Path


def homes_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        try:
            base = str(Path.home() / ".config")
        except (KeyError, RuntimeError) as e:  # no HOME and no passwd entry
            raise OSError(f"no home directory for the trawlnet homes list: {e}")
        if not os.path.isabs(base):  # HOME="": never a list relative to the session's folder
            raise OSError(f"no absolute home directory for the trawlnet homes list: {base!r}")
    return Path(base) / "trawlnet" / "homes"


def nearest(start: Path, walk: bool = True) -> Path | None:
    """The data folder (config.yaml + data/) at `start`, else (walk=True) the nearest one above it."""
    start = Path(start).expanduser().resolve()
    for d in [start, *start.parents] if walk else [start]:
        if (d / "config.yaml").exists() and (d / "data").is_dir():  # as common._find_home
            return d
    return None


def _entries(strict: bool = False) -> list:
    """Absolute paths listed (a relative entry would follow the session's cwd). A missing list is empty; an unreadable
    or damaged one is empty too, or raises with strict=True so register() never appends to it on every run."""
    try:
        path = homes_file()
    except OSError:
        if strict:
            raise
        return []
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if strict:
            raise ValueError(f"{path} is unreadable or damaged ({e}); fix or delete it")
        return []
    return [line for line in text.replace("\r", "").split("\n") if os.path.isabs(line)]


def _listed(home: Path, entries: list) -> bool:
    target = str(Path(home).resolve())
    for entry in entries:
        if entry == target:
            return True
        if entry.casefold() == target.casefold():  # same folder in another case (macOS); stat only these
            try:
                if os.path.samefile(entry, target):
                    return True
            except OSError:
                pass
    return False


def is_registered(home: Path) -> bool:
    return _listed(home, _entries())


def register(home: Path) -> bool:
    """List a data folder; False when it is already listed. Raises OSError when the list cannot be written."""
    home = Path(home).resolve()
    if any(c < " " for c in str(home)):
        raise ValueError(f"path has control characters: {home!r}")
    if _listed(home, _entries(strict=True)):
        return False
    path = homes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = path.read_bytes()[-1:] if path.exists() else b""
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if tail not in (b"", b"\n") else "") + f"{home}\n")  # a hand edit may lack the last newline
    return True
