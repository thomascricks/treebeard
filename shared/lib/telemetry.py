#!/usr/bin/env python3
"""
telemetry.py — Treebeard shared LLM-call ledger.

Target path:  /opt/treebeard/shared/lib/telemetry.py   (root:treebeard 640)
DB path:      /opt/treebeard/shared/telemetry/telemetry.db
Dir perms:    root:treebeard 2770  (setgid so WAL/SHM files inherit group)
DB perms:     660 root:treebeard   (now ENFORCED by init_db when run as root)

Purpose
-------
Append-only audit ledger for every LLM call made by any agent, any tier.
This is the calibration instrument for the cost-aware router (how often do
high-difficulty trees actually reach the paid tier?) and the cognition-layer
half of the Fourth Law's audit trail. Systemd logs the runs; this logs the
thinking.

Changes in this revision (review fixes #4, #2, and the minimal half of #3)
--------------------------------------------------------------------------
- #4 WRITE-PATH FRAGILITY: validation no longer raises in the write path.
  A bad agent/tier/status used to raise ValueError *inside timed_call, before
  the wrapped LLM call ran* — a ledger problem that could kill the audited
  work, violating "a broken ledger must never kill the call it records."
  timed_call no longer validates before yielding; log_call now refuses a bad
  row by returning False instead of raising.
- #3 SILENT GAPS (minimal half): because #4 stops raising, a bad identifier
  could now vanish silently — trading a crash for a silent hole, which is
  worse for an audit trail. So every refused/failed write now leaves a
  durable, out-of-band trace in telemetry_failures.log (append + fsync) AND a
  stderr line. The loudness moves OUT of the call path, not away entirely.
  (The other half of #3 — Sun surfacing the failure count in the daily digest
  — is a Sun change, not done here.)
- #2 SELF-PROTECTION: init_db now ENFORCES the directory/file ownership and
  perms for the production ledger (root:treebeard, dir 2770 setgid, files
  660) and refuses to run as non-root against the production path. The perms
  are a load-bearing control; they no longer live only in the operator's
  fingers and a docstring.

NOT done here (deliberately — gate item, see review):
- #1 TAMPER-RESISTANCE: append-only is still convention-only. Any treebeard-
  group member with write access to the db file can still DELETE/UPDATE rows
  directly. The real fix (a single privileged writer the agents send to) is a
  governance-gate item, to land before any untrusted/earning agent (Clark,
  Han, Meeseeks) comes online. Today the only writers are sun, moon, and the
  operator — none adversarial — so this is acceptable for now, not forever.

Design notes
------------
- Python stdlib only (sqlite3). Preserves the egress allowlist posture.
- WAL mode + busy_timeout: multiple agents may log concurrently. SQLite
  serializes writers; a 5s busy timeout absorbs contention at this scale.
- Append-only by convention. No update/delete functions are provided here;
  see #1 above for why that is not yet a real control.
- A failed OR refused log write must never kill the call it was recording.
  log_call() catches everything and returns False; timed_call() re-raises the
  *wrapped call's* exception but never raises one of its own.

Public API
----------
init_db()                          -- create schema + enforce perms (deploy, as root)
log_call(...)                      -- one row, fire-and-forget; returns True/False
timed_call(agent, tier, model,...) -- context manager: times, logs, even on error
summary_since(hours=24)            -- per-tier rollup for Sun's digest
escalations_since(hours=24)        -- count of escalated calls for Sun's digest

Self-test
---------
    python3 telemetry.py --selftest      (uses a temp DB, touches nothing real)
"""

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")
_TB_PROD_DB_PATH = "/opt/treebeard/shared/telemetry/telemetry.db"  # literal on purpose: production detection must not follow TREEBEARD_BASE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_DIR  = f"{_TB_BASE}/shared/telemetry"
DB_PATH = os.path.join(DB_DIR, "telemetry.db")

# Sibling of whichever DB we write. Where refused/failed writes are recorded
# so a lost audit row is never silent (#3, minimal half).
FAIL_LOG_NAME = "telemetry_failures.log"

# Same agent-name discipline as statusboard.py: strict ASCII, no surprises.
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The router ladder. Anything else is refused at write time so the ledger
# can't silently accumulate garbage categories.
VALID_TIERS = ("local", "free", "cheap", "premium", "none")

