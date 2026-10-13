"""lock_writer.py -- the ONLY authorised writer of acceptance LOCK.sha256 (tbctl).

Wraps s3_verify_harness.cmd_lock semantics: lock-once (refuse if already
locked), require test_*.py present. Integrity lock is written by an identity
OUTSIDE the adversarial treebeard uid, closing the writer==adversary defect.
"""
import sys, os, json, hashlib

BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
CTL = os.path.join(BASE, "shared", "ctl")
LIB = os.path.join(BASE, "shared", "lib")
MEE = os.path.join(BASE, "shared", "meeseeks")
POLICY = os.path.join("/etc/treebeard", "lock_ops.conf")
RECEIPTS = os.path.join(CTL, "receipts", "lock.receipts")
sys.path.insert(0, CTL)
import tbpolicy, tbreceipt
OPS = {"lock"}

def _fail(op, dec, argv, msg, code=2):
    try:
        tbreceipt.append(RECEIPTS, op=op, decision=dec, caller_uid=os.getuid(),
                         args_hash=hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                         detail=msg)
    except Exception: pass
    sys.stderr.write("lock_writer: %s\n" % msg); sys.exit(code)

def main(argv):
    if not argv or argv[0] not in OPS:
        _fail(argv[0] if argv else "", "denied", argv, "unknown/absent operation")
    op = argv[0]
    try:
        allowed = tbpolicy.load_ops(POLICY)
    except tbpolicy.PolicyError as e:
        _fail(op, "policy-error", argv, "policy: %s" % e)
    if op not in allowed:
        _fail(op, "denied", argv, "operation not in policy")
    if len(argv) != 2:
        _fail(op, "denied", argv, "lock needs <project>")
    sys.path.insert(0, MEE)
    import s3_verify_harness as vh, argparse
    try:
        vh.cmd_lock(argparse.Namespace(project=argv[1]))   # refuses re-lock, requires test_*.py
    except SystemExit:
        raise
    except Exception as e:
        _fail(op, "denied", argv, "lock refused: %s" % e)
    tbreceipt.append(RECEIPTS, op=op, decision="accepted", caller_uid=os.getuid(),
                     args_hash=hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                     detail="locked %s" % argv[1])
    sys.exit(0)

if __name__ == "__main__":
    main(sys.argv[1:])
