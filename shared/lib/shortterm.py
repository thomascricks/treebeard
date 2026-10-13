#!/usr/bin/env python3
"""shortterm.py — verbatim capture of every ptt exchange, day-bucketed.

The raw short-term layer: "all ptts and results for a day." Append-only JSONL,
one file per UTC day. Local filesystem only — no egress, no embedding, no gate.
Capture and recall both fail OPEN: a memory error can never break a ptt.

Substrate only. Semantic indexing of short-term, and the medium/long
consolidation tiers, are a separate later build — not done here.
"""
import os
import json
import glob
import datetime
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

STORE_DIR = os.environ.get(
    "TREEBEARD_SHORTTERM_DIR", f"{_TB_BASE}/shared/memory/shortterm")


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _day_path(when=None):
    day = (when or _utcnow()).strftime("%Y-%m-%d")
    return os.path.join(STORE_DIR, day + ".jsonl")


def capture(agent, prompt, response, *, tier=None, model=None,
            thread_id=None, turn_id=None, parent_turn_id=None, source=None,
            mission_id=None, origin=None):
    """Append one exchange to today's log.

    mission_id (A+ ROUTER 1b) is provenance: the mission selected for the
    turn that produced this exchange, or None. Read by the selector
    tie-break; never authority on its own.

    thread_id is conversational identity. Agent identifies who answered
    and source identifies how the turn entered the system; neither
    substitutes for thread identity.

    Metadata is optional for backward compatibility. Legacy rows without
    thread_id remain readable archive records but are not authoritative
    thread history.
    """
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        row = {
            "ts": _utcnow().isoformat(),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "parent_turn_id": parent_turn_id,
            "source": source,
            "agent": agent,
            "prompt": prompt,
            "response": response,
            "tier": tier,
            "model": model,
            "mission_id": mission_id,
            "origin": origin,
        }
        with open(_day_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def recent(agent=None, n=12, days=1):
    """Most-recent exchanges (oldest-first) across the last `days` day-files,
    optionally filtered to one agent. [] on any failure — never raises."""
    try:
        files = sorted(glob.glob(os.path.join(STORE_DIR, "*.jsonl")))[-days:]
        rows = []
        for fp in files:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if agent and r.get("agent") != agent:
                        continue
                    rows.append(r)
        return rows[-n:]
    except Exception:
        return []



def recent_thread(thread_id, n=12, days=None):
    """Return authoritative history for exactly one conversation thread.

    Legacy rows without thread_id are excluded. There is deliberately no
    fallback to agent-wide history.
    """
    if not thread_id:
        return []
    try:
        files = sorted(glob.glob(os.path.join(STORE_DIR, "*.jsonl")))
        if days is not None:
            files = files[-max(1, int(days)):]
        rows = []
        for fp in files:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("thread_id") != thread_id:
                        continue
                    rows.append(r)
        return rows[-n:]
    except Exception:
        return []

def _selftest():
    if not __debug__:
        # Assertions are stripped under -O: this selftest would verify
        # nothing, still exit 0, and be recorded as PASS. Fail closed.
        import sys as _sys
        print("shortterm: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    import tempfile
    global STORE_DIR
    STORE_DIR = tempfile.mkdtemp(prefix="shortterm_selftest_")

    # Legacy/archive capture remains supported.
    assert capture("treebeard", "legacy treebeard", "legacy response",
                   tier="free", model="m"), "legacy capture failed"
    assert capture("clark", "legacy clark", "other response"), \
        "legacy capture failed"

    # Two independent threads owned by the same agent.
    assert capture("treebeard", "alpha one", "a1",
                   thread_id="thread-a", turn_id="ta1",
                   source="web"), "thread-a capture failed"
    assert capture("treebeard", "beta one", "b1",
                   thread_id="thread-b", turn_id="tb1",
                   source="cli"), "thread-b capture failed"

    # Thread identity is independent of agent identity.
    assert capture("clark", "alpha two", "a2",
                   thread_id="thread-a", turn_id="ta2",
                   parent_turn_id="ta1", source="web"), \
        "cross-agent thread capture failed"

    # Existing archive/diagnostic API still behaves as before.
    mine = recent(agent="treebeard", n=10)
    assert len(mine) == 3, "agent-filtered archive recall failed"

    # Authoritative conversational recall is exact-thread only.
    ta = recent_thread("thread-a", n=10)
    assert [r["prompt"] for r in ta] == ["alpha one", "alpha two"], \
        "thread-a exact recall failed"
    assert [r["agent"] for r in ta] == ["treebeard", "clark"], \
        "thread identity incorrectly tied to agent"

    tb = recent_thread("thread-b", n=10)
    assert len(tb) == 1 and tb[0]["prompt"] == "beta one", \
        "thread-b exact recall failed"

    # This is the P0 invariant: unrelated history must never leak in.
    assert recent_thread("new-thread", n=10) == [], \
        "new thread fell back to unrelated history"
    assert recent_thread(None, n=10) == [], \
        "missing thread id returned shared history"

    # Old rows remain archive data but never become thread history.
    allrows = recent(n=10)
    legacy = [r for r in allrows if not r.get("thread_id")]
    assert len(legacy) == 2, "legacy archive rows lost or misclassified"
    assert not any(r["prompt"].startswith("legacy")
                   for r in recent_thread("thread-a", n=10)), \
        "legacy row contaminated authoritative thread history"

    # Mission provenance stamp (A+ ROUTER 1b): additive, None when absent.
    assert capture("treebeard", "gamma one", "g1",
                   thread_id="thread-g", turn_id="tg1",
                   source="web", mission_id="m-stamp"), "stamped capture failed"
    tg = recent_thread("thread-g", n=10)
    assert len(tg) == 1 and tg[0].get("mission_id") == "m-stamp", \
        "mission stamp did not round-trip"
    assert all(r.get("mission_id") is None
               for r in recent_thread("thread-a", n=10)), \
        "unstamped rows must carry no mission"

    # Origin stamp (governance hardening chunk 1): who started the turn.
    assert capture("treebeard", "delta one", "d1",
                   thread_id="thread-d", turn_id="td1",
                   source="cli", origin="automation:gh1-test"), \
        "origin capture failed"
    td = recent_thread("thread-d", n=10)
    assert len(td) == 1 and td[0].get("origin") == "automation:gh1-test", \
        "origin stamp did not round-trip"
    assert all(r.get("origin") is None
               for r in recent_thread("thread-a", n=10)), \
        "rows captured without origin must carry none"

    print("shortterm.py selftest OK "
          "(archive, exact-thread, cross-agent, legacy-isolation, "
          "mission-stamp, origin-stamp)")


if __name__ == "__main__":
    _selftest()
