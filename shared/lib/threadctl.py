#!/usr/bin/env python3
"""threadctl.py — non-destructive controls over conversational context.

Raw short-term records remain append-only evidence.  This sidecar records
operator decisions about whether an otherwise valid turn may be supplied
back to a model as authoritative conversational context.

This is intentionally separate from transport, agent identity, and storage.
"""
import json
import os
import tempfile
import datetime

_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
CONTROL_PATH = os.environ.get(
    "TREEBEARD_THREAD_CONTROL",
    f"{_TB_BASE}/shared/memory/thread_control.json",
)


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load():
    try:
        with open(CONTROL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Context-control failure must never fabricate an exclusion.
        return {}


def context_excluded(turn_id):
    """True only when this exact turn_id is explicitly excluded."""
    if not turn_id:
        return False
    data = _load()
    rows = data.get("context_exclusions") or {}
    return str(turn_id) in rows


def exclusion(turn_id):
    """Return exclusion metadata for one turn, or None."""
    if not turn_id:
        return None
    return (_load().get("context_exclusions") or {}).get(str(turn_id))


def exclude(turn_id, reason, *, by="operator"):
    """Non-destructively exclude one turn from future model context."""
    turn_id = str(turn_id or "").strip()
    if not turn_id:
        raise ValueError("turn_id required")

    data = _load()
    rows = data.setdefault("context_exclusions", {})
    rows[turn_id] = {
        "reason": str(reason or "").strip(),
        "by": str(by or "operator"),
        "ts": _utcnow(),
    }

    parent = os.path.dirname(CONTROL_PATH)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".thread_control.", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, CONTROL_PATH)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return rows[turn_id]


def _selftest():
    if not __debug__:
        # Assertions are stripped under -O: this selftest would verify
        # nothing, still exit 0, and be recorded as PASS. Fail closed.
        import sys as _sys
        print("threadctl: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    global CONTROL_PATH
    with tempfile.TemporaryDirectory(prefix="threadctl_selftest_") as d:
        CONTROL_PATH = os.path.join(d, "control.json")

        assert not context_excluded("t1")
        row = exclude("t1", "known bad context")
        assert row["reason"] == "known bad context"
        assert context_excluded("t1")
        assert not context_excluded("t2")
        assert exclusion("t1")["by"] == "operator"

    print("threadctl.py selftest OK (non-destructive exact-turn exclusion)")


if __name__ == "__main__":
    _selftest()
