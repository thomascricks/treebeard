#!/usr/bin/env python3
"""
ltm_clark.py — Clark's long-term memory store.

Target path:  /opt/treebeard/shared/lib/ltm_clark.py   (root:treebeard 640)
DB path:      /opt/treebeard/shared/ltm_clark/ltm.db
Dir perms:    root:treebeard 2770  (setgid so WAL/SHM files inherit group)
DB perms:     660 root:treebeard   (ENFORCED by init_db when run as root)

Purpose
-------
Clark's private long-term memory: mutable knowledge about the job search —
contacts, statuses, positioning notes, recruiter behavior — that gets
corrected and updated as the search moves. Standalone read path: does NOT
depend on the unbuilt global consolidation/flagger pipeline.

Design ruling (Treebeard, overseer call)
----------------------------------------
VERSIONED APPEND, not overwrite. The deciding reason: on this substrate the
DBA has flagged overwrite-semantics as the corruption vector, because an
overwrite makes a corrupted write indistinguishable from an intentional
correction. Every other store on the box (telemetry, record_log) is
append-only for exactly this reason; breaking that for Clark's LTM needs a
positive justification and there isn't one. So:

  - Every remember/update/forget is a NEW ROW. Nothing mutates in place.
  - recall(key) returns the latest row per key: newest id wins.
  - "update" is a label for caller intent, not a description of the store.
  - forget(key) writes a RETRACTED TOMBSTONE row (status="retracted") so
    recall() returns None while history() still proves Clark once knew it.
    Ruling: without forget, killing a dead contact means overwriting it with
    a "dead" note — a semantic hack that poisons the record and defeats the
    audit trail versioned-append was chosen to protect.

Discipline (inherited from record_log.py verbatim)
--------------------------------------------------
- A failed write MUST NEVER kill Clark's action. Write verbs return
  True/False, never raise. Failures leave a durable trace in
  ltm_failures.log (append + fsync) plus a stderr line.
- init_db ENFORCES production ownership/perms and refuses to run as non-root
  against the production path.
- Reads open the store immutable so a reader can never mutate it and never
  needs write access.

Scope (unchanged by the store shape)
------------------------------------
LTM Clark holds CLARK's knowledge of the job search. Clark writes only its
own store. Treebeard reads across all stores under overseer authority. This
isolation is record_log discipline and does not change here.

NOT done here (deliberately, same honest gate as record_log):
- TAMPER-RESISTANCE: append-only is convention-only; any treebeard-group
  writer could in principle mutate rows. Hardening (triggers, hash chain) is
  a tracked future gate item, not silently skipped.
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
_TB_PROD_DB_PATH = "/opt/treebeard/shared/ltm_clark/ltm.db"  # literal on purpose: production detection must not follow TREEBEARD_BASE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = f"{_TB_BASE}/shared/ltm_clark/ltm.db"
FAIL_LOG_NAME = "ltm_failures.log"

AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Keys are caller-defined handles (contact:acme-cto, status:acme, note:pitch).
# Kept liberal but bounded: printable, no control chars, 1..200 chars.
KEY_RE = re.compile(r"^[\x20-\x7e]{1,200}$")

VALID_STATUS = ("live", "retracted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ltm (
    id          INTEGER PRIMARY KEY,   -- monotonic; newest id per key wins
    ts_utc      TEXT    NOT NULL,      -- ISO 8601, UTC
    key         TEXT    NOT NULL,      -- caller handle: contact:acme-cto, status:acme
    value       TEXT,                  -- JSON blob or str; NULL on a tombstone
    status      TEXT    NOT NULL,      -- live | retracted
    agent       TEXT    NOT NULL,      -- writer (clark)
    provenance  TEXT,                  -- where the fact came from / why written
    ref         TEXT                   -- optional pointer: board id, record id
);
CREATE INDEX IF NOT EXISTS idx_ltm_key    ON ltm (key);
CREATE INDEX IF NOT EXISTS idx_ltm_ts     ON ltm (ts_utc);
CREATE INDEX IF NOT EXISTS idx_ltm_status ON ltm (status);
"""

