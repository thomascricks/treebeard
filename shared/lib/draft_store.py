#!/usr/bin/env python3
"""
draft_store.py — Clark's named-draft store with explicit versioning.

Target path:  /opt/treebeard/shared/lib/draft_store.py   (root:treebeard 640)
DB path:      /opt/treebeard/shared/drafts/drafts.db
Dir perms:    root:treebeard 2770  (setgid so WAL/SHM inherit group)
DB perms:     660 root:treebeard   (ENFORCED by init_db when run as root)

Purpose
-------
Save and retrieve named drafts with versioning — "cover-letter:acme" v1, v2,
v3 — where any version is addressable and retrievable by number. Clark #4/#5.

Why a SEPARATE store from ltm_clark (the deciding design point)
---------------------------------------------------------------
ltm_clark and draft_store are both versioned-append, but their RETRIEVAL
contracts are opposites, and retrieval contract is what defines a store:

  - ltm_clark.recall(key) returns LATEST-LIVE ONLY. Correct for memory: you
    want the current truth about a contact; history is behind history().
  - draft_store.get(name, version=N) must return an ARBITRARY version by
    number. Correct for drafts: you may prefer v2 over v3 and must be able to
    pull it by name+number. Version is a first-class address, not buried
    history.

Forcing draft semantics onto ltm_clark's key model would overload keys to fake
version addressing and fight recall()'s latest-wins contract. So: separate
store, version as a first-class column, get-by-version as a primary verb.

Discipline (inherited from record_log.py / ltm_clark.py verbatim)
-----------------------------------------------------------------
- Append-only: every save is a NEW ROW with an incremented version. Editing a
  draft never mutates a prior version.
- Write path NEVER raises: save() returns the new version int on success or
  None on refusal/failure, with a durable trace in draft_failures.log
  (append + fsync) plus a stderr line.
- init_db ENFORCES production ownership/perms and refuses non-root on prod.
- Reads open the store immutable.

NOT done here (same honest gate): tamper-resistance is convention-only;
hardening is a tracked future item, not silently skipped.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")
_TB_PROD_DB_PATH = "/opt/treebeard/shared/drafts/drafts.db"  # literal on purpose: production detection must not follow TREEBEARD_BASE

DB_PATH = f"{_TB_BASE}/shared/drafts/drafts.db"
FAIL_LOG_NAME = "draft_failures.log"

AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NAME_RE = re.compile(r"^[\x20-\x7e]{1,200}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY,   -- global monotonic row id
    ts_utc      TEXT    NOT NULL,      -- ISO 8601, UTC
    name        TEXT    NOT NULL,      -- draft handle: cover-letter:acme
    version     INTEGER NOT NULL,      -- 1-based, per-name, incremented on save
    body        TEXT    NOT NULL,      -- the actual draft text
    agent       TEXT    NOT NULL,      -- writer (clark)
    note        TEXT,                  -- optional per-version note ("tightened intro")
    ref         TEXT,                  -- optional pointer: board id, jd id
    UNIQUE(name, version)              -- one row per (name, version); race-safe
);
CREATE INDEX IF NOT EXISTS idx_drafts_name ON drafts (name);
CREATE INDEX IF NOT EXISTS idx_drafts_ts   ON drafts (ts_utc);
"""

# ---------------------------------------------------------------------------
# Internals (mirror ltm_clark / record_log)
# ---------------------------------------------------------------------------
def _connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _ro_connect(db_path):
    try:
        return sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5.0)
    except sqlite3.Error:
        return _connect(db_path)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate(name, agent):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"bad draft name: {name!r}")
    if not AGENT_RE.match(agent or ""):
        raise ValueError(f"bad agent name: {agent!r}")


def _failure_log_path(db_path):
    return os.path.join(os.path.dirname(db_path) or ".", FAIL_LOG_NAME)