# ok      = backend attempted, result accepted
# error   = backend attempted and failed (incl. validator reject, exhaustion)
# skipped = backend NOT attempted; a routing constraint excluded the rung
#
# 'skipped' is first-class so health and performance logic can exclude
# non-attempts from failure-rate denominators. Rungs skipped for context
# capacity previously mapped to 'error', which made local_model_family look unreliable when
# it had simply never been called.
VALID_STATUS = ("ok", "error", "skipped")

# Editable cost table, USD per million tokens (input, output).
# Local is genuinely zero-marginal-cost. Fill paid rows as tiers come online;
# callers may also pass an explicit cost_usd which always wins.
COST_PER_MTOK = {
    # "frontier_v2": (3.00, 15.00),   # example — verify current pricing
    "phi3:mini": (0.0, 0.0),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY,
    ts_utc          TEXT    NOT NULL,   -- ISO 8601, UTC
    agent           TEXT    NOT NULL,   -- who called (sun, moon, treebeard, ...)
    session_id      TEXT,               -- optional correlation id
    tier            TEXT    NOT NULL,   -- local | free | cheap | premium
    model           TEXT    NOT NULL,   -- e.g. phi3:mini
    router_reason   TEXT,               -- why the router chose this tier
    escalated_from  TEXT,               -- prior tier if this call is a retry upward
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    latency_ms      INTEGER,
    cost_usd        REAL,
    cost_basis      TEXT,               -- price_table | fallback | free | NULL
    status          TEXT    NOT NULL,   -- ok | error
    error           TEXT,               -- short error string when status=error
    synthetic       INTEGER,            -- 1 = not a real provider call
    canonical_agent TEXT,               -- agent after alias resolution
    skip_reason     TEXT,               -- why a tier was skipped
    resolved_model  TEXT,               -- model after resolution
    cost_source     TEXT,               -- where cost came from
    cost_ticks_raw  INTEGER,
    tokens_cached   INTEGER,
    tokens_reasoning INTEGER,
    tokens_total_billed INTEGER,
    usage_raw       TEXT                -- provider usage blob, verbatim
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts    ON llm_calls (ts_utc);
CREATE INDEX IF NOT EXISTS idx_llm_calls_agent ON llm_calls (agent);
"""

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _migrate_columns(con, table, cols):
    """Idempotent forward migration (E4, 2026-08-24). Writers gained columns
    over time; CREATE TABLE IF NOT EXISTS silently skips existing tables, so
    a FRESH store got the old schema and every INSERT failed. Production was
    unaffected only because its tables had been altered by hand. Fail-soft:
    never raises into the caller."""
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return
    if not have:
        return
    for name, decl in cols:
        if name not in have:
            try:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s"
                            % (table, name, decl))
            except Exception:
                pass


def _autoinit_if_ephemeral(db_path):
    """W6/F6: a NON-PRODUCTION ledger that does not exist yet gets its schema
    created on first use.

    Why: a run with TREEBEARD_BASE pointed at a fresh tree (benchmark,
    autonomous task loop, any sandboxed run) found no table and fell soft --
    so isolated runs silently produced no telemetry records at all, and no
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
    """Open the ledger with concurrency-friendly pragmas."""
    _autoinit_if_ephemeral(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")   # safe with WAL; durable enough for a ledger
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate(agent, tier, status):
    """Raises ValueError on a bad identifier. Callers in the write path catch
    this and refuse-and-record rather than letting it propagate (see #4)."""
    if not AGENT_RE.match(agent or ""):
        raise ValueError(f"bad agent name: {agent!r}")
    if tier not in VALID_TIERS:
        raise ValueError(f"bad tier: {tier!r} (valid: {VALID_TIERS})")
    if status not in VALID_STATUS:
        raise ValueError(f"bad status: {status!r}")


def _failure_log_path(db_path):
    """Failure log sits next to whatever DB we're writing — so --selftest's
    temp DB keeps its failures in the temp dir and never touches production."""
    return os.path.join(os.path.dirname(db_path) or ".", FAIL_LOG_NAME)


def _record_failure(db_path, reason, agent=None, tier=None, model=None, status=None):
    """
    Durable, out-of-band signal that an audit row did NOT get written (a
    refused identifier or a DB error). Append-only, fsync'd, best-effort.
    Never raises: if even this fails, stderr already carried the signal and
    there is nothing safe left to do. This is what keeps #4's "don't raise"
    from silently swallowing real problems (#3).
    """
    print(f"telemetry: {reason} (agent={agent!r} tier={tier!r} model={model!r})",
          file=sys.stderr)
    try:
        line = json.dumps({
            "ts_utc": _utcnow(), "reason": reason, "agent": agent,
            "tier": tier, "model": model, "status": status,
        }, ensure_ascii=False)
        fd = os.open(_failure_log_path(db_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o660)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # stderr already emitted; never let logging-of-a-failure fail the caller


def estimate_cost(model, tokens_in, tokens_out):
    """Cost at ingestion, from the shared pricing table (pricing.py).

    Was a one-entry stub ({'phi3:mini': (0,0)}) that returned None for every
    other model -- 1514 of 1710 ledger rows had NULL cost_usd as a result,
    while budget re-derived correct totals from its own table. Both now share
    pricing.py. None only when tokens are missing (call is unmeasurable).
    Unknown models take the conservative fallback, never free."""
    import pricing
    return pricing.ingest_cost(model, tokens_in, tokens_out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(db_path=DB_PATH):
    """
    Create schema and, for the real production ledger, ENFORCE the directory
    and file ownership/permissions the rest of the system depends on.
    Idempotent; run once at deploy.

    Production contract (db_path == DB_PATH):
      - MUST run as root (raises PermissionError otherwise) so it can
        chown/chmod. The perms are a load-bearing control, not a suggestion,
        so the code enforces them instead of trusting the operator to have
        set them by hand (review finding #2).
      - Directory -> root:treebeard 2770 (setgid: WAL/SHM inherit the group).
      - DB (+ -wal/-shm if present) -> root:treebeard 660.

    Test/custom contract (any other db_path):
      - Schema only. No root requirement, no chown (the 'treebeard' group may
        not exist and the path may be anywhere). Lets --selftest run
        unprivileged against a temp DB.
    """
    is_production = os.path.abspath(db_path) == os.path.abspath(_TB_PROD_DB_PATH)

    if is_production and os.geteuid() != 0:
        raise PermissionError(
            "init_db() on the production ledger must run as root "
            "(it sets root:treebeard ownership and 2770/660 perms)."
        )

    db_dir = os.path.dirname(db_path) or "."
    os.makedirs(db_dir, exist_ok=True)

    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_columns(conn, "llm_calls", [
            ("synthetic", "INTEGER"), ("canonical_agent", "TEXT"),
            ("skip_reason", "TEXT"), ("resolved_model", "TEXT"),
            ("cost_source", "TEXT"), ("cost_ticks_raw", "INTEGER"),
            ("tokens_cached", "INTEGER"), ("tokens_reasoning", "INTEGER"),
            ("tokens_total_billed", "INTEGER"), ("usage_raw", "TEXT")])
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
        # Directory: setgid so WAL/SHM siblings inherit the treebeard group.
        os.chown(db_dir, 0, gid)
        os.chmod(db_dir, 0o2770)
        # DB and its WAL/SHM siblings: group read/write, no world.
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.chown(p, 0, gid)
                os.chmod(p, 0o660)
        # Failure log: pre-create as root:treebeard 660 so EVERY group agent
        # can append to it directly — independent of directory-write perms.
        # Without this, the first failure relies on an agent creating the file
        # in the dir, and a later perms tightening could drop a different agent
        # back to stderr-only (the unread channel #3 exists to escape).
        fail_log = _failure_log_path(db_path)
        open(fail_log, "a").close()   # touch; append mode never truncates; idempotent
        os.chown(fail_log, 0, gid)
        os.chmod(fail_log, 0o660)


def log_call(agent, tier, model, status,
             tokens_in=None, tokens_out=None, latency_ms=None,
             cost_usd=None, router_reason=None, escalated_from=None,
             session_id=None, error=None, synthetic=None,
             canonical_agent=None, skip_reason=None, resolved_model=None,
             cost_source=None, cost_ticks_raw=None, tokens_cached=None,
             tokens_reasoning=None, tokens_total_billed=None, usage_raw=None,
             db_path=DB_PATH):
    """
    Write one ledger row. Returns True on success, False on any failure.

    Never raises — not for I/O problems and (changed this revision) not for
    bad identifiers either. A bad agent/tier/status used to raise; it now
    refuses the row, records the refusal durably (telemetry_failures.log +
    stderr), and returns False. Rationale: this runs in the audited call's
    path (directly and via timed_call), and the ledger must not be able to
    take down the work it is measuring (#4). The failure log (#3) is what
    keeps that refusal from being silent.
    """
    # Validate without raising into the caller: a refused row is recorded
    # out-of-band, not thrown.
    try:
        _validate(agent, tier, status)
    except ValueError as e:
        _record_failure(db_path, f"refused row: {e}", agent, tier, model, status)
        return False

    cost_basis = None
    if cost_usd is not None and cost_source:
        # Provider-reported cost: mirror the provenance into cost_basis so a
        # query filtering on that column does not see the MOST authoritative
        # rows as unclassified. The two columns must never disagree.
        cost_basis = cost_source
    if cost_usd is None:
        import pricing
        cost_usd, cost_basis = pricing.cost_and_basis(
            model, tokens_in, tokens_out, missing_tokens_as_zero=False)
        if cost_usd is None:
            cost_basis = None   # unmeasurable: do not pretend we know
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                """INSERT INTO llm_calls
                   (ts_utc, agent, session_id, tier, model, router_reason,
                    escalated_from, tokens_in, tokens_out, latency_ms,
                    cost_usd, cost_basis, status, error, synthetic,
                    canonical_agent, skip_reason, resolved_model, cost_source,
                    cost_ticks_raw, tokens_cached, tokens_reasoning,
                    tokens_total_billed, usage_raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utcnow(), agent, session_id, tier, model, router_reason,
                 escalated_from, tokens_in, tokens_out, latency_ms,
                 cost_usd, cost_basis, status, (error or None),
                 synthetic, (canonical_agent or agent), skip_reason,
                 resolved_model, cost_source, cost_ticks_raw, tokens_cached,
                 tokens_reasoning, tokens_total_billed, usage_raw),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as e:
        _record_failure(db_path, f"db write failed: {e}", agent, tier, model, status)
        return False


class _CallRecord:
    """Mutable holder the caller fills in inside the timed_call block."""
    def __init__(self):
        self.tokens_in = None
        self.tokens_out = None
        self.cost_usd = None       # explicit override; else estimated


@contextmanager
def timed_call(agent, tier, model, router_reason=None,
               escalated_from=None, session_id=None, db_path=DB_PATH):
    """
    Usage:
        with timed_call("treebeard", "local", "phi3:mini",
                        router_reason="triage:low") as rec:
            resp = ...make the call...
            rec.tokens_in  = resp["prompt_eval_count"]
            rec.tokens_out = resp["eval_count"]

    Times the block, logs ok on clean exit, logs error (with the exception
    string) and RE-RAISES on failure. The caller's exception is never eaten;
    the ledger's own faults never propagate.

    Changed this revision (#4): there is no longer a pre-yield validation
    step. A bad identifier must not abort the wrapped call — so validation is
    deferred to log_call, which refuses-and-records instead of raising. The
    body below always runs regardless of whether the row turns out loggable.
    """
    rec = _CallRecord()
    t0 = time.monotonic()
    try:
        yield rec
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        log_call(agent, tier, model, "error",
                 tokens_in=rec.tokens_in, tokens_out=rec.tokens_out,
                 latency_ms=latency, cost_usd=rec.cost_usd,
                 router_reason=router_reason, escalated_from=escalated_from,
                 session_id=session_id, error=f"{type(e).__name__}: {e}"[:500],
                 db_path=db_path)
        raise
    latency = int((time.monotonic() - t0) * 1000)
    log_call(agent, tier, model, "ok",
             tokens_in=rec.tokens_in, tokens_out=rec.tokens_out,
             latency_ms=latency, cost_usd=rec.cost_usd,
             router_reason=router_reason, escalated_from=escalated_from,
             session_id=session_id, db_path=db_path)


def summary_since(hours=24, db_path=DB_PATH):
    """
    Per-tier rollup for Sun's digest. Returns a list of dicts:
    [{tier, calls, errors, tokens_in, tokens_out, cost_usd}, ...]
    Read-only; safe for any group member.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT tier,
                      COUNT(*),
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),
                      COALESCE(SUM(tokens_in), 0),
                      COALESCE(SUM(tokens_out), 0),
                      COALESCE(SUM(cost_usd), 0.0)
               FROM llm_calls
               WHERE ts_utc >= ?
               GROUP BY tier
               ORDER BY tier""",
            (cutoff_iso,),
        ).fetchall()
    finally:
        conn.close()
    keys = ("tier", "calls", "errors", "tokens_in", "tokens_out", "cost_usd")
    return [dict(zip(keys, r)) for r in rows]


def escalations_since(hours=24, db_path=DB_PATH):
    """Count of calls that arrived at their tier by escalation. For Sun."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE ts_utc >= ? AND escalated_from IS NOT NULL",
            (cutoff_iso,),
        ).fetchone()
    finally:
        conn.close()
    return n


# ---------------------------------------------------------------------------
# Self-test (temp DB only — never touches the real ledger)
# ---------------------------------------------------------------------------

def _selftest():
    if not __debug__:
        # Assertions are stripped under -O: this selftest would verify
        # nothing, still exit 0, and be recorded as PASS. Fail closed.
        import sys as _sys
        print("telemetry: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        init_db(db)   # custom path -> schema only, no root needed

        checks = 0

        # 1. plain log
        assert log_call("treebeard", "local", "phi3:mini", "ok",
                        tokens_in=10, tokens_out=20, latency_ms=42, db_path=db)
        checks += 1

        # 2. timed_call success path
        with timed_call("treebeard", "local", "phi3:mini",
                        router_reason="selftest", db_path=db) as rec:
            rec.tokens_in, rec.tokens_out = 5, 7
            time.sleep(0.01)
        checks += 1

        # 3. timed_call error path re-raises AND logs
        try:
            with timed_call("treebeard", "local", "phi3:mini", db_path=db):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        else:
            raise AssertionError("timed_call swallowed the exception")
        checks += 1

        # 4. validation REFUSES garbage by returning False — and must NOT raise
        #    (changed: bad identifiers used to raise; now they are refused).
        for bad in (("Bad Agent", "local"), ("sun", "platinum")):
            assert log_call(bad[0], bad[1], "m", "ok", db_path=db) is False, bad
        checks += 1

        # 5. (#4 regression) a bad identifier must not abort the wrapped block
        ran = {"did": False}
        with timed_call("Bad Name", "local", "phi3:mini", db_path=db) as rec:
            ran["did"] = True
            rec.tokens_in = 1
        assert ran["did"], "timed_call aborted the wrapped block on a bad identifier"
        checks += 1

        # 6. rollups: 3 valid rows on 'local' (two ok, one error); refused rows
        #    wrote nothing to the ledger, only to the failure log.
        s = summary_since(hours=1, db_path=db)
        assert s and s[0]["tier"] == "local" and s[0]["calls"] == 3 and s[0]["errors"] == 1, s
        assert escalations_since(hours=1, db_path=db) == 0
        checks += 1

        # confirm the failure log captured the refusals (#3, minimal half)
        fail_log = _failure_log_path(db)
        assert os.path.exists(fail_log), "failure log was not written"
        with open(fail_log) as fh:
            fail_lines = [ln for ln in fh if ln.strip()]
        assert len(fail_lines) == 3, f"expected 3 recorded refusals, got {len(fail_lines)}"

        print(f"telemetry selftest: {checks}/6 OK  (failures recorded: {len(fail_lines)})")
        print(json.dumps(s, indent=2))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--init" in sys.argv:
        init_db()
        print(f"initialized {DB_PATH}")
    elif "--summary" in sys.argv:
        print(json.dumps(summary_since(), indent=2))
        print(f"escalations_24h: {escalations_since()}")
    else:
        print(__doc__)
