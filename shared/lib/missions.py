#!/usr/bin/env python3
"""
missions.py -- Treebeard durable mission store (F25b, operator half).

Target path:  /opt/treebeard/shared/lib/missions.py   (root:treebeard 640)
DB path:      /opt/treebeard/shared/missions/missions.db
Dir perms:    root:treebeard 2770  (setgid, same pattern as records/)
DB perms:     660 root:treebeard   (ENFORCED by init_db when run as root)

Purpose
-------
Separates MISSION (what an agent is currently working on) from CHARTER (what
an agent IS). Charter stays where it always was: /etc/treebeard/<agent>.charter.txt,
root-owned, operator-authored. This store holds the mission RECORD: durable id,
status, ownership, timestamps, Definition of Done, and full history.

Authority
---------
The operator assigns and reassigns. Nothing here grants an agent the ability to
assign, reassign, or self-transition a mission -- there is no agent-facing write
path in this module, and callers pass `actor` explicitly for the audit trail.

Mission TEXT remains operator-authored in /etc/treebeard (root-owned 644), so an
agent cannot rewrite its own tasking. Han's charter grants capability "except
where his mission file explicitly grants it" -- mission text can widen
authorization, so its write path stays root-only by design. This store keeps a
SNAPSHOT of the text at assignment time, which is what makes the archive survive
the file being deleted.

Design contract
---------------
- Writes are FAIL-LOUD. Unlike record_log.log_event (which returns False so a
  failed log can never kill the action it records), a failed mission write MUST
  raise: a silently failed assignment would leave the operator believing an agent
  is tasked when it is not.
- MULTIPLE CONCURRENT MISSIONS per agent (2026-09-03). Mission count is data,
  not architecture: open_missions() returns every mission an agent owns in a
  deterministic order, active_missions()/routing_index() return the ones a
  per-turn selector may choose (ACTIVE only), and each mission carries a short
  stored routing SUMMARY so a selector never has to read full charters. The
  old one-open-per-agent unique index is dropped by init_db (additive,
  idempotent migration; rows are never touched). current() is kept only as an
  EXPLICIT single-mission read that raises when several are open.
- History is never overwritten. Closing a mission transitions its status and
  stamps closed_utc; the row and its event trail remain queryable forever.
- Generic: no agent-name branches. Any agent whose name matches AGENT_RE works
  the day the operator creates a mission for it.

NOT done here (deliberate, tracked, not silently skipped)
---------------------------------------------------------
- AGENT-AUTHORED STATE: an agent recording its own progress/evidence/BLOCKED
  requires the tool loop ask.py's one-shot subprocess shape does not provide.
  That is the Will half of F25b. The schema has the fields; nothing writes them
  from an agent yet.
- TAMPER-RESISTANCE: append-only is convention, same honest gate record_log
  flags. Any treebeard-group writer can in principle UPDATE/DELETE the state DB.

Self-test: `missions.py --selftest` -- offline, stdlib-only, temp dir, writes
nothing outside it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")
_TB_PROD_DB_PATH = "/opt/treebeard/shared/missions/missions.db"  # literal on purpose: production detection must not follow TREEBEARD_BASE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = f"{_TB_BASE}/shared/missions/missions.db"

# Same name shape ask.py and the memory layer accept. The agent name is a
# registry key here, not a path component, but keeping one alphabet across the
# system is the point.
AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

DRAFT = "DRAFT"
ACTIVE = "ACTIVE"
BLOCKED = "BLOCKED"
DONE = "DONE"
CANCELLED = "CANCELLED"
SUPERSEDED = "SUPERSEDED"

# Kept closed so the store cannot silently accumulate garbage states, same
# discipline as record_log.VALID_KINDS.
VALID_STATES = (DRAFT, ACTIVE, BLOCKED, DONE, CANCELLED, SUPERSEDED)

# An agent OWNS a mission in these states. BLOCKED is still owned (it stays
# in the roster and in open_missions) but it is NOT selectable: transitioning
# a mission to BLOCKED is how the operator deactivates it without closing it.
OPEN_STATES = (ACTIVE, BLOCKED)
SELECTABLE_STATES = (ACTIVE,)
# Routing summaries are compact metadata for a selector, not governance text.
SUMMARY_MAX = 400
# A+ ROUTER (2026-09-04): explicit routing vocabulary, separate budget from
# the human summary. Comma/newline-separated phrases; scored by the selector
# as tokens alongside title + summary. Data, never authority.
ROUTING_TERMS_MAX = 600
TERMINAL_STATES = (DONE, CANCELLED, SUPERSEDED)

# ASSIGNED is deliberately absent: there is no agent acceptance step, so
# assignment and activation are the same operator act. A state nothing can
# distinguish is dead weight.
ALLOWED = {
    DRAFT: (ACTIVE, CANCELLED),
    ACTIVE: (BLOCKED, DONE, CANCELLED, SUPERSEDED),
    BLOCKED: (ACTIVE, DONE, CANCELLED, SUPERSEDED),
    DONE: (),
    CANCELLED: (),
    SUPERSEDED: (),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id    TEXT PRIMARY KEY,   -- durable, opaque, never reused
    title         TEXT NOT NULL,      -- short human handle
    agent         TEXT,               -- NULL while unassigned DRAFT
    status        TEXT NOT NULL,      -- see VALID_STATES
    objective     TEXT NOT NULL,      -- mission text snapshot
    dod           TEXT,               -- Definition of Done / success condition
    source_path   TEXT,               -- operator-authored file it came from
    created_utc   TEXT NOT NULL,
    activated_utc TEXT,
    closed_utc    TEXT,
    notes         TEXT,               -- optional operator notes
    evidence_ref  TEXT,               -- optional pointer: record id, board id, path
    summary       TEXT,               -- compact routing summary (<= SUMMARY_MAX chars)
    grants        TEXT                -- JSON list of operation names this mission AUTHORIZES
);

-- Active-mission lookup path: (agent, status). Several open missions per
-- agent are permitted by design; there is deliberately NO uniqueness here.
CREATE INDEX IF NOT EXISTS idx_missions_agent_status ON missions (agent, status);

CREATE INDEX IF NOT EXISTS idx_missions_agent  ON missions (agent);
CREATE INDEX IF NOT EXISTS idx_missions_status ON missions (status);

CREATE TABLE IF NOT EXISTS mission_events (
    id          INTEGER PRIMARY KEY,
    mission_id  TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,      -- who performed it (operator handle)
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events (mission_id);
"""