def _record_failure(db_path, reason, name=None, agent=None):
    print(f"draft_store: {reason} (name={name!r} agent={agent!r})", file=sys.stderr)
    try:
        line = json.dumps({"ts_utc": _utcnow(), "reason": reason,
                           "name": name, "agent": agent}, ensure_ascii=False)
        fd = os.open(_failure_log_path(db_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o660)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def init_db(db_path=DB_PATH):
    is_production = os.path.abspath(db_path) == os.path.abspath(_TB_PROD_DB_PATH)
    if is_production and os.geteuid() != 0:
        raise PermissionError(
            "init_db() on the production store must run as root "
            "(it sets root:treebeard ownership and 2770/660 perms)."
        )
    db_dir = os.path.dirname(db_path) or "."
    os.makedirs(db_dir, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    if is_production and os.geteuid() == 0:
        import grp
        try:
            gid = grp.getgrnam("treebeard").gr_gid
        except KeyError:
            raise RuntimeError("group 'treebeard' does not exist; create it before init_db()")
        os.chown(db_dir, 0, gid)
        os.chmod(db_dir, 0o2770)
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.chown(p, 0, gid)
                os.chmod(p, 0o660)
        fail_log = _failure_log_path(db_path)
        open(fail_log, "a").close()
        os.chown(fail_log, 0, gid)
        os.chmod(fail_log, 0o660)


def save(name, body, *, agent="clark", note=None, ref=None, db_path=DB_PATH):
    """Save a new version of a named draft. Returns the new version int
    (1-based) on success, or None on refusal/failure. Never raises.

    The next version is (max existing for name) + 1, computed and inserted in
    one transaction; the UNIQUE(name, version) constraint makes a concurrent
    double-save fail loudly to the failure log rather than silently collide."""
    try:
        _validate(name, agent)
    except ValueError as e:
        _record_failure(db_path, f"refused save: {e}", name=name, agent=agent)
        return None
    if not isinstance(body, str):
        _record_failure(db_path, "body must be str", name=name, agent=agent)
        return None

    try:
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM drafts WHERE name = ?",
                (name,),
            ).fetchone()
            next_ver = int(row[0]) + 1
            conn.execute(
                "INSERT INTO drafts (ts_utc, name, version, body, agent, note, ref) "
                "VALUES (?,?,?,?,?,?,?)",
                (_utcnow(), name, next_ver, body, agent, note, ref),
            )
            conn.commit()
            return next_ver
        finally:
            conn.close()
    except sqlite3.Error as e:
        _record_failure(db_path, f"db error: {e}", name=name, agent=agent)
        return None


def _row_dict(row):
    cols = ("id", "ts_utc", "name", "version", "body", "agent", "note", "ref")
    return dict(zip(cols, row))


def get(name, version=None, db_path=DB_PATH):
    """Retrieve a draft. version=None returns the LATEST version; version=N
    returns that specific version. Returns the row dict, or None if the name
    (or that version) doesn't exist. Read-only, immutable connection."""
    conn = _ro_connect(db_path)
    try:
        if version is None:
            row = conn.execute(
                "SELECT id, ts_utc, name, version, body, agent, note, ref "
                "FROM drafts WHERE name = ? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, ts_utc, name, version, body, agent, note, ref "
                "FROM drafts WHERE name = ? AND version = ?",
                (name, int(version)),
            ).fetchone()
    finally:
        conn.close()
    return _row_dict(row) if row else None


def latest_version(name, db_path=DB_PATH):
    """Highest version number for name, or 0 if the name has no drafts."""
    conn = _ro_connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM drafts WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def list_versions(name, db_path=DB_PATH):
    """All versions of a named draft, newest first, metadata only (no bodies —
    keeps the listing cheap). Each dict has version, ts_utc, note, ref."""
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version, ts_utc, note, ref FROM drafts "
            "WHERE name = ? ORDER BY version DESC",
            (name,),
        ).fetchall()
    finally:
        conn.close()
    return [{"version": v, "ts_utc": t, "note": n, "ref": r} for v, t, n, r in rows]


