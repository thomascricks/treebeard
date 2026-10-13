"""mission_writer.py -- the ONLY authorised writer of missions.db (runs as tbctl).

Same pattern as board_writer. missions.py already forbids agent self-transition
and owns the ALLOWED transition table; this front door adds the root-owned
policy allow-list + arg validation + receipts. init_db stays a root operation
and is NOT exposed here.
"""
import sys, os, json, hashlib

BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
CTL = os.path.join(BASE, "shared", "ctl")
LIB = os.path.join(BASE, "shared", "lib")
POLICY = os.path.join("/etc/treebeard", "mission_ops.conf")
RECEIPTS = os.path.join(CTL, "receipts", "mission.receipts")

sys.path.insert(0, CTL)
import tbpolicy, tbreceipt

OPS = {"create", "assign", "transition"}

def _args_hash(argv):
    return hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()

def _fail(op, decision, argv, msg, code=2):
    try:
        tbreceipt.append(RECEIPTS, op=op, decision=decision, caller_uid=os.getuid(),
                         args_hash=_args_hash(argv), detail=msg)
    except Exception:
        pass
    sys.stderr.write("mission_writer: %s\n" % msg)
    sys.exit(code)

def main(argv):
    if not argv:
        _fail("", "denied", argv, "no operation")
    op, rest = argv[0], argv[1:]
    if op not in OPS:
        _fail(op, "denied", argv, "unknown operation %r" % op)
    try:
        allowed = tbpolicy.load_ops(POLICY)
    except tbpolicy.PolicyError as e:
        _fail(op, "policy-error", argv, "policy: %s" % e)
    if op not in allowed:
        _fail(op, "denied", argv, "operation %r not in policy" % op)
    sys.path.insert(0, LIB)
    import missions
    try:
        if op == "create":
            # create <title> <objective> [--dod TEXT] [--agent NAME]
            if len(rest) < 2: _fail(op, "denied", argv, "create needs <title> <objective>")
            title, objective = rest[0], rest[1]
            dod = None; agent = None
            it = iter(rest[2:])
            for a in it:
                if a == "--dod": dod = next(it)
                elif a == "--agent": agent = next(it)
                else: _fail(op, "denied", argv, "bad create arg %r" % a)
            mid = missions.create_mission(title, objective, dod=dod, agent=agent)
            detail = "created %s" % mid
        elif op == "assign":
            if len(rest) != 2: _fail(op, "denied", argv, "assign needs <mission_id> <agent>")
            missions.assign(rest[0], rest[1], actor="operator")   # DRAFT-only enforced inside
            detail = "assigned %s->%s" % (rest[0], rest[1])
        elif op == "transition":
            if len(rest) < 2: _fail(op, "denied", argv, "transition needs <mission_id> <status>")
            reason = rest[2] if len(rest) > 2 else None
            missions.transition(rest[0], rest[1], actor="operator", reason=reason)  # ALLOWED[] enforced inside
            detail = "transition %s->%s" % (rest[0], rest[1])
    except SystemExit:
        raise
    except Exception as e:
        _fail(op, "denied", argv, "domain refused: %s" % e)
    tbreceipt.append(RECEIPTS, op=op, decision="accepted", caller_uid=os.getuid(),
                     args_hash=_args_hash(argv), detail=detail)
    sys.exit(0)

if __name__ == "__main__":
    main(sys.argv[1:])
