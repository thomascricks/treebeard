"""tbreceipt.py -- append-only, hash-chained control/integrity receipts.

The chain makes silent deletion(mid)/reordering/mutation detectable by any
reader; it does NOT authenticate the writer (that comes from the store being
tbctl-owned and unwritable by treebeard/tbjail). KNOWN LIMIT: a tail-trim of
the most recent records leaves the prefix self-consistent -- prefix integrity
and ordering, not append-completeness. "recorded", never "authenticated".

CONTAINMENT T2: append() also takes `identity` (the effective execution
identity, the derived invoker, the authority class -- "recorded" in T2 --
and the recorded actor) and `result` (card, op, from, to, rev). Both are optional and part of the
hashed payload; records without them are unchanged, so existing chains and
the mission/lock writers are unaffected.
"""
import json, hashlib, os, time

GENESIS = "0" * 64

def _digest(prev_hash, payload_json):
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii")); h.update(b"\n")
    h.update(payload_json.encode("utf-8"))
    return h.hexdigest()

def _last_hash(path):
    if not os.path.exists(path):
        return GENESIS
    last = GENESIS
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            last = json.loads(line)["hash"]
    return last

def append(path, *, op, decision, caller_uid, args_hash, before_hash=None,
           after_hash=None, detail="", identity=None, result=None):
    prev = _last_hash(path)
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "op": op, "decision": decision, "caller_uid": caller_uid,
               "args_hash": args_hash, "before": before_hash,
               "after": after_hash, "detail": detail[:500], "prev": prev}
    if identity is not None:
        payload["identity"] = dict(identity)
    if result is not None:
        payload["result"] = dict(result)
    payload_json = json.dumps(payload, sort_keys=True)
    rec_hash = _digest(prev, payload_json)
    rec = dict(payload); rec["hash"] = rec_hash
    line = json.dumps(rec, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return rec_hash

def verify_chain(path):
    """Recompute the whole chain. (ok, count, first_bad_index_or_None).
    Detects deletion(mid)/reordering/in-place mutation of the prefix."""
    if not os.path.exists(path):
        return True, 0, None
    prev = GENESIS; n = 0
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            n += 1
            rec = json.loads(line); stored = rec.pop("hash")
            if rec.get("prev") != prev:
                return False, n, i
            if _digest(prev, json.dumps(rec, sort_keys=True)) != stored:
                return False, n, i
            prev = stored
    return True, n, None