def list_drafts(db_path=DB_PATH):
    """Every distinct draft name with its latest version and last-touched ts,
    most-recently-touched first. Clark's draft index."""
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, MAX(version) AS v, MAX(ts_utc) AS t "
            "FROM drafts GROUP BY name ORDER BY t DESC"
        ).fetchall()
    finally:
        conn.close()
    return [{"name": nm, "latest_version": v, "last_ts": t} for nm, v, t in rows]


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="draft_store_test_")
    dbp = os.path.join(d, "drafts.db")
    passed = 0
    total = 0

    def ok(label, cond):
        nonlocal passed, total
        total += 1
        passed += bool(cond)
        print(("PASS " if cond else "FAIL ") + label)

    try:
        init_db(dbp)
        ok("init_db on temp path (no root required)", os.path.exists(dbp))

        # --- save auto-increments version ---
        ok("first save returns version 1",
           save("cover-letter:acme", "Dear Hiring Manager, v1 body",
                note="first pass", db_path=dbp) == 1)
        ok("second save returns version 2",
           save("cover-letter:acme", "Dear Hiring Manager, v2 tightened",
                note="tightened intro", db_path=dbp) == 2)
        ok("third save returns version 3",
           save("cover-letter:acme", "Dear Hiring Manager, v3 different angle",
                note="new angle", db_path=dbp) == 3)

        # --- get latest vs get by version ---
        ok("get() with no version returns latest (v3)",
           get("cover-letter:acme", db_path=dbp)["version"] == 3)
        ok("get latest body is v3 body",
           "v3 different angle" in get("cover-letter:acme", db_path=dbp)["body"])
        ok("get by version=2 returns that exact version",
           get("cover-letter:acme", version=2, db_path=dbp)["version"] == 2)
        ok("get by version=2 returns v2 body (the one you preferred)",
           "v2 tightened" in get("cover-letter:acme", version=2, db_path=dbp)["body"])
        ok("get by version=1 still retrievable",
           "v1 body" in get("cover-letter:acme", version=1, db_path=dbp)["body"])
        ok("note round-trips per version",
           get("cover-letter:acme", version=2, db_path=dbp)["note"] == "tightened intro")

        # --- addressability helpers ---
        ok("latest_version reports 3", latest_version("cover-letter:acme", db_path=dbp) == 3)
        vs = list_versions("cover-letter:acme", db_path=dbp)
        ok("list_versions returns all 3, newest first",
           [x["version"] for x in vs] == [3, 2, 1])
        ok("list_versions omits bodies (cheap listing)", "body" not in vs[0])

        # --- multiple named drafts, independent version counters ---
        ok("second draft name starts its own v1",
           save("resume:acme", "resume tailored v1", db_path=dbp) == 1)
        ok("independent counter (acme cover still at 3)",
           latest_version("cover-letter:acme", db_path=dbp) == 3)
        names = {x["name"]: x["latest_version"] for x in list_drafts(db_path=dbp)}
        ok("list_drafts indexes both names with correct latest",
           names.get("cover-letter:acme") == 3 and names.get("resume:acme") == 1)

        # --- misses return None, not raise ---
        ok("get unknown name returns None", get("nope:nothing", db_path=dbp) is None)
        ok("get missing version returns None",
           get("cover-letter:acme", version=99, db_path=dbp) is None)

        # --- never-raise write path ---
        ok("bad name refused, returns None (no raise)",
           save("x" * 500, "body", db_path=dbp) is None)
        ok("bad agent refused, returns None (no raise)",
           save("k", "body", agent="Bad Name!", db_path=dbp) is None)
        ok("non-str body refused, returns None (no raise)",
           save("k2", {"not": "a string"}, db_path=dbp) is None)
        flog = _failure_log_path(dbp)
        ok("failure log captured the 3 refusals",
           os.path.exists(flog) and sum(1 for _ in open(flog)) == 3)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{passed}/{total} passed")
    return passed == total


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        init_db()
        print(f"initialized {DB_PATH}")
        sys.exit(0)
    print("usage: draft_store.py [--selftest | --init]", file=sys.stderr)
    sys.exit(2)