# ---------------------------------------------------------------------------
# Internals (mirror record_log.py behavior)
# ---------------------------------------------------------------------------
def _connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _ro_connect(db_path):
    """Immutable read connection; falls back to a normal RO connect if the
    immutable open is rejected (e.g. store not yet materialized)."""
    try:
        return sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5.0)
    except sqlite3.Error:
        return _connect(db_path)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate(key, agent):
    if not KEY_RE.match(key or ""):
        raise ValueError(f"bad key: {key!r}")
    if not AGENT_RE.match(agent or ""):
        raise ValueError(f"bad agent name: {agent!r}")


def _failure_log_path(db_path):
    return os.path.join(os.path.dirname(db_path) or ".", FAIL_LOG_NAME)


def _record_failure(db_path, reason, key=None, agent=None):
    print(f"ltm_clark: {reason} (key={key!r} agent={agent!r})", file=sys.stderr)
    try:
        line = json.dumps({"ts_utc": _utcnow(), "reason": reason,
                           "key": key, "agent": agent}, ensure_ascii=False)
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
    """Create schema and, for the production store, ENFORCE ownership/perms.
    Production (db_path == DB_PATH): must run as root; dir root:treebeard 2770,
    files 660, failure log pre-created 660. Test path: schema only."""
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


def _write_row(key, value, status, agent, provenance, ref, db_path):
    """Shared append path for every write verb. Never raises; returns bool."""
    try:
        _validate(key, agent)
    except ValueError as e:
        _record_failure(db_path, f"refused row: {e}", key=key, agent=agent)
        return False

    if status not in VALID_STATUS:
        _record_failure(db_path, f"bad status: {status!r}", key=key, agent=agent)
        return False

    if value is not None and not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            _record_failure(db_path, f"unserializable value: {e}", key=key, agent=agent)
            return False

    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO ltm (ts_utc, key, value, status, agent, provenance, ref) "
                "VALUES (?,?,?,?,?,?,?)",
                (_utcnow(), key, value, status, agent, provenance, ref),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        _record_failure(db_path, f"db error: {e}", key=key, agent=agent)
        return False


def remember(key, value, *, agent="clark", provenance=None, ref=None, db_path=DB_PATH):
    """Write a new live version of key. Returns True/False, never raises.
    A previously retracted key becomes live again by remembering it."""
    return _write_row(key, value, "live", agent, provenance, ref, db_path)


# update is a labeled alias for remember, per the ruling: in a versioned store,
# an update IS a new live row. Same intent, clearer call-site verb.
def update(key, value, *, agent="clark", provenance=None, ref=None, db_path=DB_PATH):
    """Correct a live fact. Identical mechanics to remember() — a new live row.
    Separate name so a call site reads as intent (correcting) vs first-write."""
    return _write_row(key, value, "live", agent, provenance, ref, db_path)


def forget(key, *, agent="clark", provenance=None, ref=None, db_path=DB_PATH):
    """Write a retracted tombstone for key. recall() then returns None, but
    history() still proves the key was known. Value is NULL on the tombstone."""
    return _write_row(key, None, "retracted", agent, provenance, ref, db_path)


def _row_dict(row):
    cols = ("id", "ts_utc", "key", "value", "status", "agent", "provenance", "ref")
    return dict(zip(cols, row))


def recall(key, db_path=DB_PATH):
    """Latest version of key. Returns the row dict if the newest row is live,
    or None if the key is unknown OR its newest row is a retracted tombstone.
    Read-only, immutable connection."""
    conn = _ro_connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, ts_utc, key, value, status, agent, provenance, ref "
            "FROM ltm WHERE key = ? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = _row_dict(row)
    return d if d["status"] == "live" else None


