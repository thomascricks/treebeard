#!/usr/bin/env python3
# -*- coding: ascii -*-
"""meetmem.py -- pending meeting records, delivered for SELF-consolidation.

Meetings are multi-agent; long-term memory is own-only. longterm.py states
the rule: "Writes are ALWAYS own-only -- oversight is read, never reach."
So meet.py does NOT write participants' stores. It ENQUEUES one record per
participant here, and each agent consolidates ITS OWN copy on its next turn
through longterm.consolidate(), the sanctioned logged path. Same
store-and-forward shape as sms.py: recorded now, delivered later, never a
live cross-agent reach.

NO AGENT NAMES IN THIS FILE. Participants arrive as parameters; a new agent
works the day its charter exists.
"""
import json
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")


class MeetMemError(Exception):
    """Enqueue or claim failure. Callers own fail-soft."""


def store_dir():
    return os.environ.get("MEETMEM_ROOT", "%s/shared/meetmem" % _TB_BASE)


def db_path():
    return os.path.join(store_dir(), "meetmem.db")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_REPL = {"\u2014": "--", "\u2013": "-", "\u2018": "'", "\u2019": "'",
         "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2192": "->",
         "\u2190": "<-", "\u00a0": " ", "\u2011": "-", "\u2022": "*",
         "\u2010": "-", "\u00ad": ""}


def to_ascii(s):
    """longterm._ascii_clean RAISES on non-ascii; coerce before it is called
    so one em-dash in an agent reply cannot lose a whole meeting record."""
    s = s or ""
    for k, v in _REPL.items():
        s = s.replace(k, v)
    return (unicodedata.normalize("NFKD", s)
            .encode("ascii", "ignore").decode("ascii"))


SHARED_GROUP = os.environ.get("TREEBEARD_GROUP", "treebeard")


def _fix_perms():
    """The store is written by SEVERAL agents: whoever enqueues first
    creates the db, and every participant must later mark its own record
    done. A db left 0644 owned by its creator is unwritable by the others
    (found live 2026-08-30: the store landed treebeard:treebeard 644 and
    had to be corrected by hand). Best-effort and silent: a non-root
    process can chmod what it owns and cannot chown, so root fixes the
    rest via init_db. SHARED_GROUP is the system group, not an agent."""
    try:
        os.chmod(store_dir(), 0o2770)
    except OSError:
        pass
    try:
        os.chmod(db_path(), 0o660)
    except OSError:
        pass
    if os.geteuid() == 0:
        try:
            import grp  # noqa: PLC0415
            gid = grp.getgrnam(SHARED_GROUP).gr_gid
            os.chown(store_dir(), 0, gid)
            os.chown(db_path(), 0, gid)
        except (OSError, KeyError):
            pass


def init_db():
    """Idempotent init for the installer: create the store and enforce the
    permission pattern as root."""
    connect().close()
    _fix_perms()


EMBED_SAFE = 2800   # dn_write's figure: the embedder 500s near ~28k chars


def chunk(text, size=EMBED_SAFE):
    """Split a long meeting for embedding. A 5-round meeting is ~17k chars
    and the embedder returns HTTP 500 on a body that size (found live
    2026-08-30: both copies of the longest meeting FAILED silently while
    every shorter one succeeded). Same guard dn_write has used since June;
    it should have been carried over when this module was written."""
    import textwrap
    if len(text) <= size:
        return [text]
    return textwrap.wrap(text, size, break_long_words=False,
                         replace_whitespace=False)


def connect():
    d = store_dir()
    os.makedirs(d, exist_ok=True)
    _fresh = not os.path.exists(db_path())
    con = sqlite3.connect(db_path())
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS pending("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_utc TEXT NOT NULL,"
        "  agent TEXT NOT NULL,"
        "  stamp TEXT NOT NULL,"
        "  participants TEXT NOT NULL,"
        "  topic TEXT NOT NULL,"
        "  body TEXT NOT NULL,"
        "  kind TEXT NOT NULL DEFAULT 'transcript',"
        "  state TEXT NOT NULL DEFAULT 'PENDING')")
    cols = [r[1] for r in con.execute("PRAGMA table_info(pending)")]
    if "kind" not in cols:      # pre-2026-08-31 store
        con.execute("ALTER TABLE pending ADD COLUMN kind TEXT "
                    "NOT NULL DEFAULT 'transcript'")
    con.execute("CREATE INDEX IF NOT EXISTS ix_pending_agent "
                "ON pending(agent, state)")
    if _fresh:
        _fix_perms()
    return con


