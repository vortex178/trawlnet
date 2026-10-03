"""Test setup: a throwaway data folder (copied from examples/data-folder) and the engine on sys.path.

Imported first by every test module. JOB_SEARCH_HOME and XDG_CONFIG_HOME are always overridden so tests never touch real data.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "job-search" / "scripts"
EXAMPLE = REPO / "examples" / "data-folder"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SKIP = {"runs", "digests", "jobs.db", "pending_tracker_rows.jsonl"}


def make_home() -> Path:
    """Fresh copy of the example data folder without run outputs, state or the tracker file."""
    home = Path(tempfile.mkdtemp(prefix="js-test-"))
    shutil.copytree(EXAMPLE, home, dirs_exist_ok=True,
                    ignore=lambda d, names: [n for n in names if n in _SKIP or n == "tracker.csv"])
    return home


def resolve_dates(rows: list) -> list:
    """Fixture dates like '-3d' -> ISO dates relative to today (fixtures never go stale)."""
    for r in rows:
        p = r.get("posted")
        if isinstance(p, str) and p.startswith("-") and p.endswith("d"):
            r["posted"] = (dt.date.today() - dt.timedelta(days=int(p[1:-1]))).isoformat()
    return rows


def fixture_jsonl(name: str) -> list:
    return resolve_dates([json.loads(l) for l in (FIXTURES / name).read_text().splitlines() if l.strip()])


HOME = make_home()
atexit.register(shutil.rmtree, HOME, True)
os.environ["JOB_SEARCH_HOME"] = str(HOME)
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="js-test-cfg-")  # homes.register never touches ~/.config
atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], True)
sys.path.insert(0, str(SCRIPTS))
