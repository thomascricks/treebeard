"""observations.py - Moon's append-only observation store.

Moon OBSERVES. It does not hypothesise and it does not write personality state:
there is no adaptive-write path in this module, and the tbpersona UID boundary
blocks Moon at the filesystem regardless.

Keyed to routing_decisions.decision_id (== llm_calls.session_id), so an
observation joins back to the decision that produced it and to every call in
that walk.

APPEND-ONLY by contract: this module exposes record() and read helpers, and no
update or delete. Evidence that can be rewritten is not evidence.

NO INVENTED QUALITY SCORE. validator_result, correction_signal and
quality_note are NULL unless something real produced them. status='ok' means
the HTTP call succeeded -- it is not a judgement about the answer, and
recording it as one would poison every hypothesis built on top.

synthetic=1 marks fixtures and test rows so future learning can exclude them.
"""
import sqlite3
import datetime as _dt
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY,
    ts_utc            TEXT NOT NULL,
    decision_id       TEXT NOT NULL,      -- joins routing_decisions + llm_calls
    agent             TEXT NOT NULL,
    task_class        TEXT,

    -- what actually happened
    outcome_status    TEXT,               -- ok | failed
    final_tier        TEXT,
    final_model       TEXT,
    selection_method  TEXT,
    degraded          INTEGER,
    escalated         INTEGER,
    retries           INTEGER,            -- rungs that ran and failed
    latency_ms        INTEGER,
    cost_usd          REAL,
    cost_basis        TEXT,

    -- state at execution time
    personality_rev   TEXT,
    model_profile_rev TEXT,

    -- REAL signals only. NULL when nothing observed them. Do not synthesise.
    validator_result  TEXT,               -- pass | reject | NULL (no validator)
    correction_signal TEXT,               -- operator/user correction, if any
    quality_note      TEXT,               -- free text from a real observer

    synthetic         INTEGER DEFAULT 0,  -- 1 = fixture, exclude from learning
    source            TEXT                -- what produced this row
);
CREATE INDEX IF NOT EXISTS idx_obs_decision ON observations(decision_id);
CREATE INDEX IF NOT EXISTS idx_obs_agent    ON observations(agent);
CREATE INDEX IF NOT EXISTS idx_obs_synth    ON observations(synthetic);
"""


def ensure_schema(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def record(decision_id, agent, *, task_class=None, outcome_status=None,
           final_tier=None, final_model=None, selection_method=None,
           degraded=None, escalated=None, retries=None, latency_ms=None,
           cost_usd=None, cost_basis=None, personality_rev=None,
           model_profile_rev=None, validator_result=None,
           correction_signal=None, quality_note=None, synthetic=0,
           source="moon", db_path=DB_PATH):
    """Append one observation. Returns row id, or None on failure.

    Never raises: an observation failure must not disturb the work being
    observed.
    """
    try:
        ensure_schema(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            cur = con.execute(
                """INSERT INTO observations
                   (ts_utc, decision_id, agent, task_class, outcome_status,
                    final_tier, final_model, selection_method, degraded,
                    escalated, retries, latency_ms, cost_usd, cost_basis,
                    personality_rev, model_profile_rev, validator_result,
                    correction_signal, quality_note, synthetic, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 decision_id, agent, task_class, outcome_status, final_tier,
                 final_model, selection_method, degraded, escalated, retries,
                 latency_ms, cost_usd, cost_basis, personality_rev,
                 model_profile_rev, validator_result, correction_signal,
                 quality_note, int(synthetic), source))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[observations] record failed ({e}); work unaffected\n")
        return None


def observe_decision(decision_id, *, synthetic=0, source="moon",
                     validator_result=None, correction_signal=None,
                     quality_note=None, db_path=DB_PATH):
    """Build an observation by reading back what the router already recorded.

    Moon derives the mechanical facts from routing_decisions + llm_calls rather
    than being told them, so an observation cannot disagree with the ledger.
    Judgement fields stay caller-supplied and default to NULL.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        d = con.execute("SELECT * FROM routing_decisions WHERE decision_id=?",
                        (decision_id,)).fetchone()
        if d is None:
            return None
        calls = con.execute(
            "SELECT tier, status, latency_ms, cost_usd, cost_basis "
            "FROM llm_calls WHERE session_id=? ORDER BY id", (decision_id,)
        ).fetchall()
    finally:
        con.close()
    okc = [c for c in calls if c["status"] == "ok"]
    last = okc[-1] if okc else (calls[-1] if calls else None)
    return record(
        decision_id, d["agent"], task_class=d["task_class"],
        outcome_status=d["outcome_status"], final_tier=d["final_tier"],
        final_model=d["final_model"], selection_method=d["selection_method"],
        degraded=d["degraded"], escalated=d["escalated"],
        retries=max(len(calls) - 1, 0),
        latency_ms=(last["latency_ms"] if last else None),
        cost_usd=(sum(c["cost_usd"] or 0 for c in calls) if calls else None),
        cost_basis=(last["cost_basis"] if last else None),
        personality_rev=d["personality_rev"],
        model_profile_rev=d["model_profile_rev"],
        validator_result=validator_result, correction_signal=correction_signal,
        quality_note=quality_note, synthetic=synthetic, source=source,
        db_path=db_path)


def for_learning(agent=None, include_synthetic=False, db_path=DB_PATH):
    """Observations fit for LEARNING. Defaults to real traffic only.

    synthetic=1 is test data and synthetic IS NULL is unclassified; both are
    excluded unless explicitly requested. Excluding NULL matters as much as
    excluding 1: an unclassified row is not evidence that something is real,
    and treating omission as real-ness is how fixtures reached the ledger
    unlabelled in the first place.

    Joins on canonical_agent so attribution typos (treebead -> treebeard)
    resolve to the right agent without the raw ledger being rewritten.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        q = "SELECT * FROM observations WHERE 1=1"
        args = []
        if not include_synthetic:
            q += " AND COALESCE(synthetic, 1) = 0"
        if agent:
            q += " AND agent = ?"
            args.append(agent)
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]
    finally:
        con.close()
