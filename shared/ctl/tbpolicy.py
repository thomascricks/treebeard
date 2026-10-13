"""tbpolicy.py -- root-owned control-plane policy loader for the tbctl helpers.

Mirrors dispatch.load_ops semantics EXACTLY (the approved reference pattern):
fresh-read every call, FAIL-CLOSED in every failure mode (missing, unreadable,
empty, malformed), and ONE malformed non-comment line voids the ENTIRE load.
Policy interpretation lives here and nowhere else; callers only ask permitted().
"""
import os

class PolicyError(Exception):
    pass

def load_ops(path):
    """Return the set of permitted operation tokens from a root-owned policy
    file. Syntax: one bare operation token per non-comment line. FAIL-CLOSED."""
    try:
        with open(path, encoding="ascii") as fh:
            raw = fh.read()
    except OSError as e:
        raise PolicyError("policy unreadable at %s (%s)" % (path, e))
    ops = set()
    for n, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tok = s
        if not tok or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for c in tok):
            raise PolicyError("malformed policy line %d in %s" % (n, path))
        ops.add(tok)
    if not ops:
        raise PolicyError("policy at %s permits nothing" % path)
    return ops

def permitted(operation, path):
    """True iff operation is allowed by live policy. Returns False on EVERY
    failure mode including PolicyError -- fail-closed, exactly like dispatch."""
    try:
        return operation in load_ops(path)
    except PolicyError:
        return False
