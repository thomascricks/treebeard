#!/usr/bin/env python3
"""
record_log.py — Treebeard shared governance record store.

Target path:  /opt/treebeard/shared/lib/record_log.py   (root:treebeard 640)
DB path:      /opt/treebeard/shared/records/records.db
Dir perms:    root:treebeard 2770  (setgid so WAL/SHM files inherit group)
DB perms:     660 root:treebeard   (ENFORCED by init_db when run as root)

Purpose
-------
Append-only governance record store. This is the substrate primitive that
three agents independently converged on:

  - Han  #14 action_logger      -> kind="action"
  - Han  #15 refusal_logger     -> kind="refusal"
  - Treebeard #18 disagreement store -> kind="disagreement"

telemetry.py logs the *thinking* (every LLM call). This logs the *governance
events* — what an agent did, what it refused, what it disputed — with durable
provenance, separate from the day log so routine consolidation can never
filter or lose it (Treebeard #18's explicit requirement).

Design contract (inherited from telemetry.py, deliberately identical)
---------------------------------------------------------------------
- A failed OR refused record write MUST NEVER kill the action it records.
  log_event() returns False on a bad/failed write instead of raising. The
  loudness moves OUT of the caller's path: every refusal/failure leaves a
  durable trace in records_failures.log (append + fsync) AND a stderr line.
- init_db ENFORCES production ownership/perms (root:treebeard, dir 2770
  setgid, files 660) and refuses to run as non-root against the production
  path. Perms are a load-bearing control, not a docstring promise.

NOT done here (deliberately — same honest gate telemetry.py flags):
- TAMPER-RESISTANCE: append-only is convention-only. Any treebeard-group
  writer can in principle UPDATE/DELETE. Hardening (triggers, WORM, hash
  chain) is a future gate item, tracked, not silently skipped.
- AUTO-CAPTURE: this store is fed by EXPLICIT log_event() calls. Automatic
  action capture (hook in router.py at turn resolution) and refusal capture
  (needs a refusal *detector* that does not yet exist) are separate queue
  items. A disagreement is inherently explicit anyway.
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
_TB_PROD_DB_PATH = "/opt/treebeard/shared/records/records.db"  # literal on purpose: production detection must not follow TREEBEARD_BASE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = f"{_TB_BASE}/shared/records/records.db"
FAIL_LOG_NAME = "records_failures.log"

AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# The governance-event categories. Kept closed so the store can't silently
# accumulate garbage kinds — same discipline as telemetry's VALID_TIERS.
VALID_KINDS = ("action", "refusal", "disagreement")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY,
    ts_utc      TEXT    NOT NULL,   -- ISO 8601, UTC
    kind        TEXT    NOT NULL,   -- action | refusal | disagreement
    agent       TEXT    NOT NULL,   -- who logged it (clark, han, treebeard, ...)
    session_id  TEXT,               -- optional correlation id
    subject     TEXT,               -- short human handle: contact, task id, dispute topic
    basis       TEXT,               -- authorization basis / reasoning / law cited
    payload     TEXT,               -- JSON blob: the full structured record
    ref         TEXT                -- optional pointer: board id, memory id, prior record id
);
CREATE INDEX IF NOT EXISTS idx_records_ts    ON records (ts_utc);
CREATE INDEX IF NOT EXISTS idx_records_kind  ON records (kind);
CREATE INDEX IF NOT EXISTS idx_records_agent ON records (agent);
"""

