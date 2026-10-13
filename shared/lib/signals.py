"""signals.py - append-only store of OBSERVED quality signals.

Deliberately separates OBSERVATION from DERIVATION. This module records
concrete things that happened. It computes no score. Performance is derived
elsewhere, later, and only where enough non-synthetic evidence exists.

There is no ground truth for most calls -- nobody reviews the output -- so
"quality" can only be inferred from proxies. The discipline is to record
proxies that are objectively observable and refuse the ones that are not:

  RECORDED (mechanical, objective, emitted by the router):
    truncated        finish_reason == max_tokens; output cut mid-generation
    empty_output     zero-length or whitespace-only response
    retry_before_ok  a rung ran and failed before another succeeded
    validator_pass   caller-supplied validate() returned True
    validator_reject caller-supplied validate() returned False

  RECORDED (requires a real observer; NULL until one exists):
    operator_correction, operator_reject, task_abandoned

  REFUSED:
    any 0..1 "quality score" with no measurement behind it
    status == 'ok' as a proxy for quality -- it means HTTP 200
    cross-model agreement -- that measures conformity, not correctness

Every signal carries polarity so derivation never has to guess whether a
signal type is good or bad, and a source so a mechanical signal is never
mistaken for a human judgement.
"""
import sqlite3
import datetime as _dt
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"

# signal_type -> (polarity, requires_human)
#   polarity: -1 degrades, +1 affirms, 0 neutral/informational
SIGNAL_TYPES = {
    "truncated":            (-1, False),
    "empty_output":         (-1, False),
    "retry_before_ok":      (-1, False),
    "validator_reject":     (-1, False),
    "validator_pass":       (+1, False),
    # A+ ROUTER (2026-09-04): a rung recovered by the router's own in-rung
    # retry (thinking-off after reasoning-exhausted). NEUTRAL by design: the
    # failed attempt already carried its own negative signals, and scoring the
    # recovery against the rung that just worked would teach the selector to
    # avoid it. Polarity 0 is inert to perf.derive (sums <0 and >0 only).
    "retry_in_rung":        (0, False),
    "operator_correction":  (-1, True),
    "operator_reject":      (-1, True),
    "task_abandoned":       (-1, True),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    ts_utc        TEXT NOT NULL,
    decision_id   TEXT,               -- joins routing_decisions / llm_calls
    call_id       INTEGER,            -- llm_calls.id when attributable
    agent         TEXT,
    task_class    TEXT,
    tier          TEXT NOT NULL,
    model         TEXT,
    signal_type   TEXT NOT NULL,
    polarity      INTEGER NOT NULL,   -- -1 degrades, +1 affirms, 0 neutral
    requires_human INTEGER NOT NULL,  -- 1 = only a real observer can emit it
    detail        TEXT,
    source        TEXT NOT NULL,      -- router | validator | operator | moon
    synthetic     INTEGER,            -- NULL = unclassified, excluded from learning
    UNIQUE(decision_id, call_id, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_sig_tier  ON signals(tier, task_class);
CREATE INDEX IF NOT EXISTS idx_sig_dec   ON signals(decision_id);
CREATE INDEX IF NOT EXISTS idx_sig_synth ON signals(synthetic);
"""


def ensure_schema(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.executescript(SCHEMA); con.commit()
    finally:
        con.close()


def emit(signal_type, tier, *, decision_id=None, call_id=None, agent=None,
         task_class=None, model=None, detail=None, source="router",
         synthetic=None, db_path=DB_PATH):
    """Append one observed signal. Returns row id, or None. Never raises.

    An unknown signal_type is REFUSED rather than recorded: an unrecognised
    signal has no defined polarity, and a signal whose meaning is unknown
    cannot honestly contribute to a performance figure.
    """
    spec = SIGNAL_TYPES.get(signal_type)
    if spec is None:
        import sys
        sys.stderr.write(f"[signals] unknown signal_type {signal_type!r}; refused\n")
        return None
    polarity, needs_human = spec
    if needs_human and source in ("router", "validator"):
        import sys
        sys.stderr.write(f"[signals] {signal_type!r} requires a human observer; "
                         f"refused from source={source!r}\n")
        return None
    try:
        ensure_schema(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            cur = con.execute(
                """INSERT OR IGNORE INTO signals
                   (ts_utc, decision_id, call_id, agent, task_class, tier, model,
                    signal_type, polarity, requires_human, detail, source, synthetic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 decision_id, call_id, agent, task_class, tier, model,
                 signal_type, polarity, int(needs_human), detail, source,
                 synthetic))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[signals] emit failed ({e}); work unaffected\n")
        return None


def counts(tier=None, task_class=None, include_synthetic=False, db_path=DB_PATH):
    """Raw signal counts. NO derived score -- callers decide what to make of
    these, and only above a threshold they set."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        q = ("SELECT tier, COALESCE(task_class,'-'), signal_type, polarity, "
             "COUNT(*) FROM signals WHERE 1=1")
        args = []
        if not include_synthetic:
            q += " AND COALESCE(synthetic, 1) = 0"
        if tier:
            q += " AND tier = ?"; args.append(tier)
        if task_class:
            q += " AND task_class = ?"; args.append(task_class)
        q += " GROUP BY tier, task_class, signal_type, polarity"
        return [tuple(r) for r in con.execute(q, args)]
    finally:
        con.close()
