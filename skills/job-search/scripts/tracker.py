"""Tracker backends. Append-only: rows are added, never edited; the Status column is left for the user.

Usage (./js tracker <cmd>, alias ./js sheets <cmd>):
  check          verify the backend (file / key, access, tab, header)
  init           create the header (CSV) or name + format the tab (Google Sheets)
  flush          push queued rows (data/pending_tracker_rows.jsonl)

Config `tracker.backend`: csv (default; a file in the data folder, opens in any spreadsheet app) | gsheets
(service account; setup steps in references/tracker-setup.md). `tracker.columns` picks and orders fields from
FIELDS. Rows produced while a backend is unavailable stay queued and are pushed on the next publish/flush.
"""
from __future__ import annotations

import contextlib
import csv
import functools
import importlib
import json
import sys
import urllib.parse

from common import (HOME, PENDING_ROWS_PATH, append_jsonl, confined, load_config, read_jsonl, rel, save_config_value,
                    write_jsonl)

FIELDS = {"company": "Company", "role": "Role", "score": "Match Score", "url": "Apply URL", "profile": "Profile",
          "location": "Location", "posted": "Posted", "source": "Source", "date_added": "Date Added",
          "status": "Status"}
DEFAULT_COLUMNS = ["company", "role", "score", "url", "profile", "status"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class Unavailable(Exception):
    """Backend can't be reached now (missing key, no access); rows stay queued."""


def _optional(module: str, name: str) -> tuple:
    """(module.name,), or () without that package (then _session raises Unavailable first)."""
    try:
        return (getattr(importlib.import_module(module), name),)
    except ImportError:
        return ()


def _auth_unavailable(fn):
    """google-auth failures (expired or revoked key) and request failures (offline, timeouts, HTTP errors) surface on
    a request: report them as Unavailable, so flush keeps the rows queued and the CLI and MCP tools say why instead of
    crashing."""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except _optional("google.auth.exceptions", "GoogleAuthError") as e:
            raise Unavailable(f"Google auth failed: {e.args[0] if e.args else e}") from e
        except _optional("requests", "RequestException") as e:
            raise Unavailable(f"Google Sheets request failed: {str(e)[:300]}") from e
    return wrapped


def tracker_cfg(cfg: dict) -> dict:
    t = dict(cfg.get("tracker") or {})
    if not t and cfg.get("sheet"):  # pre-plugin config layout
        t = {"backend": "gsheets", "gsheets": cfg["sheet"]}
    t.setdefault("backend", "csv")
    return t


def columns(cfg: dict) -> list:
    cols = tracker_cfg(cfg).get("columns") or DEFAULT_COLUMNS
    bad = [c for c in cols if c not in FIELDS]
    if bad:
        sys.exit(f"tracker.columns: unknown field(s) {bad}; choose from {list(FIELDS)}")
    return cols


def header(cfg: dict) -> list:
    return [FIELDS[c] for c in columns(cfg)]


def _row(d, cols: list) -> list:
    if isinstance(d, list):  # legacy queued row (fixed 6 columns)
        d = dict(zip(DEFAULT_COLUMNS, d))
    return ["" if d.get(c) is None else d.get(c) for c in cols]


def _is_data(cols: list, cells: list) -> bool:
    """A first row the engine wrote (a tab never initialised): an http(s) URL or a number in its column."""
    d = {c: str(cells[i]).strip() for i, c in enumerate(cols) if i < len(cells)}
    if d.get("url", "").startswith(("http://", "https://")):
        return True
    try:
        float(d.get("score", ""))
    except ValueError:
        return False
    return True


def _records(cols: list, rows: list) -> list:
    """Rows -> dicts by tracker.columns position, as the engine writes them; short rows are padded, blank rows
    skipped."""
    out = []
    for cells in rows:
        if not any(str(c).strip() for c in cells):
            continue
        d = {c: cells[i] if i < len(cells) else "" for i, c in enumerate(cols)}
        if d.get("score"):
            try:
                d["score"] = int(float(d["score"]))
            except (ValueError, OverflowError):  # "n/a", "inf", "1e999": keep the cell as typed
                pass
        out.append(d)
    return out


def _label(cell) -> str:
    return str(cell).lstrip("\ufeff").strip()  # a CSV re-saved from Excel starts with a byte-order mark


def read_rows(cfg: dict) -> tuple:
    """(rows as dicts, warning or ""). The first row is the header unless the engine wrote it. Rows map by position,
    so a header showing another layout (tracker.columns changed later) gets a warning: older rows read shifted."""
    cols, table = columns(cfg), backend(cfg).read_table()
    while table and not any(str(c).strip() for c in table[0]):  # leading blank rows
        table = table[1:]
    if not table or _is_data(cols, table[0]):
        return _records(cols, table), ""
    head = [_label(c) for c in table[0][:len(cols)]]  # columns beyond the configured ones read fine
    warning = "" if head == header(cfg) else (
        f"the header row {str(head)[:300]} differs from tracker.columns {header(cfg)}: rows are read in "
        "tracker.columns order and may be shifted; run `./js tracker check`")
    return _records(cols, table[1:]), warning


# ---------- CSV ----------

class CsvBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = confined(tracker_cfg(cfg).get("csv_path"), "tracker.csv", "tracker.csv_path", HOME)

    def init(self):
        if not self.path.exists() or not self.path.stat().st_size:
            try:
                with self.path.open("w", newline="") as f:
                    csv.writer(f).writerow(header(self.cfg))
            except OSError as e:
                raise Unavailable(f"cannot write {rel(self.path)}: {e}")
        print(f"tracker file: {rel(self.path)}")

    def append(self, rows: list) -> str:
        try:
            if not self.path.exists() or not self.path.stat().st_size:
                with self.path.open("w", newline="") as f:
                    csv.writer(f).writerow(header(self.cfg))
            with self.path.open("a", newline="") as f:
                csv.writer(f).writerows(_row(r, columns(self.cfg)) for r in rows)
        except OSError as e:  # open in a spreadsheet app that locks it (Windows), read-only: rows stay queued
            raise Unavailable(f"cannot write {rel(self.path)}: {e}")
        return f"appended to {rel(self.path)}"

    def read_table(self) -> list:
        return self._read() if self.path.exists() else []

    def _read(self) -> list:
        try:
            with self.path.open(newline="") as f:
                return list(csv.reader(f))
        except (csv.Error, OSError) as e:  # e.g. a NUL in the file (Python <= 3.10), no read permission
            raise Unavailable(f"{rel(self.path)} is unreadable: {e}")

    def check(self):
        if not self.path.exists():
            print(f"{rel(self.path)} not created yet (created on first publish or `init`)")
            return
        rows = self._read()
        print(f"file: {rel(self.path)}, rows incl. header: {len(rows)}")
        ok = rows and [_label(c) for c in rows[0]] == header(self.cfg)
        print("header OK" if ok else f"HEADER MISMATCH: {rows[0] if rows else []}")

    def describe(self) -> str:
        return f"tracker: csv {rel(self.path)}"


# ---------- Google Sheets (service account) ----------

class SheetsBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.g = tracker_cfg(cfg).get("gsheets") or {}
        self.key = confined(self.g.get("service_account_key"), ".secrets/service-account.json",
                            "tracker.gsheets.service_account_key", HOME)
        self.last_col = chr(ord("A") + len(columns(cfg)) - 1)

    def _session(self):
        if not self.g.get("sheet_id"):
            raise Unavailable("tracker.gsheets.sheet_id is not set")
        if not self.key.exists():
            raise Unavailable(f"service-account key not found at {rel(self.key)}")
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError as e:
            raise Unavailable(f"google-auth or requests is not installed ({e}); run `./js setup env`")
        try:
            creds = service_account.Credentials.from_service_account_file(
                str(self.key), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except (OSError, ValueError) as e:  # unreadable or not a service-account key
            raise Unavailable(f"service-account key {rel(self.key)} is unusable: {e}")
        return AuthorizedSession(creds), creds.service_account_email

    def _tab(self, sess) -> dict:
        r = sess.get(f"{API}/{self.g['sheet_id']}", params={"fields": "properties.title,sheets.properties"})
        if r.status_code == 403:
            err = r.json().get("error", {})
            if any(d.get("reason") == "SERVICE_DISABLED" for d in err.get("details", [])):
                raise Unavailable("403: Google Sheets API is not enabled in the service account's Cloud project")
            raise Unavailable(f"403: share the sheet with {sess.credentials.service_account_email} as Editor")
        r.raise_for_status()
        sheets = [s["properties"] for s in r.json()["sheets"]]
        gid = self.g.get("sheet_tab_gid")
        tab = next((s for s in sheets if s["sheetId"] == gid), None) if gid is not None else None
        return tab or sheets[0]

    @staticmethod
    def _range(tab: dict, a1: str) -> str:
        return urllib.parse.quote(f"'{tab['title']}'!{a1}", safe="")

    @_auth_unavailable
    def append(self, rows: list) -> str:
        sess, _ = self._session()
        tab = self._tab(sess)
        r = sess.post(f"{API}/{self.g['sheet_id']}/values/{self._range(tab, f'A:{self.last_col}')}:append",
                      params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                      json={"values": [_row(x, columns(self.cfg)) for x in rows]})
        if not r.ok:
            raise Unavailable(f"append failed: {r.status_code} {r.text[:200]}")
        return f"appended to tab '{tab['title']}'"

    @_auth_unavailable
    def read_table(self) -> list:
        sess, _ = self._session()
        r = sess.get(f"{API}/{self.g['sheet_id']}/values/{self._range(self._tab(sess), f'A:{self.last_col}')}")
        r.raise_for_status()
        return r.json().get("values") or []

    @_auth_unavailable
    def check(self):
        sess, email = self._session()
        tab = self._tab(sess)
        sid = self.g["sheet_id"]
        r = sess.get(f"{API}/{sid}/values/{self._range(tab, f'A1:{self.last_col}1')}")
        r.raise_for_status()
        got = (r.json().get("values") or [[]])[0]
        n = sess.get(f"{API}/{sid}/values/{self._range(tab, 'A:A')}").json().get("values", [])
        print(f"service account: {email}")
        print(f"tab: '{tab['title']}' (gid {tab['sheetId']}), rows incl. header: {len(n)}")
        print("header OK" if got == header(self.cfg) else f"HEADER MISMATCH: {got}")

    @_auth_unavailable
    def init(self):
        sess, email = self._session()
        tab = self._tab(sess)
        sid, gid = self.g["sheet_id"], tab["sheetId"]
        reqs = [
            {"updateSheetProperties": {"properties": {"sheetId": gid, "title": "Tracker",
                                                      "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "title,gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
        ]
        sess.post(f"{API}/{sid}:batchUpdate", json={"requests": reqs}).raise_for_status()
        tab["title"] = "Tracker"
        a1 = f"A1:{self.last_col}1"
        r = sess.get(f"{API}/{sid}/values/{self._range(tab, a1)}")
        if not (r.json().get("values") or [[]])[0]:
            sess.put(f"{API}/{sid}/values/{self._range(tab, a1)}", params={"valueInputOption": "RAW"},
                     json={"values": [header(self.cfg)]}).raise_for_status()
        save_config_value("tracker.gsheets.sheet_tab_gid", gid)
        print(f"initialized tab 'Tracker' (gid {gid}) as {email}")

    def describe(self) -> str:
        return (f"tracker: google sheet {self.g.get('sheet_url') or self.g.get('sheet_id') or '(not set)'} | "
                f"key {'present' if self.key.exists() else 'MISSING (' + rel(self.key) + ')'}")


BACKENDS = {"csv": CsvBackend, "gsheets": SheetsBackend}


def backend(cfg: dict):
    name = tracker_cfg(cfg)["backend"]
    if name not in BACKENDS:
        sys.exit(f"tracker.backend '{name}' unknown; choose from {list(BACKENDS)}")
    return BACKENDS[name](cfg)


_lock_depth = 0


@contextlib.contextmanager
def queue_lock():
    """Holds the queue across processes (the MCP server and CLI runs share it), so a flush never pushes a row twice
    or truncates one queued meanwhile. Reentrant, so a caller can hold it around queue_rows + flush."""
    global _lock_depth
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    try:
        import fcntl
    except ImportError:  # Windows: no lock
        fcntl = None
    with contextlib.ExitStack() as stack:
        try:
            f = stack.enter_context(PENDING_ROWS_PATH.with_name(PENDING_ROWS_PATH.name + ".lock").open("a"))
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)  # released when the file closes
        except OSError:  # a read-only folder or a filesystem without locks (some network or sync mounts): unlocked
            pass
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0


def queue_rows(rows: list) -> None:
    with queue_lock():
        append_jsonl(PENDING_ROWS_PATH, [{"row": r} for r in rows])


def flush(cfg=None) -> tuple:
    """Push queued rows. Returns (pushed, still_pending, message)."""
    cfg = cfg or load_config()
    with queue_lock():
        legacy = PENDING_ROWS_PATH.with_name("pending_sheet_rows.jsonl")  # pre-plugin queue file
        if legacy.exists():
            append_jsonl(PENDING_ROWS_PATH, read_jsonl(legacy))
            legacy.unlink()
        pending = [p["row"] for p in read_jsonl(PENDING_ROWS_PATH)]
        if not pending:
            return 0, 0, "nothing queued"
        try:
            msg = backend(cfg).append(pending)
        except (Unavailable, ValueError) as e:  # ValueError: a config path outside the data folder
            return 0, len(pending), str(e)
        write_jsonl(PENDING_ROWS_PATH, [])
        return len(pending), 0, msg


def describe(cfg: dict) -> str:
    try:
        what = backend(cfg).describe()
    except ValueError as e:  # a config path outside the data folder
        what = f"tracker unavailable: {e}"
    return f"{what} | queued rows: {len(read_jsonl(PENDING_ROWS_PATH))}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    cfg = load_config()
    try:
        if cmd == "check":
            backend(cfg).check()
            print(describe(cfg))
        elif cmd == "init":
            backend(cfg).init()
        elif cmd == "flush":
            print(json.dumps(dict(zip(("pushed", "pending", "msg"), flush(cfg)))))
        else:
            sys.exit(__doc__)
    except (Unavailable, ValueError) as e:
        sys.exit(f"tracker unavailable: {e}")
