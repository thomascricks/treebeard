"""decision_store.py - the PROTECTED approval store. Owned by tbpersona.

This is the authority boundary. The proposal database (telemetry.db) is
writable by treebeard, and therefore by Dream, so ANY field there -- including
`status` -- is forgeable by raw SQL. Nothing in that file authorises anything.

Approval lives HERE, in a separate SQLite file that treebeard can read for
audit but cannot write. An approval row is keyed to hypothesis_id AND the
full-content hash, so:

  * a hypothesis forged as 'accepted' in the proposal DB has no approval row
    here, and the writer refuses it;
  * a hypothesis approved and THEN edited fails the hash comparison, and the
    writer refuses it.

Append-only in spirit: decisions are inserted, never updated. A reversal is a
new row, so the history of who decided what, and when, survives.
"""
import sqlite3
import datetime as _dt

DB_PATH = "/opt/treebeard/persona/decisions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    ts_utc        TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    content_hash  TEXT NOT NULL,   -- binds approval to the exact claim
    decision      TEXT NOT NULL,   -- approved | rejected | revoked
    decided_by    TEXT NOT NULL,
    rationale     TEXT,
    consumed      INTEGER DEFAULT 0,  -- 1 once a mutation has used it
    synthetic     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dec_hyp ON decisions(hypothesis_id);
"""


def ensure_schema(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def decide(hypothesis_id, content_hash, decision, decided_by,
           rationale=None, synthetic=0, db_path=DB_PATH):
    """Record an approval/rejection. Insert only; never updates a prior row."""
    if decision not in ("approved", "rejected", "revoked"):
        raise ValueError(f"invalid decision {decision!r}")
    ensure_schema(db_path)
    con = sqlite3.connect(db_path, timeout=5)
    try:
        cur = con.execute(
            """INSERT INTO decisions (ts_utc, hypothesis_id, content_hash,
               decision, decided_by, rationale, synthetic)
               VALUES (?,?,?,?,?,?,?)""",
            (_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
             hypothesis_id, content_hash, decision, decided_by, rationale,
             int(synthetic)))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def current_approval(hypothesis_id, db_path=DB_PATH):
    """Latest unconsumed approval for a hypothesis, or None.

    Latest-wins so a 'revoked' row after an 'approved' row withdraws authority
    without deleting history.
    """
    ensure_schema(db_path)
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT * FROM decisions WHERE hypothesis_id=? "
            "ORDER BY id DESC LIMIT 1", (hypothesis_id,)).fetchone()
        if r is None or r["decision"] != "approved" or r["consumed"]:
            return None
        return dict(r)
    finally:
        con.close()


def mark_consumed(decision_id, db_path=DB_PATH):
    """One approval authorises ONE mutation. Prevents replay."""
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("UPDATE decisions SET consumed=1 WHERE id=?", (decision_id,))
        con.commit()
    finally:
        con.close()