def enqueue(participants, topic, transcript, stamp=None, decision=None):
    """One pending record per participant, plus a DECISION record when the
    meeting reached one. Returns the ids written.

    Writes NOTHING to any long-term store: this is a queue, and each agent
    consolidates its own copy later. Raises on programmer error; meet.py
    owns fail-soft so a queue failure never costs the transcript file.

    Why the decision is a SEPARATE record (2026-08-31): after a 5-round
    contested meeting, BOTH participants later reported the decision as the
    inverse of what was agreed. Storage was correct; recall was not. A
    conclusion is one paragraph and the losing argument is thousands of
    words, so semantic recall returns what is most represented rather than
    what was decided. A short record with its own embedding and its own
    source_kind competes on its own terms. Uncontested meetings recalled
    correctly, so the failure is specific to meetings with real dissent --
    which are exactly the meetings whose conclusions matter."""
    parts = [str(a).strip().lower() for a in (participants or []) if str(a).strip()]
    if not parts:
        raise MeetMemError("no participants")
    stamp = stamp or utcnow()
    topic_a = to_ascii(topic)
    lines = ["MEETING %s" % stamp,
             "participants: %s" % ", ".join(parts),
             "topic: %s" % topic_a, ""]
    for who, text in (transcript or []):
        lines.append("[%s]:" % to_ascii(str(who)))
        lines.append(to_ascii(str(text)))
        lines.append("")
    if not transcript:
        raise MeetMemError("empty transcript")
    body = "\n".join(lines).strip()
    if not body:
        raise MeetMemError("empty body")
    dec = to_ascii(decision or "").strip()
    dec_body = ""
    if dec:
        dec_body = "\n".join([
            "MEETING DECISION %s" % stamp,
            "participants: %s" % ", ".join(parts),
            "topic: %s" % topic_a,
            "",
            "DECIDED: %s" % dec,
        ])
    ids = []
    con = connect()
    with con:
        for a in parts:
            cur = con.execute(
                "INSERT INTO pending(ts_utc, agent, stamp, participants, "
                "topic, body) VALUES(?,?,?,?,?,?)",
                (utcnow(), a, stamp, ",".join(parts), topic_a, body))
            ids.append(cur.lastrowid)
            if dec_body:
                cur = con.execute(
                    "INSERT INTO pending(ts_utc, agent, stamp, participants, "
                    "topic, body, kind) VALUES(?,?,?,?,?,?,'decision')",
                    (utcnow(), a, stamp, ",".join(parts), topic_a, dec_body))
                ids.append(cur.lastrowid)
    con.close()
    return ids


def claim(agent, limit=3):
    """Pending records for ONE agent. Read-only; caller marks done."""
    agent = (agent or "").strip().lower()
    if not agent:
        raise MeetMemError("agent required")
    con = connect()
    rows = con.execute(
        "SELECT id, stamp, participants, topic, body, kind FROM pending "
        "WHERE agent=? AND state='PENDING' ORDER BY id LIMIT ?",
        (agent, limit)).fetchall()
    con.close()
    return [{"id": r[0], "stamp": r[1], "participants": r[2],
             "topic": r[3], "body": r[4], "kind": r[5]} for r in rows]


def mark(rec_id, state):
    if state not in ("DONE", "FAILED"):
        raise MeetMemError("invalid state %r" % (state,))
    con = connect()
    with con:
        con.execute("UPDATE pending SET state=? WHERE id=?", (state, rec_id))
    con.close()


def consolidate_pending(agent, longterm_mod=None, limit=3):
    """Each agent consolidates ITS OWN pending meeting records.

    Called from the agent's own process, so the own-only invariant holds:
    the only store touched is the caller's. Uses consolidate(), the
    sanctioned logged path, with meeting provenance so a recalled memory
    is always identifiable as meeting-origin rather than self-authored.
    Returns (written, failed). Never raises.
    """
    written = failed = 0
    try:
        if longterm_mod is None:
            sys.path.insert(0, "%s/shared/lib" % _TB_BASE)
            import longterm as longterm_mod  # noqa: PLC0415
        for rec in claim(agent, limit):
            # A decision is short and never chunked: chunking is what
            # buried it among transcript fragments in the first place.
            _dec = rec.get("kind") == "decision"
            base = "%s:%s:%s" % ("meeting-decision" if _dec else "meeting",
                                 rec["stamp"], rec["participants"])
            _skind = "meeting_decision" if _dec else "meeting"
            pieces = [rec["body"]] if _dec else chunk(rec["body"])
            ok = True
            for i, piece in enumerate(pieces, 1):
                prov = (base if len(pieces) == 1
                        else "%s:part%d/%d" % (base, i, len(pieces)))
                try:
                    res = longterm_mod.consolidate(
                        agent, piece, prov, _skind,
                        metadata={"topic": rec["topic"][:200]})
                    if not getattr(res, "ok", False):
                        ok = False
                except Exception:
                    ok = False
            mark(rec["id"], "DONE" if ok else "FAILED")
            if ok:
                written += 1
            else:
                failed += 1
    except Exception as e:
        sys.stderr.write("[meetmem] pending consolidation unavailable (%s)\n"
                         % (e,))
    return written, failed