# ---------------------------------------------------------------------------
# Internals  (mirrors telemetry.py verbatim in behavior)
# ---------------------------------------------------------------------------
def _autoinit_if_ephemeral(db_path):
    """W6/F6: a NON-PRODUCTION store that does not exist yet gets its schema
    created on first use.

    Why: a run with TREEBEARD_BASE pointed at a fresh tree (benchmark,
    autonomous task loop, any sandboxed run) found no table and fell soft --
    so isolated runs silently produced no record_log records at all, and no
    isolated result could honestly claim to have exercised the full path.

    This does NOT touch production. Production detection uses the literal
    _TB_PROD_DB_PATH, which deliberately does not follow TREEBEARD_BASE, and
    the production file always exists; the root-only ownership/permission
    contract in init_db() is untouched and still the only way that file is
    created. Non-production init is schema-only and unprivileged BY THE
    EXISTING CONTRACT -- this just calls it at the moment it is needed.

    Failure here is never fatal: the caller's existing fail-soft path still
    records the refusal exactly as it does today."""
    try:
        if os.path.abspath(db_path) == os.path.abspath(_TB_PROD_DB_PATH):
            return
        if os.path.exists(db_path):
            return
        init_db(db_path)
    except Exception:
        pass


def _connect(db_path=DB_PATH):
    """Open the store with concurrency-friendly pragmas."""
    _autoinit_if_ephemeral(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate(kind, agent):
    """Raises ValueError on a bad identifier. The write path catches this and
    refuses-and-records rather than letting it propagate."""
    if kind not in VALID_KINDS:
        raise ValueError(f"bad kind: {kind!r} (valid: {VALID_KINDS})")
    if not AGENT_RE.match(agent or ""):
        raise ValueError(f"bad agent name: {agent!r}")


def _failure_log_path(db_path):
    return os.path.join(os.path.dirname(db_path) or ".", FAIL_LOG_NAME)


def _record_failure(db_path, reason, kind=None, agent=None, subject=None):
    """Durable, out-of-band signal that a record did NOT get written. Append-only,
    fsync'd, best-effort. Never raises."""
    print(f"record_log: {reason} (kind={kind!r} agent={agent!r} subject={subject!r})",
          file=sys.stderr)
    try:
        line = json.dumps({
            "ts_utc": _utcnow(), "reason": reason,
            "kind": kind, "agent": agent, "subject": subject,
        }, ensure_ascii=False)
        fd = os.open(_failure_log_path(db_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o660)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # stderr already carried it; never let logging-of-a-failure fail the caller


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def init_db(db_path=DB_PATH):
    """
    Create schema and, for the real production store, ENFORCE directory and
    file ownership/permissions. Idempotent; run once at deploy.

    Production contract (db_path == DB_PATH):
      - MUST run as root (raises PermissionError otherwise).
      - Directory -> root:treebeard 2770 (setgid).
      - DB (+ -wal/-shm if present) -> root:treebeard 660.
      - Failure log pre-created root:treebeard 660 so every group agent can append.

    Test contract (any other db_path): schema only, unprivileged.
    """
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
            raise RuntimeError(
                "group 'treebeard' does not exist; create it before init_db()"
            )
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


def log_event(kind, agent, *, subject=None, basis=None,
              payload=None, ref=None, session_id=None, db_path=DB_PATH):
    """
    Append one governance record. Returns True on success, False on a
    refused/failed write — NEVER raises into the caller's path.

      kind     : "action" | "refusal" | "disagreement"
      agent    : who logged it (validated against AGENT_RE)
      subject  : short handle — contact, task id, dispute topic
      basis    : authorization basis / reasoning / law cited
      payload  : dict (JSON-serialized) or str — the full structured record
      ref      : optional pointer (board id, memory id, prior record id)

    A bad identifier or DB error is recorded durably (records_failures.log +
    stderr) and returns False, so a broken store can never kill the governance
    action it was trying to record.
    """
    try:
        _validate(kind, agent)
    except ValueError as e:
        _record_failure(db_path, f"refused row: {e}", kind=kind, agent=agent, subject=subject)
        return False

    if payload is not None and not isinstance(payload, str):
        try:
            payload = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            _record_failure(db_path, f"unserializable payload: {e}",
                            kind=kind, agent=agent, subject=subject)
            return False

    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO records "
                "(ts_utc, kind, agent, session_id, subject, basis, payload, ref) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_utcnow(), kind, agent, session_id, subject, basis, payload, ref),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        _record_failure(db_path, f"db error: {e}", kind=kind, agent=agent, subject=subject)
        return False


def records_since(hours=24, kind=None, agent=None, db_path=DB_PATH):
    """Read helper: return recent records (newest first) as list of dicts.
    Read-only; opens the store immutable so a reporter can never mutate it and
    never needs write access (the mode=ro/immutable lesson from the digest)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    q = ("SELECT id, ts_utc, kind, agent, session_id, subject, basis, payload, ref "
         "FROM records WHERE ts_utc > ?")
    args = [cutoff]
    if kind is not None:
        q += " AND kind = ?"; args.append(kind)
    if agent is not None:
        q += " AND agent = ?"; args.append(agent)
    q += " ORDER BY id DESC"
    try:
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5.0)
    except sqlite3.Error:
        conn = _connect(db_path)
    try:
        cols = ("id", "ts_utc", "kind", "agent", "session_id", "subject", "basis", "payload", "ref")
        return [dict(zip(cols, row)) for row in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


def counts_since(hours=24, db_path=DB_PATH):
    """Summary helper for Sun's digest: {kind: count} over the window."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5.0)
    except sqlite3.Error:
        conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM records WHERE ts_utc > ? GROUP BY kind",
            (cutoff,),
        ).fetchall()
        return {k: c for k, c in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="record_log_test_")
    dbp = os.path.join(d, "records.db")
    passed = 0
    total = 0

    def ok(label, cond):
        nonlocal passed, total
        total += 1
        passed += bool(cond)
        print(("PASS " if cond else "FAIL ") + label)

    try:
        init_db(dbp)  # unprivileged temp path -> schema only, no root needed
        ok("init_db on temp path (no root required)", os.path.exists(dbp))

        ok("action write returns True",
           log_event("action", "han", subject="acme-cto",
                     basis="Second Law: prepared, not sent",
                     payload={"drafted": "cold outreach v1"}, db_path=dbp) is True)
        ok("refusal write returns True",
           log_event("refusal", "han", subject="send-request",
                     basis="no operator authorization per contact",
                     payload={"requested": "send outreach", "gate": "D2"}, db_path=dbp) is True)
        ok("disagreement write returns True",
           log_event("disagreement", "treebeard", subject="build-order",
                     basis="memory read-path unbuilt; write-first is negative work",
                     ref="board:1", db_path=dbp) is True)

        ok("bad kind refused, returns False (no raise)",
           log_event("bogus", "han", db_path=dbp) is False)
        ok("bad agent refused, returns False (no raise)",
           log_event("action", "Bad Name!", db_path=dbp) is False)

        # failures were recorded out-of-band, not raised
        flog = _failure_log_path(dbp)
        ok("failure log captured both refusals",
           os.path.exists(flog) and sum(1 for _ in open(flog)) == 2)

        recs = records_since(hours=24, db_path=dbp)
        ok("read-back sees the 3 good rows", len(recs) == 3)
        ok("newest-first ordering", recs[0]["kind"] == "disagreement")
        ok("payload round-trips as JSON",
           json.loads(records_since(hours=24, kind="action", db_path=dbp)[0]["payload"])["drafted"] == "cold outreach v1")
        ok("kind filter works", len(records_since(hours=24, kind="refusal", db_path=dbp)) == 1)
        ok("agent filter works", len(records_since(hours=24, agent="treebeard", db_path=dbp)) == 1)

        c = counts_since(hours=24, db_path=dbp)
        ok("counts_since buckets by kind",
           c.get("action") == 1 and c.get("refusal") == 1 and c.get("disagreement") == 1)

        # unserializable payload is refused, not raised
        class X: pass
        ok("unserializable payload returns False",
           log_event("action", "clark", payload=X(), db_path=dbp) is False)
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
    print("usage: record_log.py [--selftest | --init]", file=sys.stderr)
    sys.exit(2)
