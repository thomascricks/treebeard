"""hypotheses.py - Dream's append-only PROPOSAL store.

Dream HYPOTHESISES. It does not decide and it cannot authorise anything.

A hypothesis is a CLAIM ABOUT EVIDENCE, not a command. Every row must carry the
observation ids or query basis behind it so Treebeard can audit why it exists
and re-derive the claim independently.

AUTHORITY LIVES ELSEWHERE. This store is in the telemetry database, which
`treebeard` (and therefore Dream, sharing that UID) can write. SQLite
permissions are file-level, so ANY column here is forgeable by raw SQL --
including `status`. Therefore:

    status here is INFORMATIONAL ONLY and MUST NEVER authorise a mutation.

Approval is recorded in a SEPARATE database owned by tbpersona, which
treebeard cannot write at all. persona_writer acts solely on that protected
record, keyed to hypothesis_id + content_hash. A hypothesis forged as
'accepted' in this file changes nothing, because nothing reads this field for
authority.

content_hash binds the claim to its content: if a proposal is edited after
approval, the hash stops matching and the writer refuses.
"""
import hashlib
import json
import sqlite3
import datetime as _dt
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"

VALID_DIRECTIONS = ("increase", "decrease")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id   TEXT PRIMARY KEY,
    ts_utc          TEXT NOT NULL,
    agent           TEXT NOT NULL,
    dimension       TEXT NOT NULL,
    direction       TEXT NOT NULL,      -- increase | decrease
    magnitude       REAL,               -- proposed delta, subject to bounds
    confidence      REAL,
    support_n       INTEGER,            -- observations behind the claim
    conditioned_on  TEXT,               -- task class / context the claim covers
    observation_ids TEXT,               -- JSON list -> observations.id
    query_basis     TEXT,               -- how the claim was derived, auditable
    rationale       TEXT,
    proposer        TEXT,
    content_hash    TEXT NOT NULL,      -- binds approval to exact content
    status          TEXT DEFAULT 'proposed',  -- INFORMATIONAL ONLY. Not authority.
    synthetic       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hyp_agent ON hypotheses(agent);
CREATE INDEX IF NOT EXISTS idx_hyp_synth ON hypotheses(synthetic);
"""


def ensure_schema(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


# Fields whose alteration changes WHAT IS BEING CLAIMED. All are hashed.
SUBSTANTIVE_FIELDS = ("agent", "dimension", "direction", "magnitude",
                      "confidence", "support_n", "conditioned_on",
                      "observation_ids", "query_basis", "rationale")

# Metadata: descriptive, not part of the claim. Deliberately NOT hashed, so a
# status transition or a re-proposal timestamp does not invalidate approval.
METADATA_FIELDS = ("hypothesis_id", "ts_utc", "proposer", "status", "synthetic")


def content_hash(agent, dimension, direction, magnitude, observation_ids,
                 confidence=None, support_n=None, conditioned_on=None,
                 query_basis=None, rationale=None):
    """Canonical hash of the ENTIRE substantive claim.

    Every field a reader would use to judge the claim is bound. An earlier
    version hashed only agent/dimension/direction/magnitude/observation_ids --
    which left confidence, support_n, conditioned_on, query_basis and rationale
    mutable after approval. Since the proposal database is writable by
    treebeard (and therefore Dream), those could have been rewritten while the
    hash still matched, so a protected approval would have authorised a
    materially different hypothesis.

    Canonical serialization: sorted keys, normalised numerics, sorted
    observation ids, None distinguished from empty string.
    """
    obs = sorted(observation_ids or [])
    payload = {
        "agent": agent,
        "dimension": dimension,
        "direction": direction,
        "magnitude": None if magnitude is None else round(float(magnitude), 6),
        "confidence": None if confidence is None else round(float(confidence), 6),
        "support_n": None if support_n is None else int(support_n),
        "conditioned_on": conditioned_on,
        "observation_ids": obs,
        "query_basis": query_basis,
        "rationale": rationale,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def hash_row(row):
    """Recompute the hash from a stored row (dict or sqlite3.Row). Used by the
    writer to detect post-approval tampering."""
    obs = row["observation_ids"]
    if isinstance(obs, str):
        obs = json.loads(obs or "[]")
    return content_hash(row["agent"], row["dimension"], row["direction"],
                        row["magnitude"], obs, row["confidence"],
                        row["support_n"], row["conditioned_on"],
                        row["query_basis"], row["rationale"])


def propose(agent, dimension, direction, *, magnitude=None, confidence=None,
            observation_ids=None, query_basis=None, conditioned_on=None,
            rationale=None, proposer="dream", synthetic=0, db_path=DB_PATH):
    """Append one hypothesis. Returns (hypothesis_id, content_hash) or None.

    Deliberately has NO status parameter: every proposal enters as 'proposed'.
    That is a convention, not a guarantee -- the field is forgeable by raw SQL
    and is never consulted for authority.
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}")
    if not observation_ids and not query_basis:
        raise ValueError("a hypothesis must cite observation_ids or a "
                         "query_basis: an unauditable claim is not evidence")
    obs = list(observation_ids or [])
    hid = "h-" + hashlib.sha256(
        f"{agent}{dimension}{direction}{_dt.datetime.now(_dt.timezone.utc)}"
        .encode()).hexdigest()[:16]
    chash = content_hash(agent, dimension, direction, magnitude, obs,
                         confidence, len(obs), conditioned_on, query_basis,
                         rationale)
    try:
        ensure_schema(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute(
                """INSERT INTO hypotheses
                   (hypothesis_id, ts_utc, agent, dimension, direction,
                    magnitude, confidence, support_n, conditioned_on,
                    observation_ids, query_basis, rationale, proposer,
                    content_hash, status, synthetic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?)""",
                (hid, _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 agent, dimension, direction, magnitude, confidence, len(obs),
                 conditioned_on, json.dumps(obs), query_basis, rationale,
                 proposer, chash, int(synthetic)))
            con.commit()
            return hid, chash
        finally:
            con.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[hypotheses] propose failed ({e})\n")
        return None


def get(hypothesis_id, db_path=DB_PATH):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?",
                        (hypothesis_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def for_learning(agent=None, include_synthetic=False, db_path=DB_PATH):
    """Hypotheses fit for learning. Real only by default; see the equivalent
    note in observations.for_learning() on why NULL is excluded too."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        q = "SELECT * FROM hypotheses WHERE 1=1"
        args = []
        if not include_synthetic:
            q += " AND COALESCE(synthetic, 1) = 0"
        if agent:
            q += " AND agent = ?"
            args.append(agent)
        return [dict(r) for r in con.execute(q + " ORDER BY rowid", args)]
    finally:
        con.close()
