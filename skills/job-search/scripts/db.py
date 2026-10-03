"""SQLite machine state in <data folder>/data/jobs.db (stdlib sqlite3). Editable files stay YAML/JSON.

  jobs    every job the pipeline has decided on (rejected / scored / lead / tracked) — the "seen" list
  scores  scorer output per run (one row per job and run)
  runs    one row per published run (stats)
  cache   small lookups keyed by kind (e.g. WWR free-apply verification)

Schema changes: bump SCHEMA and add a step to MIGRATIONS; `connect()` applies pending steps.
Legacy files (data/seen.jsonl, data/wwr_verify.json) are imported once and renamed *.migrated.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3

from common import DATA, read_jsonl, today

DB_PATH = DATA / "jobs.db"
SCHEMA = 1
MIGRATIONS = {
    1: """
    CREATE TABLE jobs (key TEXT PRIMARY KEY, first_seen TEXT, status TEXT, reason TEXT, score INTEGER,
                       profile TEXT, source TEXT, company TEXT, title TEXT, location TEXT, posted TEXT, url TEXT,
                       run TEXT);
    CREATE TABLE scores (key TEXT, run TEXT, profile TEXT, score INTEGER, raw_score INTEGER, verdict TEXT,
                         gates_json TEXT, strengths_json TEXT, gaps_json TEXT, flags_json TEXT, apply_url TEXT,
                         PRIMARY KEY (key, run));
    CREATE TABLE runs (id TEXT PRIMARY KEY, published TEXT, stats_json TEXT);
    CREATE TABLE cache (kind TEXT, key TEXT, value_json TEXT, updated TEXT, PRIMARY KEY (kind, key));
    CREATE INDEX jobs_status ON jobs(status);
    """,
}
_conn = None
UPGRADE = "jobs.db needs an upgrade or a legacy import; run `./js status` once in the data folder"
UNREADABLE = "jobs.db cannot be read now ({}); retry, or run `./js status` once in the data folder"


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = _conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        ver = int(row[0]) if row else 0
        for v in range(ver + 1, SCHEMA + 1):
            with _conn:
                _conn.executescript(MIGRATIONS[v])
                _conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(v),))
        _import_legacy(_conn)
    return _conn


@contextlib.contextmanager
def reading():
    """jobs.db for callers that must not write (the MCP read tools): the open connection, else a read-only one; None
    when there is no jobs.db yet. Never creates, migrates or imports; raises ValueError(UPGRADE) when that is due."""
    legacy = (DATA / "seen.jsonl").exists() or (DATA / "wwr_verify.json").exists()
    if _conn is not None or not DB_PATH.exists():
        if _conn is None and legacy:
            raise ValueError(UPGRADE)
        yield _conn
        return
    try:
        c = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:  # e.g. no read permission
        raise ValueError(UNREADABLE.format(e))
    try:
        c.row_factory = sqlite3.Row
        try:
            has_meta = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            row = has_meta and c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        except sqlite3.Error as e:  # locked by a long write, a crashed writer's journal, not a database
            raise ValueError(UNREADABLE.format(e))
        if legacy or not row or int(row[0]) < SCHEMA:
            raise ValueError(UPGRADE)
        yield c
    finally:
        c.close()


def _import_legacy(c: sqlite3.Connection) -> None:
    seen = DATA / "seen.jsonl"
    if seen.exists():
        rows = read_jsonl(seen)
        with c:
            c.executemany("INSERT OR IGNORE INTO jobs (key, first_seen, status, reason, score, profile) "
                          "VALUES (?,?,?,?,?,?)",
                          [(r["key"], r.get("date"), r.get("status"), r.get("reason"), r.get("score"),
                            r.get("profile")) for r in rows if r.get("key")])
        seen.rename(seen.with_name("seen.jsonl.migrated"))
    wwr = DATA / "wwr_verify.json"
    if wwr.exists():
        for k, v in json.loads(wwr.read_text() or "{}").items():
            cache_put("wwr", k, v, c)
        wwr.rename(wwr.with_name("wwr_verify.json.migrated"))


# ---------- jobs (seen list) ----------

def seen_keys() -> set:
    return {r[0] for r in connect().execute("SELECT key FROM jobs")}


def mark_seen(rows: list, run: str | None = None) -> int:
    """rows: dicts with key, status and optional reason/score/profile/source/company/title/location/posted/url.
    Existing keys are kept (first decision wins, like the old append-only seen list)."""
    cols = ("key", "first_seen", "status", "reason", "score", "profile", "source", "company", "title", "location",
            "posted", "url", "run")
    c = connect()
    before = c.total_changes
    with c:
        c.executemany(f"INSERT OR IGNORE INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      [tuple({**r, "first_seen": r.get("first_seen") or today(), "run": run}.get(k)
                             for k in cols) for r in rows])
    return c.total_changes - before


def seen_count(read_only: bool = False) -> int:
    if not read_only:
        return connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with reading() as c:
        return c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] if c else 0


# ---------- scores / runs ----------

def save_scores(run: str, scores: list) -> None:
    c = connect()
    with c:
        c.executemany("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (s["key"], run, s["profile"], s["score"], s.get("raw_score", s["score"]), s["verdict"],
             json.dumps(s.get("gates") or {}), json.dumps(s.get("strengths") or []),
             json.dumps(s.get("gaps") or []), json.dumps(s.get("flags") or []), s.get("apply_url"))
            for s in scores])


def save_run(run: str, stats: dict) -> None:
    c = connect()
    with c:
        c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (run, today(), json.dumps(stats)))


# ---------- cache ----------

def cache_get(kind: str, key: str):
    row = connect().execute("SELECT value_json FROM cache WHERE kind=? AND key=?", (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(kind: str, key: str, value, c: sqlite3.Connection | None = None) -> None:
    c = c or connect()
    with c:
        c.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (kind, key, json.dumps(value), today()))