def recall_all(db_path=DB_PATH):
    """Every live key at its latest version, newest-updated first. Retracted
    keys are omitted. This is Clark's current working memory."""
    conn = _ro_connect(db_path)
    try:
        # latest row per key, then keep only those whose latest is live
        rows = conn.execute(
            "SELECT id, ts_utc, key, value, status, agent, provenance, ref "
            "FROM ltm WHERE id IN "
            "(SELECT MAX(id) FROM ltm GROUP BY key) "
            "ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_dict(r) for r in rows if r[4] == "live"]


def history(key, db_path=DB_PATH):
    """Full version history for key, newest first, tombstones included.
    This is the audit trail versioned-append exists to preserve."""
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ts_utc, key, value, status, agent, provenance, ref "
            "FROM ltm WHERE key = ? ORDER BY id DESC",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="ltm_clark_test_")
    dbp = os.path.join(d, "ltm.db")
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

        # --- write / read ---
        ok("remember returns True",
           remember("contact:acme-cto", {"name": "Vega", "responsive": True},
                    provenance="linkedin-intro", db_path=dbp) is True)
        r = recall("contact:acme-cto", db_path=dbp)
        ok("recall returns the live row", r is not None and r["status"] == "live")
        ok("value round-trips as JSON",
           json.loads(recall("contact:acme-cto", db_path=dbp)["value"])["name"] == "Vega")

        # --- versioned update: new row, latest wins, history preserved ---
        ok("update returns True",
           update("contact:acme-cto", {"name": "Vega", "responsive": False},
                  provenance="ghosted 2wks", db_path=dbp) is True)
        ok("recall reflects the update",
           json.loads(recall("contact:acme-cto", db_path=dbp)["value"])["responsive"] is False)
        h = history("contact:acme-cto", db_path=dbp)
        ok("history keeps both versions (append, not overwrite)", len(h) == 2)
        ok("history is newest-first",
           json.loads(h[0]["value"])["responsive"] is False and
           json.loads(h[1]["value"])["responsive"] is True)

        # --- forget: tombstone hides from recall, history survives ---
        ok("forget returns True",
           forget("contact:acme-cto", provenance="role filled, dead", db_path=dbp) is True)
        ok("recall returns None after forget", recall("contact:acme-cto", db_path=dbp) is None)
        h2 = history("contact:acme-cto", db_path=dbp)
        ok("history still proves the key was known (3 rows incl tombstone)", len(h2) == 3)
        ok("newest row is the retracted tombstone",
           h2[0]["status"] == "retracted" and h2[0]["value"] is None)

        # --- remember after forget revives the key ---
        ok("remember after forget revives",
           remember("contact:acme-cto", {"name": "Vega", "note": "new req opened"},
                    db_path=dbp) is True)
        ok("recall live again after revive",
           recall("contact:acme-cto", db_path=dbp) is not None)

        # --- recall_all: live keys only ---
        remember("status:acme", "phone screen booked", db_path=dbp)
        remember("note:pitch", "lead with integration story", db_path=dbp)
        forget("note:pitch", db_path=dbp)
        allk = {r["key"] for r in recall_all(db_path=dbp)}
        ok("recall_all includes live keys", "contact:acme-cto" in allk and "status:acme" in allk)
        ok("recall_all omits retracted keys", "note:pitch" not in allk)

        # --- never-raise write path ---
        ok("bad key refused, returns False (no raise)",
           remember("x" * 500, "v", db_path=dbp) is False)
        ok("bad agent refused, returns False (no raise)",
           remember("k", "v", agent="Bad Name!", db_path=dbp) is False)
        class Z: pass
        ok("unserializable value returns False (no raise)",
           remember("k2", Z(), db_path=dbp) is False)
        flog = _failure_log_path(dbp)
        ok("failure log captured the 3 refusals",
           os.path.exists(flog) and sum(1 for _ in open(flog)) == 3)

        # --- recall of unknown key ---
        ok("recall unknown key returns None", recall("nope:nothing", db_path=dbp) is None)
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
    print("usage: ltm_clark.py [--selftest | --init]", file=sys.stderr)
    sys.exit(2)