# --------------------------------------------------------------- selftest
def _selftest():
    """Offline. Temp store, fake longterm module, no real memory touched."""
    import tempfile, shutil
    passed = failed = 0

    def ok(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print("  OK   %s" % label)
        else:
            failed += 1
            print("  FAIL %s" % label)

    tmp = tempfile.mkdtemp(prefix="meetmem-selftest-")
    old = os.environ.get("MEETMEM_ROOT")
    os.environ["MEETMEM_ROOT"] = os.path.join(tmp, "store")

    class FakeLT(object):
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail

        def consolidate(self, agent, text, provenance, source_kind,
                        metadata=None):
            self.calls.append((agent, text, provenance, source_kind))
            class R(object):
                pass
            r = R()
            r.ok = not self.fail
            return r

    try:
        parts = ["alpha", "beta", "gamma"]
        tx = [("ALPHA", "first point"), ("BETA", "reply with em-dash \u2014 here")]
        ids = enqueue(parts, "a topic", tx)
        ok("m1 one record per participant", len(ids) == 3)
        ok("m2 each participant has exactly one pending",
           all(len(claim(a)) == 1 for a in parts))
        ok("m3 nothing pending for a non-participant", claim("delta") == [])

        rec = claim("alpha")[0]
        ok("m4 body carries the whole meeting",
           "first point" in rec["body"] and "reply with" in rec["body"])
        ok("m5 non-ascii coerced before longterm sees it",
           "--" in rec["body"] and "\u2014" not in rec["body"])
        ok("m6 participants recorded", rec["participants"] == "alpha,beta,gamma")

        # THE INVARIANT: consolidation touches ONLY the calling agent's store.
        lt = FakeLT()
        w, f = consolidate_pending("alpha", longterm_mod=lt)
        ok("m7 alpha consolidated one record", (w, f) == (1, 0))
        ok("m8 own-only: the only store written was alpha's",
           len(lt.calls) == 1 and lt.calls[0][0] == "alpha")
        ok("m9 provenance marks meeting origin",
           lt.calls[0][2].startswith("meeting:") and lt.calls[0][3] == "meeting")
        ok("m10 consolidated record leaves the queue", claim("alpha") == [])
        ok("m11 other participants still pending",
           len(claim("beta")) == 1 and len(claim("gamma")) == 1)

        # failure path: record marked FAILED, never silently dropped or retried
        ltf = FakeLT(fail=True)
        w2, f2 = consolidate_pending("beta", longterm_mod=ltf)
        ok("m12 failed consolidation reported", (w2, f2) == (0, 1))
        ok("m13 failed record does not re-present", claim("beta") == [])

        # never raises, even with a broken module
        class Broken(object):
            def consolidate(self, *a, **k):
                raise RuntimeError("store down")
        w3, f3 = consolidate_pending("gamma", longterm_mod=Broken())
        ok("m14 broken store does not raise", (w3, f3) == (0, 1))

        # empty and malformed input
        try:
            enqueue([], "t", tx)
            ok("m15 no participants refused", False)
        except MeetMemError:
            ok("m15 no participants refused", True)
        try:
            enqueue(["alpha"], "t", [])
            ok("m16 empty transcript refused", False)
        except MeetMemError:
            ok("m16 empty transcript refused", True)

        # no agent name is hardcoded: an unheard-of agent works immediately
        ids2 = enqueue(["zzznewagent"], "wizard test", [("ZZZNEWAGENT", "hi")])
        # m18-m21: the decision record (2026-08-31).
        ids3 = enqueue(["alpha"], "decided topic",
                       [("ALPHA", "long argument " * 200)],
                       decision="we chose the flat list")
        ok("m18 decision adds a second record per participant", len(ids3) == 2)
        recs = claim("alpha", limit=5)
        kinds = sorted(r["kind"] for r in recs)
        ok("m19 one transcript and one decision", kinds == ["decision", "transcript"])
        _d = [r for r in recs if r["kind"] == "decision"][0]
        ok("m20 decision body is short and states the decision",
           "DECIDED: we chose the flat list" in _d["body"]
           and len(_d["body"]) < 400)
        lt3 = FakeLT()
        consolidate_pending("alpha", longterm_mod=lt3, limit=5)
        skinds = sorted(set(c[3] for c in lt3.calls))
        ok("m21 decision consolidates under its own source_kind",
           skinds == ["meeting", "meeting_decision"])
        ok("m22 decision is never chunked",
           len([c for c in lt3.calls if c[3] == "meeting_decision"]) == 1)

        ok("m17 a brand-new agent needs no code change",
           len(ids2) == 1 and len(claim("zzznewagent")) == 1)
    finally:
        if old is None:
            os.environ.pop("MEETMEM_ROOT", None)
        else:
            os.environ["MEETMEM_ROOT"] = old
        shutil.rmtree(tmp, ignore_errors=True)

    print("meetmem selftests: %d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    print("usage: meetmem.py --selftest", file=sys.stderr)
    sys.exit(2)
