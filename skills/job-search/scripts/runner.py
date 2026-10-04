"""Background search for the MCP run_feeds tool: plan, feeds, filter and shortlist as one detached process, so a
fetch lasting minutes never blocks the server's stdio loop or runs into a client's tool-call timeout.

`python runner.py <run dir>` runs the steps with `jobsearch.py --date <run dir name>`. It holds <run dir>/run.lock
while it runs (the OS drops the lock when it exits, crashed or not), writes its state to progress.json and appends
the steps' output to run.log (run_feeds starts that file and sends the runner's own errors there). Stdlib only."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

STEPS = ("plan", "feeds", "filter", "shortlist")
SCRIPT = Path(__file__).resolve().parent / "jobsearch.py"
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def lock(path: Path):
    """An exclusive lock on `path`, held while the returned file stays open; None when another process holds it."""
    f = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":  # pragma: no cover
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def running(d: Path) -> bool:
    """Whether a runner holds the run folder's lock."""
    if not (d / "run.lock").exists():
        return False
    f = lock(d / "run.lock")
    if f is None:
        return True
    f.close()
    return False


def write_progress(d: Path, **state) -> None:
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".progress.", suffix=".tmp")  # a fresh name: never a planted link
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({**state, "updated": now()}, f)
    for attempt in range(5):
        try:
            os.replace(tmp, d / "progress.json")
            return
        except PermissionError:  # pragma: no cover  (Windows: run_status has the file open for a moment)
            if attempt == 4:
                raise
            time.sleep(0.2)


def run(d: Path) -> int:
    held = None
    for _ in range(50):  # run_feeds' liveness probe holds the lock for an instant
        held = lock(d / "run.lock")
        if held:
            break
        time.sleep(0.1)
    if not held:
        return 1  # another runner has it: leave its progress alone
    started, done = now(), []
    with held, open(d / "run.log", "a", encoding="utf-8") as log:
        try:
            for step in STEPS:
                write_progress(d, state="running", step=step, done=done, started=started)
                log.write(f"$ {step}\n")
                log.flush()
                rc = subprocess.call([sys.executable, str(SCRIPT), step, "--date", d.name, "--quiet"],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **NO_WINDOW)
                if rc:
                    write_progress(d, state="failed", step=step, exit_code=rc, done=done, started=started,
                                   finished=now())
                    return rc
                done.append(step)
            write_progress(d, state="done", done=done, started=started, finished=now())
        except Exception:  # through this handle: on Windows the inherited stderr would write over the log's start
            log.write(traceback.format_exc())  # run_status then reports the run stopped, with this in its tail
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(Path(sys.argv[1])))
