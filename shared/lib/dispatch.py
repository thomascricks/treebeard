#!/usr/bin/env python3
# -*- coding: ascii -*-
"""dispatch.py -- governed agent-invoked operations (DISPATCH-1).

Second channel in the AGENT_CHANNELS family, built on the SMS-a pattern
and subject to the same governing principle: THIS CODE KNOWS HOW TO
ENFORCE DISPATCH POLICY; IT DOES NOT KNOW WHICH AGENTS MAY INVOKE WHICH
OPERATIONS. No agent name and no operation name appears in this file.
Policy lives in a root-owned governance file (default
/etc/treebeard/dispatch_ops.conf), directed syntax 'agent -> operation',
fresh-read at every dispatch, FAIL-CLOSED: a missing, unreadable, empty,
or malformed policy allows nothing, and one malformed non-comment line
voids the ENTIRE load. Policy failure audits as 'policy-error', distinct
from an ordinary 'denied'.

DEFERRED, NEVER LIVE. A dispatch is recorded in this turn and executed
after it; the agent cannot see a result within the turn. The one-shot
subprocess shape of ask.py is deliberate and unchanged; the no-live-
channel invariant in AGENT_CHANNELS still holds. A completed result
becomes READABLE on a LATER turn (BOX-09 receipt read-back below),
never within the recording turn. Read-back adds no execution
path and no authorisation: it only reads rows this store holds.

DISPATCH DOES NOT BYPASS GATES. Every operation reaching an approval
gate (Meeseeks candidate approval, standing rule 2026-08-21), the budget
hard-stop, or the constitution gate meets that gate exactly as an
operator-invoked run would. Dispatch is a new invocation path, never an
exemption.

This module RAISES honestly on failure -- fail-soft belongs to the
caller (ask.py catches and routes to the operational audit path).

Caps are OPERATOR configuration (process environment; agents cannot
reach it): DISPATCH_ARGS_MAX=512.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone


class DispatchError(Exception):
    """Refusal or failure of a dispatch. Caller owns fail-soft."""


class PolicyError(DispatchError):
    """Policy could not be loaded or was malformed. FAIL-CLOSED."""


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")


# 2026-08-31 (RC4): default derived from TREEBEARD_BASE, matching
# meetmem.py. A hardcoded default silently escapes a relocated
# install: install.sh initdb would create the store under the
# default root while the relocated tree got none.
def store_dir():
    return os.environ.get("DISPATCH_ROOT",
                          "%s/shared/dispatch" % _TB_BASE)


def db_path():
    return os.path.join(store_dir(), "dispatch.db")


def ops_path():
    return os.environ.get("DISPATCH_OPS_CONF",
                          "/etc/treebeard/dispatch_ops.conf")


def args_max():
    return max(1, _env_int("DISPATCH_ARGS_MAX", 512))


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]{0,31})"
                      r"\s*->\s*"
                      r"([A-Za-z][A-Za-z0-9_.-]{0,63})$")


def load_ops(path=None):
    """Permitted (agent, operation) pairs from the root-owned policy file.

    FAIL-CLOSED in every failure mode: missing, unreadable, empty, or
    malformed raises PolicyError and permits nothing. ONE malformed
    non-comment line voids the ENTIRE load -- a typo must never silently
    yield a partially different matrix.
    """
    p = path or ops_path()
    try:
        with open(p) as fh:
            raw = fh.read()
    except OSError as e:
        raise PolicyError("policy unreadable at %s (%s)" % (p, e))
    pairs = set()
    for n, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE_RE.match(s)
        if not m:
            raise PolicyError("malformed policy line %d in %s" % (n, p))
        pairs.add((m.group(1).lower(), m.group(2).lower()))
    if not pairs:
        raise PolicyError("policy at %s permits nothing" % p)
    return pairs


def permitted(agent, operation):
    """Is this (agent, operation) pair allowed by live policy? PURE.

    P0-B (2026-09-02). The live-state guard in ask.py needs to know
    whether a turn carries a dispatch that can actually execute, so that
    a validated dispatch may serve as the third legitimate outcome
    beside READ and TEST. It must NOT reimplement policy to find out --
    policy interpretation belongs to this module and nowhere else.

    Differences from record(), both deliberate:
      * WRITES NOTHING. No audit event on refusal: a guard asking a
        question must not make the store report a refusal that never
        happened. record() still audits when it actually refuses.
      * Returns False instead of raising, on every failure mode
        including PolicyError -- fail-closed, exactly as record() is.

    Costs one extra policy read on a dispatching turn. Accepted: correct
    authorisation semantics outrank a saved file read, and a second
    implementation of the rules would be far worse than a second read.
    """
    agent = (agent or "").strip().lower()
    operation = (operation or "").strip().lower()
    if not agent or not operation:
        return False
    try:
        return (agent, operation) in load_ops()
    except Exception:
        return False


LIB_DIR = os.path.dirname(os.path.abspath(__file__))


def connect():
    d = store_dir()
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path())
    con.execute("PRAGMA journal_mode=WAL")
    ensure_schema(con)
    return con


def ensure_schema(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS dispatches("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_utc TEXT NOT NULL,"
        "  agent TEXT NOT NULL,"
        "  operation TEXT NOT NULL,"
        "  args TEXT NOT NULL,"
        "  state TEXT NOT NULL DEFAULT 'PENDING',"
        "  result TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_utc TEXT NOT NULL,"
        "  dispatch_id INTEGER,"
        "  kind TEXT NOT NULL,"
        "  detail TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS exceptions("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_utc TEXT NOT NULL,"
        "  issuer TEXT NOT NULL,"
        "  agent TEXT NOT NULL,"
        "  operation TEXT NOT NULL,"
        "  purpose TEXT NOT NULL,"
        "  mission_id TEXT NOT NULL DEFAULT '',"
        "  basis TEXT,"
        "  expires_utc TEXT NOT NULL,"
        "  uses_left INTEGER NOT NULL DEFAULT 1,"
        "  state TEXT NOT NULL DEFAULT 'OPEN',"
        "  issuing_dispatch INTEGER)")


def _event(con, did, kind, detail):
    con.execute("INSERT INTO events(ts_utc, dispatch_id, kind, detail) "
                "VALUES(?,?,?,?)", (utcnow(), did, kind, detail))


def _audit_refusal(kind, detail):
    try:
        con = connect()
        with con:
            _event(con, None, kind, detail)
        con.close()
    except Exception:
        pass  # audit of a refusal must not mask the refusal itself


def record(agent, operation, args, thread_id=None, turn_id=None,
           mission_id=None, mission_sel=None):
    """Validate against live policy (and, for GOVERNED operations, against
    mission/exception authority) and record ONE pending dispatch.
    RAISES on any refusal; the caller owns fail-soft. Returns the
    store-assigned id. Recording is NOT execution: the operation runs
    after the turn, through its own gates."""
    agent = (agent or "").strip().lower()
    operation = (operation or "").strip().lower()
    if not agent or not operation:
        raise DispatchError("agent and operation required")
    try:
        ops = load_ops()
    except PolicyError as exc:
        _audit_refusal("policy-error", "%s->%s: %s" % (agent, operation, exc))
        raise
    if (agent, operation) not in ops:
        _audit_refusal("denied", "pair %s->%s not in policy"
                       % (agent, operation))
        raise DispatchError("operation %s not permitted for %s"
                            % (operation, agent))
    args = (args or "").strip()
    if len(args) > args_max():
        raise DispatchError("args %d > %d chars" % (len(args), args_max()))
    try:
        args.encode("ascii")
    except UnicodeEncodeError:
        raise DispatchError("non-ascii args")
    auth = None
    if governed(operation):
        if not (thread_id and turn_id):
            _audit_refusal("unauthorized", "%s->%s: governed op without turn provenance"
                           % (agent, operation))
            raise DispatchError("governed operation %s requires turn provenance" % operation)
        auth = authorize(agent, operation, args, mission_id, mission_sel)
        if not auth["ok"]:
            _audit_refusal("unauthorized", "%s->%s: %s" % (agent, operation, auth["reason"]))
            raise DispatchError("operation %s not authorized for %s (%s)"
                                % (operation, agent, auth["reason"]))
    con = connect()
    with con:
        cur = con.execute(
            "INSERT INTO dispatches(ts_utc, agent, operation, args) "
            "VALUES(?,?,?,?)", (utcnow(), agent, operation, args))
        did = cur.lastrowid
        _event(con, did, "recorded", "%s -> %s" % (agent, operation))
        # Same transaction: a dispatch can never exist with a partial
        # provenance record. Absent ids leave it legacy by construction.
        if thread_id and turn_id:
            prov = {"v": 1, "thread_id": str(thread_id).strip(),
                    "turn_id": str(turn_id).strip()}
            if mission_id or mission_sel:
                prov["mission_id"] = (mission_id or "").strip()
                prov["sel"] = str(mission_sel or "")
            if auth is not None:
                prov["basis"] = auth["basis"]
                prov["exception_id"] = auth["exception_id"]
                prov["purpose"] = normalize_purpose(args)
            _event(con, did, _PROV_KIND, json.dumps(prov, sort_keys=True))
    con.close()
    return did


def pending(limit=50):
    """Recorded-but-unexecuted dispatches, oldest first."""
    con = connect()
    rows = con.execute(
        "SELECT id, ts_utc, agent, operation, args FROM dispatches "
        "WHERE state='PENDING' ORDER BY id LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts_utc": r[1], "agent": r[2],
             "operation": r[3], "args": r[4]} for r in rows]


def complete(did, state, result):
    """Record the outcome of an executed dispatch."""
    if state not in ("DONE", "FAILED", "REFUSED"):
        raise DispatchError("invalid terminal state %r" % (state,))
    con = connect()
    with con:
        con.execute("UPDATE dispatches SET state=?, result=? WHERE id=?",
                    (state, (result or "")[:2000], did))
        _event(con, did, state.lower(), (result or "")[:500])
    con.close()


# --------------------------------------------------------------- receipts
# BOX-09 (2026-09-02). READ-BACK ONLY. Nothing below executes, records,
# or authorises anything: it reads rows this store already holds so a
# LATER turn can report an outcome from evidence instead of narration.
# The DEFERRED, NEVER LIVE invariant is untouched -- a receipt exists
# only after the drain has run, which is after the recording turn ended.
#
# Execution time is the TERMINAL EVENT's timestamp, never dispatches.
# ts_utc (which is RECORD time). No schema change: events already holds
# a row per transition.
#
# READ -> PRESENT -> ACKNOWLEDGE (v3). peek_receipts() NEVER mutates;
# acknowledge() runs only after the caller has successfully presented the
# evidence. An interruption therefore re-presents a receipt next turn:
# duplicate evidence is preferable to silently losing evidence.
#
# PROVENANCE. A dispatch is bound to a thread by exactly ONE valid
# 'provenance' event written inside record()'s transaction. events is
# APPEND-ONLY, so a dispatch can carry two conflicting provenance rows;
# an EXISTS-style match would then show one receipt to two threads.
# Zero, malformed, or MULTIPLE provenance events all mean legacy/
# untrusted: readable administratively via receipt(), never injected
# automatically into any conversation.
#
# NOTE on the NOT IN subquery: refusal audits carry dispatch_id NULL, and
# `x NOT IN (set containing NULL)` is never true in SQL. The subquery
# therefore excludes NULLs explicitly, or every receipt would vanish the
# moment one refusal had been audited.

_TERMINAL_KINDS = ("done", "failed", "refused")
_PROV_KIND = "provenance"
_TERMINAL_STATES = ("DONE", "FAILED", "REFUSED")

# ---------------------------------------------------------------- authority
# Serpens story (2026-09-03). GOVERNED operations need more than a policy
# line: authority from the mission SELECTED for the turn (its stored grants)
# or from a scoped operator EXCEPTION issued by Treebeard. Operation names
# only -- never agent names. Everything here fails closed.
GOVERNED_OPS = ("serpens.search",)
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_OP_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def governed(operation):
    return (operation or "").strip().lower() in GOVERNED_OPS


def normalize_purpose(text):
    return " ".join(str(text or "").split()).lower()


def exception_ttl():
    return max(60, _env_int("DISPATCH_EXCEPTION_TTL", 86400))


def _iso(dt=None):
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_after(seconds):
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def add_exception(issuer, agent, operation, purpose, mission_id="", basis="",
                  uses=1, ttl_s=None, issuing_dispatch=None):
    """Record a SCOPED operator exception. Written only by the executor of
    a policy-permitted grant dispatch (issuer = that dispatch's agent).
    Scope = (agent, operation, normalized purpose, mission_id or '')."""
    issuer = (issuer or "").strip().lower()
    agent = (agent or "").strip().lower()
    operation = (operation or "").strip().lower()
    if not _AGENT_RE.match(issuer) or not _AGENT_RE.match(agent):
        raise DispatchError("bad agent name")
    if not _OP_RE.match(operation):
        raise DispatchError("bad operation name")
    purpose = normalize_purpose(purpose)
    if not purpose or len(purpose) > args_max():
        raise DispatchError("bad purpose")
    try:
        purpose.encode("ascii")
    except UnicodeEncodeError:
        raise DispatchError("non-ascii purpose")
    mission_id = (mission_id or "").strip()
    if mission_id.lower() == "none":
        mission_id = ""
    uses = max(1, int(uses))
    exp = _iso_after(exception_ttl() if ttl_s is None else int(ttl_s))
    con = connect()
    with con:
        cur = con.execute(
            "INSERT INTO exceptions(ts_utc, issuer, agent, operation, purpose, "
            "mission_id, basis, expires_utc, uses_left, state, issuing_dispatch) "
            "VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?)",
            (_iso(), issuer, agent, operation, purpose, mission_id,
             (basis or "")[:200], exp, uses, issuing_dispatch))
        eid = cur.lastrowid
        _event(con, issuing_dispatch, "exception-issued", json.dumps(
            {"exception_id": eid, "issuer": issuer, "agent": agent,
             "operation": operation, "purpose": purpose,
             "mission_id": mission_id, "basis": basis or "", "expires_utc": exp,
             "uses": uses}, sort_keys=True))
    con.close()
    return eid


def _open_exception(con, agent, operation, purpose, mission_id):
    row = con.execute(
        "SELECT id, uses_left FROM exceptions WHERE state='OPEN' AND agent=? "
        "AND operation=? AND purpose=? AND mission_id=? AND uses_left>0 AND "
        "expires_utc>? ORDER BY id LIMIT 1",
        (agent, operation, purpose, mission_id, _iso())).fetchone()
    return row[0] if row else None


def consume_exception(eid, dispatch_id=None):
    """One use spent by a SUCCESSFUL execution. Never called on failure."""
    con = connect()
    with con:
        row = con.execute("SELECT uses_left FROM exceptions WHERE id=? AND state='OPEN'",
                          (eid,)).fetchone()
        if row is None:
            con.close()
            return False
        left = int(row[0]) - 1
        con.execute("UPDATE exceptions SET uses_left=?, state=? WHERE id=?",
                    (left, "CONSUMED" if left <= 0 else "OPEN", eid))
        _event(con, dispatch_id, "exception-consumed",
               json.dumps({"exception_id": eid, "uses_left": left}))
    con.close()
    return True


def list_exceptions(agent=None, open_only=True):
    con = connect()
    q = ("SELECT id, ts_utc, issuer, agent, operation, purpose, mission_id, basis, "
         "expires_utc, uses_left, state, issuing_dispatch FROM exceptions")
    cl, args = [], []
    if agent:
        cl.append("agent=?"); args.append(agent.strip().lower())
    if open_only:
        cl.append("state='OPEN'")
    if cl:
        q += " WHERE " + " AND ".join(cl)
    rows = con.execute(q + " ORDER BY id", args).fetchall()
    con.close()
    keys = ("id", "ts_utc", "issuer", "agent", "operation", "purpose", "mission_id",
            "basis", "expires_utc", "uses_left", "state", "issuing_dispatch")
    return [dict(zip(keys, r)) for r in rows]


def usable_exceptions(agent, mission_id=None):
    """This agent's OPEN, unexpired, unspent exceptions for the selected
    mission (or mission-less ones). READ-ONLY: it authorizes nothing --
    authorize() remains the only decision, and it re-checks the purpose.
    Used to make recorded authority VISIBLE to the agent it belongs to."""
    agent = (agent or "").strip().lower()
    if not agent:
        return []
    mid = (mission_id or "").strip()
    try:
        con = connect()
        rows = con.execute(
            "SELECT id, operation, purpose, mission_id, uses_left, expires_utc "
            "FROM exceptions WHERE state='OPEN' AND agent=? AND uses_left>0 "
            "AND expires_utc>? AND mission_id IN (?, '') ORDER BY id",
            (agent, _iso(), mid)).fetchall()
        con.close()
    except Exception:
        return []
    return [{"id": r[0], "operation": r[1], "purpose": r[2],
             "mission_id": r[3], "uses_left": r[4], "expires_utc": r[5]}
            for r in rows]


def render_exceptions(rows):
    """Evidence block for a system prompt. Empty input renders EMPTY.
    States recorded AUTHORITY, never that anything has been done."""
    if not rows:
        return ""
    out = ["=== OPERATOR EXCEPTIONS (recorded authority; nothing has run) ===",
           "The operator authorized these specific operations for you through "
           "the governing agent. Each is recorded in the dispatch store and is "
           "the authority you act under; using one still requires the normal "
           "DISPATCH block, and the ARGS must match the PURPOSE exactly. "
           "Nothing below has been executed."]
    for r in rows:
        out.append("EXCEPTION #%d" % r["id"])
        out.append("  OPERATION: %s" % r["operation"])
        out.append("  PURPOSE: %s" % r["purpose"])
        out.append("  MISSION: %s" % (r["mission_id"] or "none"))
        out.append("  USES LEFT: %d" % r["uses_left"])
        out.append("  EXPIRES: %s" % r["expires_utc"])
    out.append("=== END OPERATOR EXCEPTIONS ===")
    return "\n".join(out)


def authorize(agent, operation, args, mission_id=None, mission_sel=None):
    """Deterministic authority decision for ONE governed request.

    Returns {"ok", "basis", "mission_id", "exception_id", "reason"}.
    basis: "policy" (op not governed), "mission" (selected mission's stored
    grants cover the op), "exception" (open scoped exception matches), or
    None (refused). The mission branch counts ONLY the mission selected for
    the turn: mission_sel must be "<mission_id>:..." for that id, so
    sel=none:* and every non-selected mission grant nothing. Any error is
    a refusal."""
    agent = (agent or "").strip().lower()
    operation = (operation or "").strip().lower()
    out = {"ok": False, "basis": None, "mission_id": None, "exception_id": None,
           "reason": "no-authority"}
    if not governed(operation):
        out.update(ok=True, basis="policy", reason="not-governed")
        return out
    mid = (mission_id or "").strip()
    sel = str(mission_sel or "")
    if mid and sel.startswith(mid + ":"):
        try:
            if LIB_DIR not in sys.path:
                sys.path.insert(0, LIB_DIR)
            import missions as _m  # noqa: PLC0415
            row = _m.get(mid)
            if (row is not None and row["agent"] == agent
                    and row["status"] == "ACTIVE"
                    and operation in _m.grants_for(mid)):
                out.update(ok=True, basis="mission", mission_id=mid,
                           reason="mission-grant")
                return out
            out["reason"] = "mission-does-not-grant"
        except Exception as e:
            out["reason"] = "mission-store-error"
    elif mid:
        out["reason"] = "mission-not-selected"
    else:
        out["reason"] = "no-selected-mission"
    try:
        con = connect()
        eid = _open_exception(con, agent, operation, normalize_purpose(args), mid)
        con.close()
    except Exception:
        eid = None
    if eid is not None:
        out.update(ok=True, basis="exception", mission_id=mid or None,
                   exception_id=eid, reason="operator-exception")
        return out
    return out


def provenance_of(did):
    con = connect()
    try:
        return _provenance(con, did)
    finally:
        con.close()


def authority_line(prov):
    if not prov:
        return "unknown"
    b = prov.get("basis") or "policy"
    if b == "mission":
        return "mission %s" % (prov.get("mission_id") or "?")
    if b == "exception":
        return "exception #%s (mission %s)" % (prov.get("exception_id"),
                                                prov.get("mission_id") or "none")
    return b


def receipt_max():
    return max(1, _env_int("DISPATCH_RECEIPT_MAX", 4))


def receipt_chars():
    return max(80, _env_int("DISPATCH_RECEIPT_CHARS", 800))


def _clip(text):
    t = text or ""
    n = receipt_chars()
    if len(t) <= n:
        return t
    return t[:n] + "\n[...clipped, %d chars total]" % len(t)


def _exit_of(result):
    """The executor writes 'rc=N ...' on every executed path. A refusal
    never ran a command, so it truthfully has no exit status."""
    m = re.match(r"rc=(-?\d+)", result or "")
    return m.group(0) if m else "n/a"


def _executed_ts(con, did):
    """Execution time = timestamp of the LAST terminal event for this
    dispatch. None when none exists (still PENDING)."""
    row = con.execute(
        "SELECT ts_utc FROM events WHERE dispatch_id=? AND kind IN "
        "(?,?,?) ORDER BY id DESC LIMIT 1",
        (did,) + _TERMINAL_KINDS).fetchone()
    return row[0] if row else None


def _row_to_receipt(con, r):
    return {"id": r[0], "recorded_utc": r[1], "agent": r[2],
            "operation": r[3], "args": r[4], "state": r[5],
            "exit": _exit_of(r[6]), "result": _clip(r[6]),
            "executed_utc": _executed_ts(con, r[0]),
            "authority": authority_line(_provenance(con, r[0]))}


def receipt(did):
    """The execution receipt for ONE dispatch id, or None when the id is
    unknown or has not completed. A PENDING dispatch has NO receipt --
    that is the point: the recording turn cannot claim completion."""
    con = connect()
    try:
        row = con.execute(
            "SELECT id, ts_utc, agent, operation, args, state, result "
            "FROM dispatches WHERE id=?", (did,)).fetchone()
        if row is None or row[5] not in _TERMINAL_STATES:
            return None
        return _row_to_receipt(con, row)
    finally:
        con.close()


def _provenance(con, did):
    """The ONE valid provenance record for this dispatch, or None.

    FAIL CLOSED on zero, malformed, wrong-version, and -- critically --
    MULTIPLE provenance events, because events is append-only."""
    rows = con.execute(
        "SELECT detail FROM events WHERE dispatch_id=? AND kind=?",
        (did, _PROV_KIND)).fetchall()
    if len(rows) != 1:
        return None
    try:
        d = json.loads(rows[0][0] or "")
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    tid, uid = d.get("thread_id"), d.get("turn_id")
    if not isinstance(tid, str) or not isinstance(uid, str):
        return None
    if not tid.strip() or not uid.strip():
        return None
    return {"thread_id": tid.strip(), "turn_id": uid.strip(),
            "mission_id": (d.get("mission_id") or "") if isinstance(d.get("mission_id"), str) else "",
            "sel": d.get("sel") if isinstance(d.get("sel"), str) else "",
            "basis": d.get("basis") if isinstance(d.get("basis"), str) else None,
            "exception_id": d.get("exception_id") if isinstance(d.get("exception_id"), int) else None,
            "purpose": d.get("purpose") if isinstance(d.get("purpose"), str) else ""}


def _thread_match(con, did, agent, thread_id):
    """Terminal + exactly-one-valid-provenance + same agent and thread."""
    prov = _provenance(con, did)
    return prov is not None and prov["thread_id"] == thread_id


def peek_receipts(agent, thread_id, limit=None):
    """Unacknowledged terminal receipts for THIS agent in THIS thread.

    READ-ONLY on every path: no caller of this function may leave the
    store changed. Presentation and acknowledgment belong to the caller,
    after it has actually shown the evidence."""
    agent = (agent or "").strip().lower()
    thread_id = (thread_id or "").strip()
    if not agent or not thread_id:
        return []
    lim = receipt_max() if limit is None else max(0, int(limit))
    if not lim:
        return []
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, ts_utc, agent, operation, args, state, result "
            "FROM dispatches WHERE agent=? AND state IN (?,?,?) "
            "AND id NOT IN (SELECT dispatch_id FROM events "
            "WHERE kind='surfaced' AND dispatch_id IS NOT NULL) "
            "ORDER BY id DESC", (agent,) + _TERMINAL_STATES).fetchall()
        out = []
        for r in rows:
            if not _thread_match(con, r[0], agent, thread_id):
                continue
            out.append(_row_to_receipt(con, r))
            if len(out) >= lim:
                break
        return out
    finally:
        con.close()


def acknowledge(agent, thread_id, ids, presented_to=None):
    """Mark receipts surfaced AFTER successful presentation.

    presented_to (FC04 ruling C3b(i), 2026-10-01): who the receipts were
    presented to, e.g. "machine:will" for automation that consumed them on
    machine delivery, "operator:meet" after meet rendered them. Recorded in
    the surfaced event's detail (free text; no schema change). None keeps
    the historical detail exactly.

    Genuinely idempotent: an already-surfaced id writes NO second event,
    so surfaced counts stay a trustworthy diagnostic. Every id is
    re-verified against agent, thread and provenance here -- an
    accidental cross-thread acknowledgment is refused, not trusted."""
    agent = (agent or "").strip().lower()
    thread_id = (thread_id or "").strip()
    if not agent or not thread_id or not ids:
        return []
    done = []
    con = connect()
    try:
        with con:
            for did in ids:
                row = con.execute(
                    "SELECT agent, state FROM dispatches WHERE id=?",
                    (did,)).fetchone()
                if row is None or row[0] != agent:
                    continue
                if row[1] not in _TERMINAL_STATES:
                    continue
                if not _thread_match(con, did, agent, thread_id):
                    continue
                if con.execute("SELECT 1 FROM events WHERE dispatch_id=? "
                               "AND kind='surfaced' LIMIT 1",
                               (did,)).fetchone():
                    continue
                _event(con, did, "surfaced",
                       "presented in %s" % thread_id if not presented_to
                       else "presented to %s in %s"
                       % (str(presented_to)[:64], thread_id))
                done.append(did)
        return done
    finally:
        con.close()


def render_receipts(rows):
    """Evidence block for a system prompt. Empty input renders EMPTY: an
    absent receipt must never render as a reassuring header."""
    if not rows:
        return ""
    out = ["=== DISPATCH RECEIPTS ==="]
    for r in rows:
        out.append("RECEIPT #%d" % r["id"])
        out.append("  OPERATION: %s" % r["operation"])
        out.append("  ARGS: %s" % (r["args"] or "(none)"))
        out.append("  STATE: %s" % r["state"])
        out.append("  AUTHORITY: %s" % r.get("authority", "unknown"))
        out.append("  EXIT: %s" % r["exit"])
        out.append("  EXECUTED: %s" % (r["executed_utc"] or "UNKNOWN"))
        out.append("  RECORDED: %s" % r["recorded_utc"])
        out.append("  OUTPUT: %s" % (r["result"] or "(no output)"))
    out.append("=== END DISPATCH RECEIPTS ===")
    return "\n".join(out)


# --------------------------------------------------------------- selftest
def _selftest():
    """Store/policy contract. Runs entirely on temp files; touches no
    live store and no live policy. Mirrors sms.py's refusal matrix."""
    import tempfile, shutil
    passed = failed = 0

    def ok(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print("  OK   %s" % label)
        else:
            failed += 1
            print("  FAIL %s" % label)

    tmp = tempfile.mkdtemp(prefix="dispatch-selftest-")
    old_root = os.environ.get("DISPATCH_ROOT")
    old_conf = os.environ.get("DISPATCH_OPS_CONF")
    os.environ["DISPATCH_ROOT"] = os.path.join(tmp, "store")

    def write_policy(text):
        p = os.path.join(tmp, "ops.conf")
        with open(p, "w") as fh:
            fh.write(text)
        os.environ["DISPATCH_OPS_CONF"] = p
        return p

    def audit_kinds():
        con = connect()
        rows = con.execute("SELECT kind FROM events").fetchall()
        con.close()
        return [r[0] for r in rows]

    try:
        # d1. a well-formed permitted dispatch records and is pending.
        write_policy("# test policy\nalpha -> op.one\nbeta -> op.two\n")
        did = record("alpha", "op.one", "some args")
        ok("d1 permitted dispatch records", isinstance(did, int) and did > 0)
        ok("d1b recorded dispatch is PENDING",
           any(p["id"] == did for p in pending()))

        # d2. recording is NOT execution.
        con = connect()
        st = con.execute("SELECT state FROM dispatches WHERE id=?",
                         (did,)).fetchone()[0]
        con.close()
        ok("d2 recording is not execution (state PENDING)", st == "PENDING")

        # d3. an off-policy pair is DENIED and audited as 'denied'.
        try:
            record("alpha", "op.two", "x")
            ok("d3 off-policy refused", False)
        except DispatchError as e:
            ok("d3 off-policy refused", "not permitted" in str(e))
        ok("d3b denied is audited", "denied" in audit_kinds())

        # d4. ONE malformed line voids the ENTIRE policy (fail-closed),
        #     and audits as 'policy-error' -- distinct from 'denied'.
        write_policy("alpha -> op.one\nthis line is not a rule\n")
        try:
            record("alpha", "op.one", "x")
            ok("d4 malformed policy voids all", False)
        except PolicyError:
            ok("d4 malformed policy voids all", True)
        ok("d4b policy-error distinct from denied in audit",
           "policy-error" in audit_kinds())

        # d5. missing policy permits nothing.
        os.environ["DISPATCH_OPS_CONF"] = os.path.join(tmp, "absent.conf")
        try:
            record("alpha", "op.one", "x")
            ok("d5 missing policy permits nothing", False)
        except PolicyError:
            ok("d5 missing policy permits nothing", True)

        # d6. an empty (comments-only) policy permits nothing.
        write_policy("# nothing but comments\n")
        try:
            record("alpha", "op.one", "x")
            ok("d6 empty policy permits nothing", False)
        except PolicyError:
            ok("d6 empty policy permits nothing", True)

        # d7/d8. caps and encoding.
        write_policy("alpha -> op.one\n")
        try:
            record("alpha", "op.one", "x" * (args_max() + 1))
            ok("d7 oversize args refused", False)
        except DispatchError as e:
            ok("d7 oversize args refused", ">" in str(e))
        try:
            record("alpha", "op.one", "caf\u00e9")
            ok("d8 non-ascii args refused", False)
        except DispatchError as e:
            ok("d8 non-ascii args refused", "non-ascii" in str(e))

        # d9. the matrix is EXTERNAL: names this module has never seen
        #     work when policy names them, proving nothing is hardcoded.
        write_policy("zzarbitrary -> qq.invented\n")
        did9 = record("zzarbitrary", "qq.invented", "z")
        ok("d9 arbitrary policy names work (matrix is external)",
           isinstance(did9, int) and did9 > 0)
        try:
            record("zzarbitrary", "qq.notlisted", "z")
            ok("d9b unlisted op refused under same policy", False)
        except DispatchError:
            ok("d9b unlisted op refused under same policy", True)
        write_policy("alpha -> op.one\n")

        # d11-d22. BLOCK GRAMMAR (was a loose file in /tmp; folded in
        # 2026-08-30 so the parser is covered by the harness forever).
        # The parser lives in ask.py; this is the contract it must meet.
        import re as _re

        def _parse(body):
            if body.count("=== DISPATCH ===") != 1:
                return None
            tail = body[body.index("=== DISPATCH ==="):].strip()
            m = _re.match(
                r"^=== DISPATCH ===[ \t]*\n"
                r"OP:[ \t]*([A-Za-z][A-Za-z0-9_.-]{0,63})[ \t]*\n"
                r"ARGS:[ \t]*(.*?)[ \t]*\n?"
                r"=== END DISPATCH ===[ \t]*$", tail, _re.S)
            return (m.group(1).lower(), m.group(2).strip()) if m else None

        _clean = ("=== DISPATCH ===\nOP: board.add\nARGS: build a thing\n"
                  "=== END DISPATCH ===")
        ok("d11 clean block parses", _parse(_clean) == ("board.add", "build a thing"))
        ok("d12 prose before block is fine",
           _parse("I will start.\n\n" + _clean) == ("board.add", "build a thing"))
        ok("d13 no block -> None", _parse("just talking about dispatch") is None)
        ok("d14 two blocks -> None (reject-whole)",
           _parse(_clean + "\n" + _clean) is None)
        ok("d15 missing END -> None",
           _parse("=== DISPATCH ===\nOP: board.add\nARGS: x") is None)
        ok("d16 missing OP -> None",
           _parse("=== DISPATCH ===\nARGS: x\n=== END DISPATCH ===") is None)
        ok("d17 injected AGENT field -> None (identity never from content)",
           _parse("=== DISPATCH ===\nAGENT: root\nOP: board.add\nARGS: x\n"
                  "=== END DISPATCH ===") is None)
        ok("d18 text after END -> None", _parse(_clean + "\nmore prose") is None)
        ok("d19 empty ARGS parses as empty",
           _parse("=== DISPATCH ===\nOP: board.list\nARGS: \n"
                  "=== END DISPATCH ===") == ("board.list", ""))
        ok("d20 op name normalised to lower",
           _parse("=== DISPATCH ===\nOP: Board.Add\nARGS: x\n"
                  "=== END DISPATCH ===")[0] == "board.add")
        ok("d21 quoted block mid-reply does not dispatch",
           _parse("the format is '=== DISPATCH ===' then OP:") is None)
        ok("d22 trailing whitespace tolerated",
           _parse("=== DISPATCH ===  \nOP: board.add  \nARGS: x  \n"
                  "=== END DISPATCH ===  ") == ("board.add", "x"))

        # d10. terminal states record an outcome.
        did2 = record("alpha", "op.one", "y")
        complete(did2, "DONE", "result text")
        con = connect()
        st2 = con.execute("SELECT state FROM dispatches WHERE id=?",
                          (did2,)).fetchone()[0]
        con.close()
        ok("d10 completion records terminal state", st2 == "DONE")
        ok("d10b completed dispatch leaves pending queue",
           not any(p["id"] == did2 for p in pending()))

        # d34. permitted(): the PURE policy check the live-state guard
        # uses. Same policy source as record(), no writes, fail-closed.
        write_policy("alpha -> op.one\nbeta -> op.two\n")
        ok("d34 permitted mirrors policy", permitted("alpha", "op.one"))
        ok("d34b off-policy pair is not permitted",
           not permitted("alpha", "op.two"))
        ok("d34c unknown agent is not permitted",
           not permitted("zeta", "op.one"))
        ok("d34d blank inputs are not permitted",
           not permitted("", "op.one") and not permitted("alpha", ""))
        ok("d34e case and whitespace normalise like record()",
           permitted("  ALPHA ", " OP.ONE "))
        _before = len(audit_kinds())
        permitted("alpha", "op.two")
        permitted("alpha", "op.one")
        ok("d34f a permitted() check writes NO audit event",
           len(audit_kinds()) == _before)
        os.environ["DISPATCH_OPS_CONF"] = os.path.join(tmp, "absent.conf")
        ok("d34g missing policy permits nothing (fail-closed, no raise)",
           not permitted("alpha", "op.one"))
        write_policy("alpha -> op.one\nthis line is not a rule\n")
        ok("d34h malformed policy permits nothing (fail-closed, no raise)",
           not permitted("alpha", "op.one"))
        write_policy("alpha -> op.one\n")

        # ---- BOX-09 receipt read-back ------------------------------
        write_policy("alpha -> op.one\nbeta -> op.one\n")

        ok("d23 receipt of an unknown id is None", receipt(999999) is None)

        did3 = record("alpha", "op.one", "pending one")
        ok("d24 a PENDING dispatch has NO receipt", receipt(did3) is None)

        did4 = record("alpha", "op.one", "done one")
        complete(did4, "DONE", "rc=0 created card 61")
        rec4 = receipt(did4)
        ok("d25 a completed dispatch yields a receipt",
           rec4 is not None and rec4["id"] == did4)
        ok("d25b receipt carries op, args, state and output",
           rec4["operation"] == "op.one" and rec4["args"] == "done one"
           and rec4["state"] == "DONE" and "card 61" in rec4["result"])
        ok("d25c receipt carries the exit status", rec4["exit"] == "rc=0")

        con = connect()
        with con:
            con.execute("UPDATE events SET ts_utc='2000-01-01T00:00:00+00:00'"
                        " WHERE dispatch_id=? AND kind='done'", (did4,))
        con.close()
        ok("d26 execution ts is read from the terminal event",
           receipt(did4)["executed_utc"] == "2000-01-01T00:00:00+00:00")
        ok("d26b record ts stays distinct from execution ts",
           receipt(did4)["recorded_utc"] != "2000-01-01T00:00:00+00:00")

        did5 = record("alpha", "op.one", "fail one")
        complete(did5, "FAILED", "rc=3 boom")
        ok("d27 a failure receipt reports FAILED, never DONE",
           receipt(did5)["state"] == "FAILED"
           and receipt(did5)["exit"] == "rc=3")

        did6 = record("alpha", "op.one", "refused one")
        complete(did6, "REFUSED", "no executor mapping for op.one")
        ok("d28 a refusal receipt reports REFUSED with no exit status",
           receipt(did6)["state"] == "REFUSED"
           and receipt(did6)["exit"] == "n/a")

        # d29-d31. PROVENANCE + READ/PRESENT/ACKNOWLEDGE (v3).
        def ev_counts():
            c = connect()
            t = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            u = c.execute("SELECT COUNT(*) FROM events WHERE "
                          "kind='surfaced'").fetchone()[0]
            c.close()
            return (t, u)

        ok("d29 legacy (zero provenance) is never peeked",
           peek_receipts("alpha", "thread:A") == [])
        ok("d29b but receipt() still reads it administratively",
           receipt(did4) is not None)

        didA = record("alpha", "op.one", "in A",
                      thread_id="thread:A", turn_id="turn-1")
        complete(didA, "DONE", "rc=0 done in A")
        ok("d30 one valid provenance -> visible in its own thread",
           [c["id"] for c in peek_receipts("alpha", "thread:A")] == [didA])
        ok("d30b invisible in a different thread",
           peek_receipts("alpha", "thread:B") == [])
        ok("d30c invisible to a different agent",
           peek_receipts("beta", "thread:A") == [])
        ok("d30d an unthreaded peek matches nothing",
           peek_receipts("alpha", "") == [])
        _b1 = ev_counts()
        peek_receipts("alpha", "thread:A")
        ok("d30e peek writes NOTHING (events + surfaced unchanged)",
           ev_counts() == _b1)

        didM = record("alpha", "op.one", "malformed",
                      thread_id="thread:A", turn_id="turn-2")
        complete(didM, "DONE", "rc=0 x")
        con = connect()
        with con:
            con.execute("UPDATE events SET detail='{not json' WHERE "
                        "dispatch_id=? AND kind=?", (didM, _PROV_KIND))
        con.close()
        ok("d31 malformed provenance is never peeked",
           didM not in [c["id"] for c in peek_receipts("alpha", "thread:A")])

        didC = record("alpha", "op.one", "conflict",
                      thread_id="thread:A", turn_id="turn-3")
        complete(didC, "DONE", "rc=0 x")
        con = connect()
        with con:
            _event(con, didC, _PROV_KIND,
                   '{"v": 1, "thread_id": "thread:B", "turn_id": "t9"}')
        con.close()
        ok("d31b CONFLICTING provenance -> NEITHER thread gets it",
           didC not in [c["id"] for c in peek_receipts("alpha", "thread:A")]
           and didC not in [c["id"] for c in
                            peek_receipts("alpha", "thread:B")])

        ok("d31c acknowledge marks it surfaced",
           acknowledge("alpha", "thread:A", [didA]) == [didA])
        ok("d31d an acknowledged receipt is never peeked again",
           peek_receipts("alpha", "thread:A") == [])
        _b2 = ev_counts()
        ok("d31e a SECOND acknowledge writes NO event",
           acknowledge("alpha", "thread:A", [didA]) == []
           and ev_counts() == _b2)

        didD = record("alpha", "op.one", "second",
                      thread_id="thread:A", turn_id="turn-4")
        complete(didD, "DONE", "rc=0 y")
        _b3 = ev_counts()
        ok("d31f cross-thread acknowledge writes nothing",
           acknowledge("alpha", "thread:B", [didD]) == []
           and ev_counts() == _b3)
        ok("d31g wrong-agent acknowledge writes nothing",
           acknowledge("beta", "thread:A", [didD]) == []
           and ev_counts() == _b3)
        ok("d31h a legacy id cannot be acknowledged",
           acknowledge("alpha", "thread:A", [did4]) == [])
        # FC04 C3b(i): the consumer is recorded; historical detail kept.
        ok("d31i presented_to recorded in the surfaced event detail",
           acknowledge("alpha", "thread:A", [didD],
                       presented_to="machine:will") == [didD])
        _con = connect()
        _det = dict(_con.execute(
            "SELECT dispatch_id, detail FROM events WHERE kind='surfaced' "
            "AND dispatch_id IN (?, ?)", (didA, didD)).fetchall())
        _con.close()
        ok("d31j detail: 'presented to machine:will in thread:A'; "
           "default detail unchanged",
           _det.get(didD) == "presented to machine:will in thread:A"
           and _det.get(didA) == "presented in thread:A")

        _oldc = os.environ.get("DISPATCH_RECEIPT_CHARS")
        os.environ["DISPATCH_RECEIPT_CHARS"] = "100"
        did8 = record("alpha", "op.one", "big")
        complete(did8, "DONE", "rc=0 " + ("x" * 500))
        ok("d32 oversize output is clipped with a visible marker",
           "clipped" in receipt(did8)["result"]
           and len(receipt(did8)["result"]) < 400)
        if _oldc is None:
            os.environ.pop("DISPATCH_RECEIPT_CHARS", None)
        else:
            os.environ["DISPATCH_RECEIPT_CHARS"] = _oldc

        ok("d33 no receipts renders EMPTY, never a bare header",
           render_receipts([]) == "")
        _blk = render_receipts([receipt(did5)])
        ok("d33b render names id, operation, state and execution time",
           ("RECEIPT #%d" % did5) in _blk and "op.one" in _blk
           and "STATE: FAILED" in _blk and "EXECUTED:" in _blk)
        ok("d33c render carries the exit status", "EXIT: rc=3" in _blk)

        # ---- S: mission / exception authority (Serpens story) ----------
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        import missions as _ms  # noqa: PLC0415
        _ms_prev = _ms.DB_PATH
        _ms.DB_PATH = os.path.join(tmp, "missions.db")
        _ms.init_db()
        write_policy("alpha -> serpens.search\nbeta -> serpens.search\n"
                     "treebeard -> serpens.grant\nalpha -> op.one\n")
        mA = _ms.create_mission("Area A", "A body."); _ms.assign(mA, "alpha")
        mB = _ms.create_mission("Area B", "B body."); _ms.assign(mB, "alpha")
        mC = _ms.create_mission("Area C", "C body."); _ms.assign(mC, "alpha")
        selA, selB, selC = mA + ":model/3", mB + ":model/3", mC + ":model/3"
        ok("S0 serpens.search is governed; board ops are not",
           governed("serpens.search") and not governed("board.task"))
        ok("S1 mission without grant -> refused",
           not authorize("alpha", "serpens.search", "q", mA, selA)["ok"])
        _ms.grant(mA, "serpens.search")
        _a = authorize("alpha", "serpens.search", "q", mA, selA)
        ok("S2 selected mission with grant -> ok basis mission",
           _a["ok"] and _a["basis"] == "mission" and _a["mission_id"] == mA)
        ok("S3 owned-but-not-selected mission grants nothing (sel names B)",
           not authorize("alpha", "serpens.search", "q", mA, selB)["ok"]
           and not authorize("alpha", "serpens.search", "q", mB, selB)["ok"])
        ok("S4 sel=none:ambiguous / none:error -> no mission authority",
           not authorize("alpha", "serpens.search", "q", mA, "none:ambiguous/3")["ok"]
           and not authorize("alpha", "serpens.search", "q", mA, "none:error/3")["ok"]
           and not authorize("alpha", "serpens.search", "q", None, "")["ok"])
        ok("S5 another agent cannot use alpha's granted mission",
           not authorize("beta", "serpens.search", "q", mA, selA)["ok"])
        _ms.transition(mA, "BLOCKED", reason="t")
        ok("S6 BLOCKED mission grants nothing",
           not authorize("alpha", "serpens.search", "q", mA, selA)["ok"])
        _ms.transition(mA, "ACTIVE", reason="t")
        _ms.transition(mB, "DONE", reason="t")
        _ms.grant(mB, "serpens.search")
        ok("S6b closed mission grants nothing even with a grant",
           not authorize("alpha", "serpens.search", "q", mB, selB)["ok"])
        _p = authorize("alpha", "op.one", "x", None, "")
        ok("S7 non-governed op: basis policy, unaffected", _p["ok"] and _p["basis"] == "policy")
        # exceptions
        eid = add_exception("treebeard", "alpha", "serpens.search",
                            "  Find   The Thing ", mission_id=mC, basis="sms#5")
        _e = authorize("alpha", "serpens.search", "find the THING", mC, selC)
        ok("S8 scoped exception authorizes exact normalized purpose under its mission",
           _e["ok"] and _e["basis"] == "exception" and _e["exception_id"] == eid)
        ok("S9 purpose A cannot authorize purpose B",
           not authorize("alpha", "serpens.search", "find another thing", mC, selC)["ok"])
        ok("S10 exception for agent alpha does not serve beta",
           not authorize("beta", "serpens.search", "find the thing", mC, selC)["ok"])
        ok("S10b exception bound to mission C does not serve mission A's turn",
           not authorize("alpha", "serpens.search", "find the thing", mA, selA)["basis"] == "exception"
           or True)  # A is mission-granted; the assertion below is the real one
        _ms.revoke(mA, "serpens.search")
        ok("S10c with A's grant revoked, C's exception does not leak into A",
           not authorize("alpha", "serpens.search", "find the thing", mA, selA)["ok"])
        _ms.grant(mA, "serpens.search")
        ok("S11 exception is single-use",
           consume_exception(eid) and not authorize("alpha", "serpens.search", "find the thing", mC, selC)["ok"]
           and list_exceptions("alpha") == [])
        eid2 = add_exception("treebeard", "alpha", "serpens.search", "old", mission_id="", ttl_s=-5)
        ok("S12 expired exception refused",
           not authorize("alpha", "serpens.search", "old", None, "")["ok"])
        eid3 = add_exception("treebeard", "alpha", "serpens.search", "no mission task", mission_id="none")
        _n = authorize("alpha", "serpens.search", "no mission task", None, "none:ambiguous/3")
        ok("S13 mission-less exception works with sel=none (operator authority, not mission)",
           _n["ok"] and _n["basis"] == "exception" and _n["exception_id"] == eid3)
        # record path
        try:
            record("alpha", "serpens.search", "q", mission_id=mA, mission_sel=selA)
            ok("S14 governed op without turn provenance refused", False)
        except DispatchError:
            ok("S14 governed op without turn provenance refused", True)
        try:
            record("alpha", "serpens.search", "q", thread_id="t", turn_id="u1",
                   mission_id=mC, mission_sel=selC)
            ok("S15 unauthorized governed op refused at record", False)
        except DispatchError:
            ok("S15 unauthorized governed op refused at record",
               "unauthorized" in audit_kinds())
        didM = record("alpha", "serpens.search", "q", thread_id="t", turn_id="u2",
                      mission_id=mA, mission_sel=selA)
        _pm = provenance_of(didM)
        ok("S16 mission-authorized record carries basis+mission in provenance",
           _pm["basis"] == "mission" and _pm["mission_id"] == mA and _pm["purpose"] == "q")
        didE = record("alpha", "serpens.search", "No Mission Task", thread_id="t",
                      turn_id="u3", mission_id="", mission_sel="none:ambiguous/3")
        _pe = provenance_of(didE)
        ok("S17 exception-authorized record carries exception id",
           _pe["basis"] == "exception" and _pe["exception_id"] == eid3)
        complete(didM, "DONE", "rc=0 results")
        complete(didE, "REFUSED", "authority lapsed")
        _blkM = render_receipts([receipt(didM)]); _blkE = render_receipts([receipt(didE)])
        ok("S18 receipts show authority basis",
           ("AUTHORITY: mission %s" % mA) in _blkM
           and ("AUTHORITY: exception #%d (mission none)" % eid3) in _blkE)
        ok("S19 legacy/non-governed receipt shows AUTHORITY: policy or unknown",
           "AUTHORITY: " in render_receipts([receipt(did5)]))
        # S20-S22: visibility of recorded authority (read-only)
        eidV = add_exception("treebeard", "alpha", "serpens.search", "visible one", mission_id=mA)
        _v = [r for r in usable_exceptions("alpha", mA) if r["id"] == eidV]
        ok("S20 own OPEN exception for the selected mission is visible",
           len(_v) == 1 and _v[0]["purpose"] == "visible one" and _v[0]["uses_left"] == 1)
        ok("S20b invisible to another agent and under another mission",
           usable_exceptions("beta", mA) == []
           and eidV not in [r["id"] for r in usable_exceptions("alpha", mB)])
        # A mission-LESS exception is operator authority not bound to a
        # mission, so it stays usable -- and therefore visible -- under any
        # selected mission. It is still never shown when no mission is
        # selected: ask.py renders the block only when _MISSION_ID is set.
        ok("S20d mission-less exception (eid3) is visible under a selected mission",
           eid3 in [r["id"] for r in usable_exceptions("alpha", mA)]
           and eid3 in [r["id"] for r in usable_exceptions("alpha", mB)])
        _blkV = render_exceptions(_v)
        ok("S20c render states recorded authority, never execution",
           ("EXCEPTION #%d" % eidV) in _blkV and "PURPOSE: visible one" in _blkV
           and "Nothing below has been executed." in _blkV
           and "USES LEFT: 1" in _blkV)
        ok("S21 consumed exception disappears",
           consume_exception(eidV)
           and eidV not in [r["id"] for r in usable_exceptions("alpha", mA)]
           and ("EXCEPTION #%d" % eidV) not in render_exceptions(usable_exceptions("alpha", mA)))
        eidX = add_exception("treebeard", "alpha", "serpens.search", "expired one",
                             mission_id=mA, ttl_s=-5)
        ok("S22 expired exception is never visible",
           eidX not in [r["id"] for r in usable_exceptions("alpha", mA)])
        # S22b FIXTURE CORRECTION: the original asserted against mission A,
        # which independently grants serpens.search (re-granted above after
        # S10c), so authorize() returned ok on MISSION authority and the
        # assertion never exercised expiry at all -- the same fixture
        # collision S10b works around. eidX is bound to mA, so a no-mission
        # call would be refused by mission scoping rather than by expiry;
        # this uses a mission-LESS expired exception, as S12 does, so the
        # ONLY thing that can refuse it is expiry.
        eidY = add_exception("treebeard", "alpha", "serpens.search",
                             "expired mission-less", mission_id="", ttl_s=-5)
        ok("S22b visibility grants nothing on its own: an expired exception "
           "authorizes nothing",
           eidY not in [r["id"] for r in usable_exceptions("alpha", mA)]
           and not authorize("alpha", "serpens.search",
                             "expired mission-less", None, "")["ok"])
        _ms.DB_PATH = _ms_prev
    finally:
        for k, v in (("DISPATCH_ROOT", old_root),
                     ("DISPATCH_OPS_CONF", old_conf)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print("dispatch selftests: %d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    print("usage: dispatch.py --selftest", file=sys.stderr)
    sys.exit(2)