class MissionError(Exception):
    """Any refused or failed mission write. Raised, never swallowed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id():
    return "m-%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                        uuid.uuid4().hex[:8])


def _check_agent(agent):
    if not isinstance(agent, str) or not AGENT_RE.match(agent):
        raise MissionError("invalid agent name %r" % (agent,))


def _connect():
    d = os.path.dirname(DB_PATH)
    if not os.path.isdir(d):
        raise MissionError("mission store directory missing: %s "
                           "(run init_db as root)" % d)
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
    except sqlite3.Error as e:
        raise MissionError("cannot open mission store %s: %s" % (DB_PATH, e))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _migrate(con):
    """Additive, idempotent migration for stores created before
    multi-mission (2026-09-03): drop the one-open-per-agent unique index and
    add the summary column. Rows are never rewritten, deleted, or moved."""
    con.execute("DROP INDEX IF EXISTS idx_one_open_per_agent")
    cols = [r[1] for r in con.execute("PRAGMA table_info(missions)")]
    if "summary" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN summary TEXT")
    if "grants" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN grants TEXT")
    if "routing_terms" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN routing_terms TEXT")


def _check_summary(summary, required=False):
    """One line, whitespace-collapsed, <= SUMMARY_MAX chars, or None."""
    if summary is None:
        if required:
            raise MissionError("summary is required")
        return None
    s = " ".join(str(summary).split())
    if not s:
        if required:
            raise MissionError("summary is required")
        return None
    if len(s) > SUMMARY_MAX:
        raise MissionError("summary %d chars > SUMMARY_MAX %d" % (len(s), SUMMARY_MAX))
    return s


def init_db():
    """Create schema and ENFORCE production ownership/perms. Idempotent.

    Refuses to run as non-root against the production path -- perms are a
    load-bearing control, not a docstring promise (record_log's rule).
    """
    is_prod = DB_PATH == _TB_PROD_DB_PATH
    if is_prod and os.geteuid() != 0:
        raise MissionError("init_db must run as root against the production path")

    d = os.path.dirname(DB_PATH)
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()

    if is_prod:
        import grp
        gid = grp.getgrnam("treebeard").gr_gid
        os.chown(d, 0, gid)
        os.chmod(d, 0o2770)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                os.chown(p, 0, gid)
                os.chmod(p, 0o660)
    return DB_PATH


# ---------------------------------------------------------------------------
# Write API -- operator only, fail-loud
# ---------------------------------------------------------------------------
def create_mission(title, objective, dod=None, agent=None, source_path=None,
                   notes=None, actor="operator", summary=None):
    """Create a DRAFT mission. Does not activate it. Returns mission_id.
    `summary` is the compact routing summary (optional here, settable later
    with set_summary); it is stored, never derived per turn."""
    summary = _check_summary(summary)
    if not title or not str(title).strip():
        raise MissionError("title is required")
    if not objective or not str(objective).strip():
        raise MissionError("objective (mission text) is required")
    if agent is not None:
        _check_agent(agent)

    mid = _new_id()
    ts = _now()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO missions (mission_id, title, agent, status, "
                "objective, dod, source_path, created_utc, notes, summary) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, title, agent, DRAFT, objective, dod, source_path, ts,
                 notes, summary))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mid, ts, None, DRAFT, actor, "created"))
    except sqlite3.Error as e:
        raise MissionError("create failed: %s" % e)
    finally:
        con.close()
    return mid


def assign(mission_id, agent, actor="operator", reason=None):
    """Assign a DRAFT mission to an agent and activate it.

    An agent may hold several open missions at once. Assignment ADDS a
    mission; it never replaces one (supersede/close stay explicit operator
    acts). Refused only for a non-DRAFT mission or when the agent already
    holds an open mission with the same title -- that would be an accidental
    duplicate, not a concurrent mission.
    """
    _check_agent(agent)
    row = get(mission_id)
    if row is None:
        raise MissionError("no such mission: %s" % mission_id)
    if row["status"] != DRAFT:
        raise MissionError("mission %s is %s, only DRAFT can be assigned"
                           % (mission_id, row["status"]))
    for o in open_missions(agent):
        if o["title"].strip().lower() == row["title"].strip().lower():
            raise MissionError(
                "REFUSED: %s already holds open mission %s titled %r; a "
                "second copy would be a duplicate, not a concurrent mission"
                % (agent, o["mission_id"], o["title"]))

    ts = _now()
    con = _connect()
    try:
        with con:
            con.execute(
                "UPDATE missions SET agent=?, status=?, activated_utc=? "
                "WHERE mission_id=?", (agent, ACTIVE, ts, mission_id))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mission_id, ts, DRAFT, ACTIVE, actor, reason or "assigned"))
    except sqlite3.Error as e:
        raise MissionError("assign failed: %s" % e)
    finally:
        con.close()
    return mission_id


def transition(mission_id, to_status, actor="operator", reason=None):
    """Move a mission to a new status. Terminal states stamp closed_utc."""
    if to_status not in VALID_STATES:
        raise MissionError("invalid status %r (valid: %s)"
                           % (to_status, ", ".join(VALID_STATES)))
    row = get(mission_id)
    if row is None:
        raise MissionError("no such mission: %s" % mission_id)
    frm = row["status"]
    if to_status not in ALLOWED[frm]:
        raise MissionError("illegal transition %s -> %s for %s"
                           % (frm, to_status, mission_id))
    if to_status == ACTIVE and row["agent"] is None:
        raise MissionError("cannot activate unassigned mission %s" % mission_id)

    ts = _now()
    closed = ts if to_status in TERMINAL_STATES else None
    con = _connect()
    try:
        with con:
            if closed:
                con.execute("UPDATE missions SET status=?, closed_utc=? "
                            "WHERE mission_id=?", (to_status, closed, mission_id))
            else:
                con.execute("UPDATE missions SET status=? WHERE mission_id=?",
                            (to_status, mission_id))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mission_id, ts, frm, to_status, actor, reason))
    except sqlite3.Error as e:
        raise MissionError("transition failed: %s" % e)
    finally:
        con.close()
    return to_status


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------
def get(mission_id):
    con = _connect()
    try:
        cur = con.execute("SELECT * FROM missions WHERE mission_id=?",
                          (mission_id,))
        return cur.fetchone()
    finally:
        con.close()


def open_missions(agent):
    """Every mission this agent OWNS (ACTIVE or BLOCKED), in deterministic
    order: activation time, then creation order (rowid -- mission ids carry a
    random suffix and are NOT ordered). Ownership, not selection
    eligibility -- see active_missions()."""
    _check_agent(agent)
    con = _connect()
    try:
        return con.execute(
            "SELECT * FROM missions WHERE agent=? AND status IN (?,?) "
            "ORDER BY activated_utc, rowid",
            (agent, ACTIVE, BLOCKED)).fetchall()
    finally:
        con.close()


def active_missions(agent):
    """The agent's SELECTABLE missions (ACTIVE only), same order. BLOCKED
    missions are owned but do not participate in per-turn selection."""
    _check_agent(agent)
    con = _connect()
    try:
        return con.execute(
            "SELECT * FROM missions WHERE agent=? AND status=? "
            "ORDER BY activated_utc, rowid",
            (agent, ACTIVE)).fetchall()
    finally:
        con.close()


def routing_index(agent):
    """Compact routing metadata for the agent's selectable missions:
    mission_id, title, summary, status. NEVER the objective text -- this is
    what a selector reads so no full charter is loaded before one is chosen."""
    _check_agent(agent)
    con = _connect()
    try:
        # Additive-field tolerance (incident 2026-09-04): a pre-migration
        # store must degrade to '' with a visible diagnostic, never take the
        # store path down. Migration order is still the rule; this is the net.
        has_rt = "routing_terms" in [r[1] for r in con.execute("PRAGMA table_info(missions)")]
        if not has_rt:
            print("WARN: missions: column routing_terms absent in %s (pre-migration "
                  "store); routing_terms read as ''. Run: missions.py --init (root)"
                  % DB_PATH, file=sys.stderr)
        cols = "mission_id, title, summary, status" + (", routing_terms" if has_rt else "")
        rows = con.execute(
            "SELECT %s FROM missions WHERE agent=? AND status=? "
            "ORDER BY activated_utc, rowid" % cols, (agent, ACTIVE)).fetchall()
    finally:
        con.close()
    return [{"mission_id": r["mission_id"], "title": r["title"],
             "summary": r["summary"] or "",
             "routing_terms": (r["routing_terms"] if has_rt else "") or "",
             "status": r["status"]}
            for r in rows]


def current(agent):
    """EXPLICIT single-mission read kept for legacy callers: the one open
    mission when exactly one exists, None when none, and a MissionError when
    several are open. It never picks an arbitrary row."""
    rows = open_missions(agent)
    if not rows:
        return None
    if len(rows) > 1:
        raise MissionError(
            "AMBIGUOUS: %s holds %d open missions (%s); use open_missions()"
            % (agent, len(rows), ", ".join(r["mission_id"] for r in rows)))
    return rows[0]


def set_summary(mission_id, summary, actor="operator"):
    """Store or replace the compact routing summary. Charter text is
    untouched; the change is recorded in the event trail."""
    summary = _check_summary(summary, required=True)
    row = get(mission_id)
    if row is None:
        raise MissionError("no such mission: %s" % mission_id)
    ts = _now()
    con = _connect()
    try:
        with con:
            con.execute("UPDATE missions SET summary=? WHERE mission_id=?",
                        (summary, mission_id))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mission_id, ts, row["status"], row["status"], actor,
                 "summary set (%d chars)" % len(summary)))
    except sqlite3.Error as e:
        raise MissionError("set_summary failed: %s" % e)
    finally:
        con.close()


def _check_routing_terms(text):
    """Comma/newline-separated phrases -> normalised 'a, b, c' (each phrase
    whitespace-collapsed, empties dropped) or '' to clear. Capped."""
    if text is None:
        return ""
    parts = []
    for raw in str(text).replace("\n", ",").split(","):
        p = " ".join(raw.split())
        if p and p not in parts:
            parts.append(p)
    out = ", ".join(parts)
    if len(out) > ROUTING_TERMS_MAX:
        raise MissionError("routing_terms %d chars > ROUTING_TERMS_MAX %d"
                           % (len(out), ROUTING_TERMS_MAX))
    return out


def set_routing_terms(mission_id, text, actor="operator"):
    """Store or replace the routing vocabulary; '' clears it. Charter text is
    untouched; the change is recorded in the event trail. Returns the
    normalised value stored."""
    terms = _check_routing_terms(text)
    row = get(mission_id)
    if row is None:
        raise MissionError("no such mission: %s" % mission_id)
    ts = _now()
    con = _connect()
    try:
        with con:
            con.execute("UPDATE missions SET routing_terms=? WHERE mission_id=?",
                        (terms or None, mission_id))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mission_id, ts, row["status"], row["status"], actor,
                 "routing_terms set (%d chars)" % len(terms)))
    except sqlite3.Error as e:
        raise MissionError("set_routing_terms failed: %s" % e)
    finally:
        con.close()
    return terms
    return summary


def history(agent=None, include_open=True):
    """All missions, newest first. Filter by agent; the archive is never pruned."""
    if agent is not None:
        _check_agent(agent)
    q = "SELECT * FROM missions"
    args = []
    clauses = []
    if agent is not None:
        clauses.append("agent=?")
        args.append(agent)
    if not include_open:
        clauses.append("status NOT IN ('ACTIVE','BLOCKED')")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_utc DESC, mission_id DESC"
    con = _connect()
    try:
        return con.execute(q, args).fetchall()
    finally:
        con.close()


def events(mission_id):
    con = _connect()
    try:
        return con.execute(
            "SELECT * FROM mission_events WHERE mission_id=? ORDER BY id",
            (mission_id,)).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Grants -- the machine-readable mirror of what a mission's charter
# authorizes (2026-09-03, Serpens story). Operator-set data; evaluated by
# code (dispatch authority gate); never inferred from charter prose.
# ---------------------------------------------------------------------------
GRANT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _grants_of(row):
    try:
        v = json.loads(row["grants"] or "[]")
    except Exception:
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _check_op(op):
    op = (op or "").strip().lower()
    if not GRANT_RE.match(op):
        raise MissionError("invalid operation name %r" % (op,))
    return op


def grants_for(mission_id):
    """Operation names this mission authorizes, [] if none or unknown id."""
    row = get(mission_id)
    return _grants_of(row) if row is not None else []


def _set_grants(mission_id, ops, actor, reason):
    row = get(mission_id)
    if row is None:
        raise MissionError("no such mission: %s" % mission_id)
    ts = _now()
    con = _connect()
    try:
        with con:
            con.execute("UPDATE missions SET grants=? WHERE mission_id=?",
                        (json.dumps(sorted(set(ops))), mission_id))
            con.execute(
                "INSERT INTO mission_events (mission_id, ts_utc, from_status, "
                "to_status, actor, reason) VALUES (?,?,?,?,?,?)",
                (mission_id, ts, row["status"], row["status"], actor, reason))
    except sqlite3.Error as e:
        raise MissionError("grants update failed: %s" % e)
    finally:
        con.close()


def grant(mission_id, op, actor="operator"):
    """Authorize an operation under this mission. Idempotent; recorded."""
    op = _check_op(op)
    ops = grants_for(mission_id)
    if get(mission_id) is None:
        raise MissionError("no such mission: %s" % mission_id)
    if op not in ops:
        _set_grants(mission_id, ops + [op], actor, "grant %s" % op)
    return sorted(set(ops + [op]))


def revoke(mission_id, op, actor="operator"):
    """Withdraw an operation from this mission. Idempotent; recorded."""
    op = _check_op(op)
    ops = grants_for(mission_id)
    if get(mission_id) is None:
        raise MissionError("no such mission: %s" % mission_id)
    if op in ops:
        _set_grants(mission_id, [o for o in ops if o != op], actor, "revoke %s" % op)
    return sorted(o for o in ops if o != op)


def roster():
    """Every open mission for every agent (an agent may appear several
    times), ordered by agent, activation, id."""
    con = _connect()
    try:
        return con.execute(
            "SELECT agent, mission_id, title, status, activated_utc "
            "FROM missions WHERE status IN (?,?) "
            "ORDER BY agent, activated_utc, rowid",
            (ACTIVE, BLOCKED)).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile
    global DB_PATH
    fails = []
    n = [0]

    def check(label, cond):
        n[0] += 1
        print("%s %2d. %s" % ("ok  " if cond else "FAIL", n[0], label))
        if not cond:
            fails.append(label)

    def raises(label, fn):
        try:
            fn()
        except MissionError:
            check(label, True)
            return
        except Exception as e:
            check(label + " (wrong exception: %r)" % (e,), False)
            return
        check(label + " (no exception)", False)

    tmp = tempfile.mkdtemp(prefix="missions-selftest-")
    DB_PATH = os.path.join(tmp, "missions.db")
    try:
        init_db()
        check("init_db idempotent", init_db() == DB_PATH)

        # create
        m1 = create_mission("Job search pipeline", "Run the pipeline.",
                            dod="Ten scored leads.", source_path="/etc/x.txt")
        r = get(m1)
        check("create returns durable id", bool(m1) and m1.startswith("m-"))
        check("created mission is DRAFT", r["status"] == DRAFT)
        check("DRAFT has no agent", r["agent"] is None)
        check("dod stored", r["dod"] == "Ten scored leads.")
        check("objective snapshot stored", r["objective"] == "Run the pipeline.")
        check("created_utc stamped", bool(r["created_utc"]))
        raises("create without title raises",
               lambda: create_mission("", "text"))
        raises("create without objective raises",
               lambda: create_mission("t", "  "))
        raises("invalid agent name raises",
               lambda: create_mission("t", "o", agent="Clark!"))

        # assign
        assign(m1, "clark", actor="test-operator")
        r = get(m1)
        check("assign activates", r["status"] == ACTIVE)
        check("assign sets agent", r["agent"] == "clark")
        check("activated_utc stamped", bool(r["activated_utc"]))
        check("current() finds the single open mission",
              current("clark")["mission_id"] == m1)
        check("other agent has no mission", current("han") is None)
        check("open_missions lists it", [o["mission_id"] for o in open_missions("clark")] == [m1])

        # concurrent missions: assignment ADDS, never replaces
        m2 = create_mission("Second", "Other work.", summary="Second-area routing summary.")
        assign(m2, "clark")
        ids = [o["mission_id"] for o in open_missions("clark")]
        check("two ACTIVE missions held concurrently", ids == [m1, m2])
        check("first mission untouched by second assignment",
              get(m1)["status"] == ACTIVE and get(m1)["objective"] == "Run the pipeline.")
        check("second mission ACTIVE", get(m2)["status"] == ACTIVE)
        raises("current() refuses to pick among several",
               lambda: current("clark"))
        mdup = create_mission("second", "Same title, different case.")
        raises("duplicate-title assignment REFUSED", lambda: assign(mdup, "clark"))
        check("refused duplicate stays DRAFT", get(mdup)["status"] == DRAFT)

        # routing metadata: stored summaries, no charter text
        check("summary stored at create", get(m2)["summary"] == "Second-area routing summary.")
        check("summary absent until set", get(m1)["summary"] is None)
        set_summary(m1, "  Pipeline\n  routing   summary. ", actor="test-operator")
        check("set_summary stores collapsed one-line text",
              get(m1)["summary"] == "Pipeline routing summary.")
        raises("empty summary rejected", lambda: set_summary(m1, "   "))
        raises("oversize summary rejected", lambda: set_summary(m1, "x" * (SUMMARY_MAX + 1)))
        idx = routing_index("clark")
        check("routing_index has one entry per ACTIVE mission", [e["mission_id"] for e in idx] == [m1, m2])
        check("routing_index carries summaries", idx[0]["summary"] == "Pipeline routing summary.")
        # A+ ROUTER: routing_terms is additive routing data with its own budget
        check("routing_terms absent -> '' in index, not None", idx[0].get("routing_terms") == "")
        stored = set_routing_terms(m1, "  cover letter ,\nrecruiter   email,, cover letter, job application ",
                                   actor="test-operator")
        check("set_routing_terms normalises phrases, dedupes, collapses whitespace",
              stored == "cover letter, recruiter email, job application"
              and get(m1)["routing_terms"] == stored)
        check("routing_index carries routing_terms",
              [e for e in routing_index("clark") if e["mission_id"] == m1][0]["routing_terms"] == stored)
        check("routing_terms change is audited in mission_events",
              any("routing_terms set" in (e["reason"] or "") for e in events(m1)))
        raises("routing_terms over cap rejected",
               lambda: set_routing_terms(m1, ", ".join("phrase%03d" % i for i in range(120))))
        check("routing_terms cleared by empty text; index shows ''",
              set_routing_terms(m1, "") == "" and get(m1)["routing_terms"] is None
              and [e for e in routing_index("clark") if e["mission_id"] == m1][0]["routing_terms"] == "")
        # tolerance: a legacy store WITHOUT the column degrades to '' + WARN
        import io, contextlib, sqlite3 as _sq
        _legacy = DB_PATH + ".legacy"
        _lc = _sq.connect(_legacy)
        _lc.execute("CREATE TABLE missions (mission_id TEXT PRIMARY KEY, title TEXT, agent TEXT, "
                    "status TEXT, objective TEXT, summary TEXT, activated_utc TEXT)")
        _lc.execute("INSERT INTO missions VALUES ('m-legacy','Legacy','clark',?, 'obj','Legacy summary.','2026-01-01T00:00:00Z')", (ACTIVE,))
        _lc.commit(); _lc.close()
        _dbp = DB_PATH; DB_PATH = _legacy
        _err = io.StringIO()
        try:
            with contextlib.redirect_stderr(_err):
                _lidx = routing_index("clark")
        finally:
            DB_PATH = _dbp
        check("routing_index tolerates a pre-migration store (routing_terms -> '')",
              len(_lidx) == 1 and _lidx[0]["mission_id"] == "m-legacy" and _lidx[0]["routing_terms"] == "")
        check("missing column is diagnosed, not silent",
              "routing_terms absent" in _err.getvalue() and "--init" in _err.getvalue())
        check("routing_index carries NO charter text",
              not any("objective" in e for e in idx)
              and not any("Run the pipeline" in str(e) or "Other work" in str(e) for e in idx))

        # isolation
        m3 = create_mission("Han work", "Unconventional channels.")
        assign(m3, "han")
        check("han gets his own", current("han")["mission_id"] == m3)
        check("clark unaffected by han assignment",
              [o["mission_id"] for o in open_missions("clark")] == [m1, m2])
        check("han does not see clark's missions",
              [o["mission_id"] for o in open_missions("han")] == [m3])

        # BLOCKED = owned but not selectable (deactivate without closing)
        transition(m1, BLOCKED, reason="waiting on operator authorization")
        check("BLOCKED still owned", [o["mission_id"] for o in open_missions("clark")] == [m1, m2])
        check("BLOCKED not selectable",
              [a["mission_id"] for a in active_missions("clark")] == [m2]
              and [e["mission_id"] for e in routing_index("clark")] == [m2])
        transition(m1, ACTIVE, reason="unblocked")
        check("BLOCKED -> ACTIVE restores eligibility",
              [e["mission_id"] for e in routing_index("clark")] == [m1, m2])

        # supersede: removed from ownership and selection, archive intact
        transition(m1, SUPERSEDED, actor="test-operator", reason="replaced by m2")
        r = get(m1)
        check("SUPERSEDED stamps closed_utc", bool(r["closed_utc"]))
        check("superseded mission preserved in history",
              get(m1)["objective"] == "Run the pipeline.")
        check("superseded mission leaves selection immediately",
              [e["mission_id"] for e in routing_index("clark")] == [m2])
        check("current() explicit again once one remains",
              current("clark")["mission_id"] == m2)

        # N is data: a third synthetic mission joins and leaves by state only
        m5 = create_mission("Third area", "Third charter body.", summary="Third-area summary.")
        assign(m5, "clark")
        check("three-way: index grows by one entry", len(routing_index("clark")) == 2
              and [e["mission_id"] for e in routing_index("clark")] == [m2, m5])
        transition(m5, CANCELLED, reason="synthetic removed")
        check("cancelled mission leaves selection immediately",
              [e["mission_id"] for e in routing_index("clark")] == [m2])

        # history + archive
        h = history("clark")
        check("history lists all three clark missions (open + closed)", len(h) == 3)
        check("history queryable by mission id", get(m1)["mission_id"] == m1)
        check("archive excludes open when asked",
              len(history("clark", include_open=False)) == 2)
        check("han history separate", len(history("han")) == 1)

        # terminal is terminal
        raises("terminal state cannot transition",
               lambda: transition(m1, ACTIVE))
        raises("invalid status rejected",
               lambda: transition(m2, "SOMEDAY"))
        raises("unknown mission id raises",
               lambda: transition("m-nope", DONE))

        # events trail
        # routing_terms events (asserted separately above) are excluded here so
        # this check keeps counting exactly the transitions + the summary set.
        ev = [e for e in events(m1) if not str(e["reason"]).startswith("routing_terms set")]
        check("event trail records every transition plus the summary set",
              len(ev) == 6 and sum(1 for e in ev if e["from_status"] != e["to_status"]) == 5
              and any(str(e["reason"]).startswith("summary set") for e in ev))
        check("event trail records actor",
              any(e["actor"] == "test-operator" for e in ev))
        check("event trail ordered", ev[0]["to_status"] == DRAFT)

        # roster
        transition(m3, DONE, reason="finished")
        rs = roster()
        check("roster shows only open missions", len(rs) == 1)
        check("roster names the agent", rs[0]["agent"] == "clark")
        check("DONE mission still in history", len(history("han")) == 1)
        check("DONE stamps closed_utc", bool(get(m3)["closed_utc"]))

        # generic: a brand-new agent works with no code change
        m4 = create_mission("New agent work", "Anything.")
        assign(m4, "anvil")
        check("unknown-but-valid agent works with no code change",
              current("anvil")["mission_id"] == m4)

        # migration: a store built with the OLD one-open schema upgrades in
        # place -- unique index dropped, summary column added, rows intact
        old = os.path.join(tmp, "old.db")
        c = sqlite3.connect(old)
        c.executescript("""
CREATE TABLE missions (mission_id TEXT PRIMARY KEY, title TEXT NOT NULL,
  agent TEXT, status TEXT NOT NULL, objective TEXT NOT NULL, dod TEXT,
  source_path TEXT, created_utc TEXT NOT NULL, activated_utc TEXT,
  closed_utc TEXT, notes TEXT, evidence_ref TEXT);
CREATE UNIQUE INDEX idx_one_open_per_agent ON missions (agent)
  WHERE status IN ('ACTIVE','BLOCKED');
CREATE TABLE mission_events (id INTEGER PRIMARY KEY, mission_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT);
INSERT INTO missions VALUES ('m-old1','Legacy one','aa','ACTIVE','Legacy body.',
  NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:01Z',NULL,NULL,NULL);
""")
        c.commit(); c.close()
        DB_PATH = old
        init_db()
        c = sqlite3.connect(old)
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")]
        cols = [r[1] for r in c.execute("PRAGMA table_info(missions)")]
        c.close()
        check("migration drops the one-open unique index", "idx_one_open_per_agent" not in names)
        check("migration adds the (agent,status) index", "idx_missions_agent_status" in names)
        check("migration adds summary column", "summary" in cols)
        check("migration adds grants column", "grants" in cols)
        # grants: operator data, code-evaluated
        check("no grants by default", grants_for("m-old1") == [])
        check("grant records and is idempotent",
              grant("m-old1", "serpens.search", actor="test-operator") == ["serpens.search"]
              and grant("m-old1", "Serpens.Search") == ["serpens.search"]
              and grants_for("m-old1") == ["serpens.search"])
        check("second grant sorted alongside", grant("m-old1", "board.list") == ["board.list", "serpens.search"])
        check("revoke removes only the named op",
              revoke("m-old1", "board.list") == ["serpens.search"] and grants_for("m-old1") == ["serpens.search"])
        raises("invalid op name rejected", lambda: grant("m-old1", "not an op!"))
        raises("grant on unknown mission rejected", lambda: grant("m-nope", "serpens.search"))
        check("grants on unknown mission read as none", grants_for("m-nope") == [])
        check("grant events recorded",
              sum(1 for e in events("m-old1") if str(e["reason"]).startswith(("grant ", "revoke "))) == 3)
        check("grants do not leak into routing_index",
              all("grants" not in e for e in routing_index("aa")))
        check("migration leaves legacy row intact",
              get("m-old1")["objective"] == "Legacy body." and get("m-old1")["status"] == ACTIVE)
        m6 = create_mission("Legacy two", "Second body.")
        assign(m6, "aa")
        check("migrated store accepts a second concurrent mission",
              [o["mission_id"] for o in open_missions("aa")] == ["m-old1", m6])
        check("migration idempotent", init_db() == old)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (n[0], len(fails)))
    for f in fails:
        print("  FAILED:", f)
    return 1 if fails else 0


def _usage():
    print(__doc__.strip().splitlines()[0])
    print("usage: missions.py --selftest | --init | --roster | "
          "--history [agent] | --show <mission_id> | --open <agent> | "
          "--index <agent> | --create <title> <objective-file> [summary-file] | "
          "--summary <mission_id> <summary-file> | --terms <mission_id> <terms-file> | --assign <mission_id> <agent> | "
          "--transition <mission_id> <STATUS> [reason...] | "
          "--grant <mission_id> <op> | --revoke <mission_id> <op> | --grants <mission_id>")
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(_usage())
    cmd = args[0]
    if cmd == "--selftest":
        sys.exit(_selftest())
    if cmd == "--init":
        print(init_db())
        sys.exit(0)
    if cmd == "--roster":
        for r in roster():
            print("%-12s %-28s %-8s %s" % (r["agent"], r["mission_id"],
                                           r["status"], r["title"]))
        sys.exit(0)
    if cmd == "--history":
        who = args[1] if len(args) > 1 else None
        for r in history(who):
            print("%-28s %-10s %-11s %s" % (r["mission_id"], r["agent"] or "-",
                                            r["status"], r["title"]))
        sys.exit(0)
    if cmd in ("--grant", "--revoke"):
        if len(args) < 3:
            sys.exit(_usage())
        fn = grant if cmd == "--grant" else revoke
        print(" ".join(fn(args[1], args[2])) or "(no grants)")
        sys.exit(0)
    if cmd == "--grants":
        if len(args) < 2:
            sys.exit(_usage())
        print(" ".join(grants_for(args[1])) or "(no grants)")
        sys.exit(0)
    if cmd == "--open":
        if len(args) < 2:
            sys.exit(_usage())
        for r in open_missions(args[1]):
            print("%-28s %-8s %s" % (r["mission_id"], r["status"], r["title"]))
        sys.exit(0)
    if cmd == "--index":
        if len(args) < 2:
            sys.exit(_usage())
        for e in routing_index(args[1]):
            print("%s | %s | %s" % (e["mission_id"], e["title"], e["summary"]))
        sys.exit(0)
    if cmd == "--create":
        if len(args) < 3:
            sys.exit(_usage())
        with open(args[2], encoding="utf-8") as fh:
            text = fh.read()
        summ = None
        if len(args) > 3:
            with open(args[3], encoding="utf-8") as fh:
                summ = fh.read()
        print(create_mission(args[1], text, source_path=args[2], summary=summ))
        sys.exit(0)
    if cmd == "--summary":
        if len(args) < 3:
            sys.exit(_usage())
        with open(args[2], encoding="utf-8") as fh:
            print(set_summary(args[1], fh.read()))
        sys.exit(0)
    if cmd == "--assign":
        if len(args) < 3:
            sys.exit(_usage())
        print(assign(args[1], args[2], actor=(args[3] if len(args) > 3 else "operator")))
        sys.exit(0)
    if cmd == "--transition":
        if len(args) < 3:
            sys.exit(_usage())
        print(transition(args[1], args[2], reason=(" ".join(args[3:]) or None)))
        sys.exit(0)
    if cmd == "--terms":
        if len(args) < 3:
            sys.exit(_usage())
        with open(args[2], encoding="utf-8") as fh:
            print(set_routing_terms(args[1], fh.read()))
        sys.exit(0)
    if cmd == "--show":
        if len(args) < 2:
            sys.exit(_usage())
        r = get(args[1])
        if r is None:
            print("no such mission", file=sys.stderr)
            sys.exit(1)
        for k in r.keys():
            print("%-14s %s" % (k, r[k]))
        print("-- events --")
        for e in events(args[1]):
            print("%s  %-10s -> %-10s  %-10s %s"
                  % (e["ts_utc"], e["from_status"] or "-", e["to_status"],
                     e["actor"], e["reason"] or ""))
        sys.exit(0)
    sys.exit(_usage())
