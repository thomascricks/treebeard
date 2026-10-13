#!/usr/bin/env python3
"""persona_writer.py - the ONLY path that may write adaptive personality state.

Runs as UID tbpersona via a single narrowly-scoped sudoers rule. Adaptive files
are tbpersona-owned; bounds are root-owned. Neither treebeard, moon, sun, nor
Dream-running-as-treebeard can write either directly -- verified adversarially.

KNOWN LIMITATION, stated rather than implied: Dream currently runs as the
treebeard user, so Dream can invoke this same sudo rule. The UID boundary stops
direct file writes; it does not distinguish Dream from Treebeard at the call
site. Closing that requires Dream to run under its own UID -- a launch-path
change tracked separately.

Bounds are re-read HERE, at write time, from the root-owned file. An
out-of-bounds request is REJECTED, never silently clamped: clamping would let a
caller discover the limit by probing and would record a change nobody asked for.
"""
import argparse, json, os, re, stat, sys, datetime
sys.path.insert(0, "/opt/treebeard/shared/lib")
import personality as P

AUDIT_DB = "/opt/treebeard/shared/telemetry/telemetry.db"

# --agent is joined into file paths (personality._paths), so it is validated
# HERE, before either the forward or the rollback branch, and fails CLOSED:
# a name is valid only if it is a plain lowercase token AND a root-owned
# regular <agent>.charter.txt is installed. The charter directory is fixed,
# never read from the environment.
CHARTER_DIR = "/etc/treebeard"
_AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def valid_agent(agent, charter_dir=CHARTER_DIR, require_root=True):
    """True only for an installed charter name. Any doubt -> False."""
    try:
        if not isinstance(agent, str) or not _AGENT_NAME_RE.fullmatch(agent):
            return False
        if (os.sep in agent or (os.altsep and os.altsep in agent)
                or os.path.isabs(agent) or agent in (".", "..")):
            return False
        st = os.lstat(os.path.join(charter_dir, f"{agent}.charter.txt"))
        if not stat.S_ISREG(st.st_mode):
            return False
        if require_root and st.st_uid != 0:
            return False
        return True
    except Exception:
        return False


def fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


