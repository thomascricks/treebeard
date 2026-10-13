#!/usr/bin/env python3
"""persona_approve.py - records an approval in the PROTECTED decision store.

Runs as tbpersona via its own narrow sudoers rule. Deliberately SEPARATE from
persona_writer.py: approval and mutation are distinct operations, so the chain
is hypothesis -> approve -> protected record -> write -> ONE bounded mutation.

treebeard cannot write decisions.db directly (verified: "attempt to write a
readonly database"), so this entry point is the only route to an approval, and
it validates before recording.

KNOWN LIMITATION, unresolved: Dream currently runs as the treebeard UID and can
therefore invoke this same command. The OS boundary blocks Moon and blocks
direct file writes; it does NOT distinguish Dream from Treebeard. That closes
only when Dream gets its own UID.

Owner control does not depend on this boundary: bounds, locked dimensions and
adaptation_rate are enforced by the writer at mutation time, re-read from
root-owned files that neither treebeard nor tbpersona can modify.
"""
import argparse, json, os, sys
sys.path.insert(0, "/opt/treebeard/shared/lib")
sys.path.insert(0, "/opt/treebeard/persona")
import hypotheses as HY
import decision_store as DS


def fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hypothesis")
    ap.add_argument("--rollback-target", dest="rollback_target",
                    help="rollback:<agent>:<revision>")
    ap.add_argument("--decision", default="approved",
                    choices=["approved", "rejected", "revoked"])
    # NO --approver flag: identity is derived from authenticated sudo
    # provenance below. A caller-supplied approver would let the treebeard
    # UID record "--approver <operator>" and the protected store would
    # faithfully preserve a false actor. The audit record must be harder to
    # lie to than the agent generating it.
    ap.add_argument("--rationale", required=True)
    ap.add_argument("--intended-value", type=float, dest="intended_value")
    ap.add_argument("--allow-synthetic", action="store_true",
                    help="acceptance-test mode: permit synthetic hypotheses")
    a = ap.parse_args()

    approver = os.environ.get("SUDO_USER")
    if not approver:
        fail("REFUSED: no SUDO_USER; approval must arrive through the "
             "authenticated sudo path so provenance is provable")

    # Rollback targets are not hypotheses: no claim, nothing to hash.
    if a.rollback_target:
        # A syntactically valid target string is NOT authorization. Bind the
        # exact agent + revision, and prove that revision actually exists for
        # that agent before recording anything.
        parts = a.rollback_target.split(":")
        if len(parts) != 3 or parts[0] != "rollback" or not parts[2].isdigit():
            fail("--rollback-target must be rollback:<agent>:<revision>")
        r_agent, r_rev = parts[1], int(parts[2])
        import personality as PP
        import glob
        if PP.load_bounds(r_agent) is None:
            fail(f"REFUSED: {r_agent!r} has no bounds; nothing to roll back")
        _bp, _ap = PP._paths(r_agent)
        if not glob.glob(f"{_ap}.v{r_rev}-*"):
            fail(f"REFUSED: no snapshot of {r_agent!r} at revision {r_rev}")
        canonical = f"rollback:{r_agent}:{r_rev}"
        did = DS.decide(canonical, canonical, a.decision, approver,
                        a.rationale, synthetic=1 if a.allow_synthetic else 0)
        print(json.dumps({"ok": True, "decision_id": did,
                          "target": a.rollback_target, "decision": a.decision}))
        return

    if not a.hypothesis:
        fail("--hypothesis or --rollback-target required")
    hyp = HY.get(a.hypothesis)
    if hyp is None:
        fail(f"no such hypothesis {a.hypothesis!r}")
    if hyp["synthetic"] and not a.allow_synthetic:
        fail(f"{a.hypothesis!r} is SYNTHETIC; refuse unless --allow-synthetic")

    # Recompute the full substantive hash and compare to what was stored at
    # proposal time. A mismatch means the claim was edited after proposal.
    live = HY.hash_row(hyp)
    if live != hyp["content_hash"]:
        fail(f"REFUSED: hypothesis {a.hypothesis!r} has been TAMPERED "
             f"(stored {hyp['content_hash'][:16]}..., recomputed {live[:16]}...)")

    if DS.current_approval(a.hypothesis) is not None:
        fail(f"REFUSED: {a.hypothesis!r} already has an unconsumed approval")

    did = DS.decide(a.hypothesis, live, a.decision, approver,
                    f"{a.rationale} | intended_value={a.intended_value}",
                    synthetic=hyp["synthetic"])
    print(json.dumps({"ok": True, "decision_id": did,
                      "hypothesis": a.hypothesis, "content_hash": live[:16],
                      "decision": a.decision, "approver": approver,
                      "single_use": True}))


if __name__ == "__main__":
    main()
