"""decisions.py - structured routing-decision store.

One row per ROUTING DECISION, joined to llm_calls via the correlation id:

    llm_calls.session_id = routing_decisions.decision_id

Keyed by correlation id rather than llm_calls.id deliberately. The decision is
made BEFORE any call is logged, so no llm_calls.id exists yet; and one decision
can produce SEVERAL llm_calls rows, because the walk may try a rung, fail, and
try the next. The relationship is one-to-many, so the id has to be minted by
the router and carried into every log_call for that walk.

Designed for the adaptive system, not just today's router: personality and
model-profile revisions are nullable now and populate once those layers land.
A decision must be fully reconstructable from this row WITHOUT parsing
router_reason.

Never raises into the router: a telemetry failure must not fail a call.
"""
import json
import sqlite3
import uuid
import datetime as _dt
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id      TEXT PRIMARY KEY,   -- uuid4; == llm_calls.session_id
    ts_utc           TEXT NOT NULL,
    agent            TEXT NOT NULL,
    task_class       TEXT,               -- simple|medium|complex; NULL if forced
    forced_tier      TEXT,               -- non-NULL when the caller forced
    eligible_json    TEXT,               -- ["advanced_b","frontier","advanced_a"]
    eliminated_json  TEXT,               -- {"local":"below-floor(basic<advanced)"}
    scores_json      TEXT,               -- {"advanced_b":{"base":1.2,"rate_norm":0.68,...}}
    band_json        TEXT,               -- near-tie members, ranked
    selected_tier    TEXT,
    selected_model   TEXT,
    selection_method TEXT,               -- score|round_robin|personality_tiebreak
                                         -- |explore|forced|degraded|policy-fallback
    rate_basis_json  TEXT,               -- {"advanced_b":{"rate":6.25,"basis":"price_table"}}
    config_json      TEXT,               -- epsilon/reference actually applied
    final_tier       TEXT,               -- tier that actually produced the result
    final_model      TEXT,
    degraded         INTEGER,             -- 1 = landed BELOW the intended start
    escalated        INTEGER,             -- 1 = landed ABOVE it (not degradation)
    outcome_status   TEXT,                -- ok | failed
    personality_rev  TEXT,               -- nullable until personality lands
    model_profile_rev TEXT,              -- nullable until profiles land
    notes            TEXT,
    synthetic        INTEGER,            -- 1 = not a real provider call
    canonical_agent  TEXT                -- agent after alias resolution
);
CREATE INDEX IF NOT EXISTS idx_rd_ts    ON routing_decisions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_rd_agent ON routing_decisions(agent);
CREATE INDEX IF NOT EXISTS idx_rd_method ON routing_decisions(selection_method);
"""


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


def new_id():
    return str(uuid.uuid4())


def ensure_schema(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.executescript(SCHEMA)
        _migrate_columns(con, "routing_decisions", [
            ("synthetic", "INTEGER"), ("canonical_agent", "TEXT")])
        con.commit()
    finally:
        con.close()


def _j(v):
    try:
        return json.dumps(v, sort_keys=True, default=str)
    except Exception:
        return None


def record(decision_id, agent, *, task_class=None, forced_tier=None,
           eligible=None, eliminated=None, scores=None, band=None,
           selected_tier=None, selected_model=None, selection_method=None,
           rate_basis=None, config=None, personality_rev=None,
           model_profile_rev=None, notes=None, synthetic=None,
           canonical_agent=None, db_path=DB_PATH):
    """Write one decision row. Returns True/False; never raises.

    A failure here must never fail the cognition call it is describing --
    same contract as telemetry.log_call.
    """
    try:
        ensure_schema(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute(
                """INSERT OR REPLACE INTO routing_decisions
                   (decision_id, ts_utc, agent, task_class, forced_tier,
                    eligible_json, eliminated_json, scores_json, band_json,
                    selected_tier, selected_model, selection_method,
                    rate_basis_json, config_json, personality_rev,
                    model_profile_rev, notes, synthetic, canonical_agent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id,
                 _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 agent, task_class, forced_tier,
                 _j(eligible), _j(eliminated), _j(scores), _j(band),
                 selected_tier, selected_model, selection_method,
                 _j(rate_basis), _j(config), personality_rev,
                 model_profile_rev, notes, synthetic,
                 (canonical_agent or agent)))
            con.commit()
            return True
        finally:
            con.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[decisions] record failed ({e}); call unaffected\n")
        return False


def record_outcome(decision_id, *, final_tier=None, final_model=None,
                   ladder=None, selected_tier=None, outcome_status=None,
                   db_path=DB_PATH):
    """Post-walk update. selection_method is NEVER overwritten: why the router
    chose a starting candidate and whether that choice survived the provider
    walk are two different facts, and Moon needs both. A route can be chosen by
    round-robin or personality and then degrade.

    `degraded` means the walk landed BELOW the intended start on the ladder --
    not merely elsewhere. Landing above it is escalation, tracked separately;
    conflating the two would feed Moon a wrong signal.
    """
    try:
        deg = esc = None
        if ladder and selected_tier in (ladder or []) and final_tier in (ladder or []):
            si, fi = ladder.index(selected_tier), ladder.index(final_tier)
            deg, esc = int(fi < si), int(fi > si)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute(
                """UPDATE routing_decisions
                   SET final_tier=?, final_model=?, degraded=?, escalated=?,
                       outcome_status=?
                   WHERE decision_id=?""",
                (final_tier, final_model, deg, esc, outcome_status, decision_id))
            con.commit()
            return True
        finally:
            con.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[decisions] outcome update failed ({e})\n")
        return False


def reconstruct(decision_id, db_path=DB_PATH):
    """Full decision + every llm_call it produced. Proof that a decision is
    recoverable without parsing router_reason."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        d = con.execute("SELECT * FROM routing_decisions WHERE decision_id=?",
                        (decision_id,)).fetchone()
        if d is None:
            return None
        out = {k: d[k] for k in d.keys()}
        for f in ("eligible_json", "eliminated_json", "scores_json",
                  "band_json", "rate_basis_json", "config_json"):
            if out.get(f):
                try:
                    out[f[:-5]] = json.loads(out[f])
                except ValueError:
                    pass
        out["calls"] = [dict(r) for r in con.execute(
            "SELECT id, ts_utc, tier, model, status, tokens_in, tokens_out, "
            "cost_usd, cost_basis FROM llm_calls WHERE session_id=? ORDER BY id",
            (decision_id,))]
        return out
    finally:
        con.close()