def rollback(a):
    """Restore a prior adaptive file BYTE-FOR-BYTE from its versioned snapshot.

    Not a side door: requires an approved, unconsumed decision in the
    protected store keyed to "rollback:<agent>:<revision>", exactly like a
    forward change. treebeard cannot forge that record.

    Restores file CONTENT, not a recomputed value: reconstructing from numbers
    would lose confidence, evidence links and timestamps that were part of the
    prior state. History is never rewritten -- the rollback is appended as its
    own audit row citing both the superseded and restored revisions.
    """
    import glob, sqlite3, shutil as _sh
    sys.path.insert(0, "/opt/treebeard/persona")
    import decision_store as DS

    target = f"rollback:{a.agent}:{a.to_revision}"
    approval = DS.current_approval(target)
    if approval is None:
        fail(f"REFUSED: no approved, unconsumed decision for {target!r}")
    # Independent verification: do not trust the target string alone. The
    # approval must name this exact agent and revision, and that revision must
    # actually exist for this agent.
    expected = f"rollback:{a.agent}:{int(a.to_revision)}"
    if approval["content_hash"] != expected:
        fail(f"REFUSED: approval binds {approval['content_hash']!r}, "
             f"request is {expected!r}")
    if P.load_bounds(a.agent) is None:
        fail(f"REFUSED: {a.agent!r} has no bounds")

    _, path = P._paths(a.agent)
    snaps = sorted(glob.glob(f"{path}.v{a.to_revision}-*"))
    if not snaps:
        fail(f"no snapshot for {a.agent} revision {a.to_revision}")
    snap = snaps[-1]

    _vals, cur_rev = P.load_adaptive(a.agent)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # snapshot what we are about to supersede, so rollback is itself reversible
    if os.path.exists(path):
        _sh.copy2(path, f"{path}.v{cur_rev}-{now.replace(':','')}")

    con = sqlite3.connect(AUDIT_DB, timeout=5)
    cur = con.execute("""INSERT INTO personality_revisions
        (ts_utc,agent,dimension,old_value,new_value,bounds_min,bounds_max,
         adaptation_rate,hypothesis_id,rationale,actor,revision_after,applied)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (now, a.agent, "*ROLLBACK*", None, None, None, None, None,
         target, f"ROLLBACK: superseding r{cur_rev}, restoring r{a.to_revision}"
                 f" | {a.rationale}", a.actor, a.to_revision))
    audit_id = cur.lastrowid
    con.commit()

    try:
        if "no_adaptive_state = true" in open(snap).read():
            # Restoring the pre-adaptive state: remove the file so the agent
            # returns to bounds defaults exactly as before its first write.
            if os.path.exists(path):
                os.unlink(path)
        else:
            _sh.copy2(snap, path)      # byte-for-byte
            os.chmod(path, 0o644)
    except Exception as e:
        con.execute("UPDATE personality_revisions SET rationale=? WHERE id=?",
                    (f"[APPLY FAILED] {e}", audit_id))
        con.commit(); con.close()
        fail(f"rollback copy failed: {e}")

    con.execute("UPDATE personality_revisions SET applied=1 WHERE id=?", (audit_id,))
    con.commit(); con.close()
    DS.mark_consumed(approval["id"])
    _v, newrev = P.load_adaptive(a.agent)
    print(json.dumps({"ok": True, "rollback": True, "agent": a.agent,
                      "superseded_revision": cur_rev,
                      "restored_revision": newrev,
                      "snapshot": os.path.basename(snap),
                      "audit_id": audit_id}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--to-revision", type=int, dest="to_revision")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--dimension")
    ap.add_argument("--value", type=float)
    ap.add_argument("--hypothesis")
    ap.add_argument("--rationale", required=True)
    ap.add_argument("--actor", required=True)
    ap.add_argument("--confidence", type=float, default=0.0)
    a = ap.parse_args()

    if not valid_agent(a.agent):
        fail(f"invalid agent name {a.agent!r}")

    if a.rollback:
        if a.to_revision is None:
            fail("--rollback requires --to-revision")
        return rollback(a)
    for req in ("dimension", "value", "hypothesis"):
        if getattr(a, req) is None:
            fail(f"--{req} is required for a forward change")

    if a.dimension not in P.DIMENSIONS:
        fail(f"unknown dimension {a.dimension!r}")

    # --- AUTHORITY CHECK: an approved, unconsumed, hash-matching decision ---
    # The proposal database is writable by treebeard/Dream, so its `status`
    # field proves nothing. Authority comes only from the protected store,
    # which treebeard cannot write. Approval is bound to the full-content hash,
    # so a hypothesis edited after approval no longer matches and is refused.
    sys.path.insert(0, "/opt/treebeard/persona")
    import decision_store as DS
    import hypotheses as HY

    hyp = HY.get(a.hypothesis)
    if hyp is None:
        fail(f"no such hypothesis {a.hypothesis!r}")
    approval = DS.current_approval(a.hypothesis)
    if approval is None:
        fail(f"REFUSED: no approved, unconsumed decision for {a.hypothesis!r} "
             f"(proposal status={hyp.get('status')!r} is NOT authority)")
    live_hash = HY.hash_row(hyp)
    if live_hash != approval["content_hash"]:
        fail(f"REFUSED: hypothesis {a.hypothesis!r} was MODIFIED after approval "
             f"(approved {approval['content_hash'][:16]}..., "
             f"now {live_hash[:16]}...)")
    if hyp["agent"] != a.agent or hyp["dimension"] != a.dimension:
        fail(f"REFUSED: request ({a.agent}.{a.dimension}) does not match the "
             f"approved hypothesis ({hyp['agent']}.{hyp['dimension']})")

    bounds = P.load_bounds(a.agent)          # re-read at write time
    if not bounds:
        fail(f"no bounds for {a.agent!r}; personality inert, refusing to write")
    b = bounds.get(a.dimension)
    if b is None:
        fail(f"dimension {a.dimension!r} not bounded for {a.agent!r}")
    if b.get("locked"):
        fail(f"{a.agent}.{a.dimension} is LOCKED by owner bounds")

    vals, rev = P.load_adaptive(a.agent)
    old = vals.get(a.dimension, {}).get("value", b["default"])

    if not (b["min"] <= a.value <= b["max"]):
        fail(f"REJECTED: {a.value} outside owner bounds "
             f"[{b['min']},{b['max']}] for {a.agent}.{a.dimension}")

    rate = b.get("adaptation_rate", 0.05)
    if abs(a.value - old) > rate + 1e-12:
        fail(f"REJECTED: step {abs(a.value-old):.4f} exceeds "
             f"adaptation_rate {rate} for {a.agent}.{a.dimension}")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    vals[a.dimension] = {"value": a.value, "confidence": a.confidence,
                         "updated": now, "evidence": a.hypothesis}
    newrev = rev + 1

    # AUDIT FIRST, then apply. Ordering is deliberate: an audit row describing a
    # change that failed to land is recoverable and visible, whereas a change
    # applied with no audit row is neither. (Observed: two writes landed with a
    # failing audit insert, leaving revisions nobody could account for.)
    import sqlite3
    try:
        con = sqlite3.connect(AUDIT_DB, timeout=5)
        con.execute("""CREATE TABLE IF NOT EXISTS personality_revisions (
            id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, agent TEXT NOT NULL,
            dimension TEXT NOT NULL, old_value REAL, new_value REAL,
            bounds_min REAL, bounds_max REAL, adaptation_rate REAL,
            hypothesis_id TEXT, rationale TEXT, actor TEXT,
            revision_after INTEGER, applied INTEGER DEFAULT 0)""")
        cur = con.execute("""INSERT INTO personality_revisions
            (ts_utc,agent,dimension,old_value,new_value,bounds_min,bounds_max,
             adaptation_rate,hypothesis_id,rationale,actor,revision_after,applied)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (now, a.agent, a.dimension, old, a.value, b["min"], b["max"],
             rate, a.hypothesis, a.rationale, a.actor, newrev))
        audit_id = cur.lastrowid
        con.commit()
    except Exception as e:
        fail(f"audit write failed, change NOT applied: {e}")

    # Version the prior state by copy BEFORE overwriting (box convention).
    # The ABSENT-FILE case is snapshotted too: an agent's first adaptive write
    # supersedes "bounds defaults, no adaptive file", which is a real prior
    # state and must be restorable. Without this marker the first revision of
    # any agent would be permanently irreversible.
    _, path = P._paths(a.agent)
    import shutil
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.v{rev}-{now.replace(':','')}")
    else:
        marker = f"{path}.v{rev}-{now.replace(':','')}"
        with open(marker, "w") as fh:
            fh.write("# NO PRIOR ADAPTIVE STATE\n"
                     "# Agent ran on bounds defaults; no adaptive file existed.\n"
                     "# Rolling back to this revision REMOVES the adaptive file.\n"
                     "no_adaptive_state = true\n")
        os.chmod(marker, 0o644)

    ok, info = P.write_adaptive(a.agent, vals, newrev)
    if not ok:
        con.execute("UPDATE personality_revisions SET rationale=? WHERE id=?",
                    (a.rationale + " [APPLY FAILED]", audit_id))
        con.commit(); con.close()
        fail(f"write failed (audit row {audit_id} marked unapplied): {info}")
    con.execute("UPDATE personality_revisions SET applied=1 WHERE id=?", (audit_id,))
    con.commit(); con.close()
    DS.mark_consumed(approval["id"])   # one approval authorises ONE mutation
    print(json.dumps({"ok": True, "agent": a.agent, "dimension": a.dimension,
                      "old": old, "new": a.value, "revision": newrev}))


if __name__ == "__main__":
    main()
