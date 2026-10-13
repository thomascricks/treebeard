#!/usr/bin/env python3
# -*- coding: ascii -*-
"""sms.py -- governed inter-agent store-and-forward messaging (SMS-b).

Design ruled by Treebeard (SMS-a 2026-08-23), Tex-reviewed, PO-approved,
with the configuration-policy correction: THIS CODE KNOWS HOW TO ENFORCE
COMMUNICATION POLICY; IT DOES NOT KNOW WHICH AGENTS MAY COMMUNICATE. No
agent name appears in this file. Policy lives in a root-owned governance
file (default /etc/treebeard/sms_pairs.conf), directed-pair syntax
'sender -> recipient', fresh-read at every send, FAIL-CLOSED: a missing,
unreadable, empty, or malformed policy allows nothing, and one malformed
non-comment line voids the ENTIRE load (a typo must not silently yield a
partially different matrix). Policy failure audits as 'policy-error',
distinct from an ordinary 'denied' pair.

Store-and-forward only; SMS never wakes or invokes anyone. All messages
operator-readable. This module RAISES honestly on failure -- fail-soft
belongs to the caller (ask.py catches and routes to the operational
audit path). Delivery: PENDING -> PRESENTED(turn) -> DELIVERED on
successful turn completion; stale PRESENTED reclaims to PENDING
(at-least-once, stable store-assigned ids). Reclaim touches only
messages presented longer ago than SMS_STALE_S -- a slow but live
recipient turn is never concurrently re-presented.

Caps are OPERATOR configuration (process environment; agents cannot
reach it): SMS_BODY_MAX=1024, SMS_PRESENT_MAX=5, SMS_STALE_S=900.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")


# 2026-08-31 (RC4): default derived from TREEBEARD_BASE, matching
# meetmem.py. A hardcoded default silently escapes a relocated
# install: install.sh initdb would create the store under the
# default root while the relocated tree got none.
def store_dir():
    return os.environ.get("SMS_ROOT", "%s/shared/sms" % _TB_BASE)


def db_path():
    return os.path.join(store_dir(), "sms.db")


def pairs_path():
    return os.environ.get("SMS_PAIRS_CONF", "/etc/treebeard/sms_pairs.conf")


def body_max():
    return max(1, _env_int("SMS_BODY_MAX", 1024))


def present_max():
    return max(1, _env_int("SMS_PRESENT_MAX", 5))


def stale_s():
    return max(60, _env_int("SMS_STALE_S", 900))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class SmsError(RuntimeError):
    pass


class PolicyError(SmsError):
    """Policy file missing/unreadable/empty/malformed: fail closed."""


_PAIR_RE = re.compile(
    r"^([a-z][a-z0-9_-]{0,31})[ \t]*->[ \t]*([a-z][a-z0-9_-]{0,31})$")


def load_pairs():
    """Fresh-read the directed-pair policy. Raises PolicyError on any
    defect; one malformed non-comment line voids the whole load."""
    try:
        with open(pairs_path(), "r", encoding="ascii") as fh:
            raw = fh.read()
    except OSError as exc:
        raise PolicyError("policy unreadable: %s" % exc)
    pairs = set()
    for n, line in enumerate(raw.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _PAIR_RE.match(s)
        if not m:
            raise PolicyError("malformed policy line %d: %r "
                              "(entire policy voided)" % (n, s[:60]))
        if m.group(1) == m.group(2):
            raise PolicyError("self-pair at line %d voids policy" % n)
        pairs.add((m.group(1), m.group(2)))
    if not pairs:
        raise PolicyError("policy is empty; nothing is permitted")
    return pairs


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL, sender TEXT NOT NULL,
        recipient TEXT NOT NULL, body TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'PENDING',
        presented_turn TEXT, presented_ts TEXT, delivered_ts TEXT)""",
    """CREATE TABLE IF NOT EXISTS events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, msg_id INTEGER, kind TEXT NOT NULL,
        detail TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_msg_state ON messages(recipient, state)",
]


def connect():
    con = sqlite3.connect(db_path(), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _event(con, msg_id, kind, detail):
    con.execute("INSERT INTO events(ts, msg_id, kind, detail) "
                "VALUES(?,?,?,?)", (utcnow(), msg_id, kind, detail[:400]))


def init_db():
    """Root-only idempotent init enforcing the records permission
    pattern. Test roots (SMS_ROOT override) skip the root gate.
    Idempotent: the schema statements run on every call, but the
    'init' audit event is written only by the call that creates the
    events table (RC6 #22: a re-run must not add rows)."""
    prod = "SMS_ROOT" not in os.environ
    if prod and os.geteuid() != 0:
        raise SmsError("init must run as root (permission enforcement)")
    os.makedirs(store_dir(), exist_ok=True)
    con = connect()
    with con:
        created = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='events'").fetchone() is None
        for stmt in _SCHEMA:
            con.execute(stmt)
        if created:
            _event(con, None, "init",
                   "sms store initialized at %s" % db_path())
    con.close()
    if prod:
        import grp  # noqa: PLC0415
        gid = grp.getgrnam("treebeard").gr_gid
        os.chown(store_dir(), 0, gid)
        os.chmod(store_dir(), 0o2770)
        os.chown(db_path(), 0, gid)
        os.chmod(db_path(), 0o660)


def _audit_refusal(kind, detail):
    try:
        con = connect()
        with con:
            _event(con, None, kind, detail)
        con.close()
    except Exception:
        pass  # audit of a refusal must not mask the refusal itself


_ASCII_REPL = {"\u2014": "--", "\u2013": "-", "\u2018": "'", "\u2019": "'",
               "\u201c": '"', "\u201d": '"', "\u2026": "...",
               "\u2192": "->", "\u2190": "<-", "\u00a0": " ",
               "\u2011": "-", "\u2022": "*", "\u2010": "-", "\u00ad": ""}


def to_ascii(s):
    """Coerce a body to ascii AT THE BOUNDARY (2026-08-30).

    The store is ascii and stays ascii -- that contract is unchanged. What
    changed is the enforcement: a raw refusal on any non-ascii character
    meant agents writing an ordinary em-dash had their messages silently
    rejected, and inter-agent messaging had never worked in practice
    (found live: three meeting rounds where every send after the first was
    refused and neither agent knew). Typography is not a governance
    concern, so it degrades instead of refusing. A body that coerces to
    NOTHING still refuses -- storing an empty message is worse than
    declining to store one.
    """
    s = s or ""
    for k, v in _ASCII_REPL.items():
        s = s.replace(k, v)
    return (unicodedata.normalize("NFKD", s)
            .encode("ascii", "ignore").decode("ascii"))


def _prov(con, msg_id):
    """The ONE provenance record for a message, or None (fail closed on
    zero, malformed, or multiple -- events is append-only)."""
    rows = con.execute("SELECT detail FROM events WHERE msg_id=? AND kind='provenance'",
                       (msg_id,)).fetchall()
    if len(rows) != 1:
        return None
    try:
        d = json.loads(rows[0]["detail"] or "")
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    t, u = d.get("thread_id"), d.get("turn_id")
    if not (isinstance(t, str) and isinstance(u, str) and t.strip() and u.strip()):
        return None
    return {"thread_id": t.strip(), "turn_id": u.strip()}


def send(sender, recipient, body, thread_id=None, turn_id=None):
    """Validate against live policy and store one message. RAISES on any
    refusal; the caller owns fail-soft. Returns store-assigned id.
    thread_id/turn_id (optional) record which turn SENT the message, in
    the same transaction, so a recipient can quote the originating
    operator prompt instead of trusting the sender's paraphrase."""
    sender = (sender or "").strip().lower()
    recipient = (recipient or "").strip().lower()
    if not sender or not recipient:
        raise SmsError("sender and recipient required")
    if sender == recipient:
        raise SmsError("self-message refused")
    try:
        pairs = load_pairs()
    except PolicyError as exc:
        _audit_refusal("policy-error", "%s->%s: %s"
                       % (sender, recipient, exc))
        raise
    if (sender, recipient) not in pairs:
        _audit_refusal("denied", "pair %s->%s not in policy"
                       % (sender, recipient))
        raise SmsError("pair %s->%s not permitted" % (sender, recipient))
    raw = (body or "").strip()
    if not raw:
        raise SmsError("empty body")
    body = to_ascii(raw).strip()
    if not body:
        raise SmsError("body has no ascii-representable content")
    if len(body) > body_max():
        raise SmsError("body %d > %d chars" % (len(body), body_max()))
    con = connect()
    with con:
        cur = con.execute(
            "INSERT INTO messages(ts_utc, sender, recipient, body) "
            "VALUES(?,?,?,?)", (utcnow(), sender, recipient, body))
        mid = cur.lastrowid
        _event(con, mid, "sent", "%s->%s %d chars"
               % (sender, recipient, len(body)))
        if thread_id and turn_id:
            _event(con, mid, "provenance", json.dumps(
                {"v": 1, "thread_id": str(thread_id).strip(),
                 "turn_id": str(turn_id).strip()}, sort_keys=True))
    con.close()
    return mid


def _reclaim(con):
    # Cutoff computed in Python in the SAME format presented_ts is written
    # in (utcnow(): ISO-8601 with 'T' and offset). Comparing against
    # sqlite's datetime('now') ('YYYY-MM-DD HH:MM:SS') never matched
    # ('T' sorts above ' '), so before 2026-09-03 stale presentations were
    # never reclaimed and at-least-once did not hold after a failed turn.
    from datetime import timedelta  # noqa: PLC0415
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_s())).isoformat()
    rows = con.execute(
        "SELECT id FROM messages WHERE state='PRESENTED' AND presented_ts < ?",
        (cutoff,)).fetchall()
    for r in rows:
        con.execute("UPDATE messages SET state='PENDING', "
                    "presented_turn=NULL, presented_ts=NULL WHERE id=?",
                    (r["id"],))
        _event(con, r["id"], "reclaimed", "stale presentation > %ds"
               % stale_s())
    return len(rows)


def present(recipient, turn_id, n=None):
    """Atomically claim up to present_max() oldest PENDING messages for
    this turn. Returns ([dict(id, sender, ts_utc, body)], pending_more).
    Policy-agnostic by design: authorization occurred at send."""
    recipient = (recipient or "").strip().lower()
    cap = present_max() if n is None else max(1, min(int(n), present_max()))
    con = connect()
    out = []
    with con:
        _reclaim(con)
        rows = con.execute(
            "SELECT * FROM messages WHERE recipient=? AND state='PENDING' "
            "ORDER BY id LIMIT ?", (recipient, cap)).fetchall()
        now = utcnow()
        for r in rows:
            con.execute("UPDATE messages SET state='PRESENTED', "
                        "presented_turn=?, presented_ts=? WHERE id=?",
                        (turn_id, now, r["id"]))
            _event(con, r["id"], "presented", "turn %s" % turn_id)
            p = _prov(con, r["id"])
            out.append({"id": r["id"], "sender": r["sender"],
                        "ts_utc": r["ts_utc"], "body": r["body"],
                        "thread_id": p["thread_id"] if p else None,
                        "turn_id": p["turn_id"] if p else None})
        more = con.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient=? AND "
            "state='PENDING'", (recipient,)).fetchone()[0]
    con.close()
    return out, more


def deliver(turn_id):
    """Successful turn completion: PRESENTED(turn) -> DELIVERED."""
    con = connect()
    with con:
        rows = con.execute(
            "SELECT id FROM messages WHERE state='PRESENTED' AND "
            "presented_turn=?", (turn_id,)).fetchall()
        now = utcnow()
        for r in rows:
            con.execute("UPDATE messages SET state='DELIVERED', "
                        "delivered_ts=? WHERE id=?", (now, r["id"]))
            _event(con, r["id"], "delivered", "turn %s" % turn_id)
    con.close()
    return len(rows)


def cmd_init(args):
    init_db()
    print("sms store initialized: %s" % db_path())
    print("policy file: %s" % pairs_path())
    return 0


def cmd_send(args):
    mid = send(args.sender, args.recipient, " ".join(args.body))
    print("sent #%d %s->%s" % (mid, args.sender, args.recipient))
    return 0


def cmd_log(args):
    con = connect()
    for r in con.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?",
                         (args.n,)):
        print("%s #%d %s->%s [%s] %s"
              % (r["ts_utc"], r["id"], r["sender"], r["recipient"],
                 r["state"], r["body"][:80]))
    con.close()
    return 0


def cmd_policy(args):
    try:
        pairs = load_pairs()
        for a, b in sorted(pairs):
            print("%s -> %s" % (a, b))
        print("(%d pairs)" % len(pairs))
        return 0
    except PolicyError as exc:
        print("POLICY ERROR (fail-closed, nothing permitted): %s" % exc)
        return 2


def cmd_selftest(args):
    results = []

    def ok(name, cond, note=""):
        results.append((name, bool(cond)))
        print("%-46s %s" % (name, "ok" if cond else "FAIL"))
        if not cond and note:
            print("    detail: %s" % note)

    saved = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix="sms-selftest-")
    conf = os.path.join(tmp, "pairs.conf")

    def write_conf(text):
        with open(conf, "w", encoding="ascii") as fh:
            fh.write(text)

    try:
        os.environ["SMS_ROOT"] = tmp
        os.environ["SMS_PAIRS_CONF"] = conf
        init_db()
        ok("init creates db", os.path.exists(db_path()))

        def _init_events():
            c = connect()
            try:
                return c.execute("SELECT count(*) FROM events "
                                 "WHERE kind='init'").fetchone()[0]
            finally:
                c.close()

        def _dump():
            c = connect()
            try:
                return "\n".join(c.iterdump())
            finally:
                c.close()

        ok("first init records exactly one init event", _init_events() == 1,
           "init events %d" % _init_events())
        # RC6 #22: idempotence on its own store, so later checks see the
        # store exactly as before (SMS_ROOT is restored below).
        os.environ["SMS_ROOT"] = os.path.join(tmp, "idem")
        os.makedirs(os.environ["SMS_ROOT"], exist_ok=True)
        init_db()
        c = connect()
        with c:
            c.execute("INSERT INTO messages(ts_utc, sender, recipient, body) "
                      "VALUES('2026-01-01T00:00:00Z', 'aa', 'zz', 'keep')")
        c.close()
        d0 = _dump()
        init_db()
        d1 = _dump()
        ok("re-init adds no row (dump identical)", d0 == d1,
           "dump changed: %d -> %d lines" % (len(d0.splitlines()),
                                             len(d1.splitlines())))
        ok("re-init keeps one init event and the message",
           _init_events() == 1 and "'keep'" in d1,
           "init events %d" % _init_events())
        c = connect()
        with c:
            c.execute("DROP INDEX idx_msg_state")
        c.close()
        init_db()
        c = connect()
        idx = c.execute("SELECT count(*) FROM sqlite_master WHERE "
                        "type='index' AND name='idx_msg_state'").fetchone()[0]
        c.close()
        ok("re-init still runs the schema (index restored)", idx == 1)
        os.environ["SMS_ROOT"] = tmp

        # policy load discipline
        try:
            send("aa", "bb", "x")
            r = False
        except PolicyError:
            r = True
        ok("missing policy fails closed", r)
        write_conf("# demo roster\naa -> bb\nbb -> aa\naa -> cc\n")
        mid = send("aa", "bb", "hello from aa")
        ok("conf-driven pair sends, store id", isinstance(mid, int))
        try:
            send("bb", "cc", "x")
            r = False
        except SmsError:
            r = True
        ok("conf-driven deny fail-closed", r)
        write_conf("aa -> bb\nthis is not a pair line\nbb -> aa\n")
        try:
            send("aa", "bb", "x")
            r = False
        except PolicyError:
            r = True
        ok("one malformed line voids whole policy", r)
        write_conf("")
        try:
            send("aa", "bb", "x")
            r = False
        except PolicyError:
            r = True
        ok("empty policy fails closed", r)
        write_conf("aa -> bb\nbb -> aa\naa -> cc\n")
        ok("conf edit takes effect next call",
           isinstance(send("aa", "cc", "now allowed"), int))
        con = connect()
        kinds = [r2["kind"] for r2 in
                 con.execute("SELECT kind FROM events")]
        con.close()
        ok("policy-error distinct from denied in audit",
           "policy-error" in kinds and "denied" in kinds)

        # validation
        try:
            send("aa", "aa", "x")
            r = False
        except SmsError:
            r = True
        ok("self-message refused", r)
        try:
            send("aa", "bb", "y" * (body_max() + 1))
            r = False
        except SmsError:
            r = True
        ok("oversize refused", r)
        # 2026-08-30: a non-ascii body is COERCED, not refused. The old
        # test asserted the refusal that had silently broken inter-agent
        # messaging; typography is not a governance concern.
        try:
            _mid = send("aa", "bb", "caf" + chr(233))
            _con = connect()
            _row = _con.execute("SELECT body FROM messages WHERE id=?",
                                (_mid,)).fetchone()
            _con.close()
            r = _row is not None and _row[0] == "cafe"
        except SmsError:
            r = False
        ok("accented body coerced and sent", r)
        try:
            _mid = send("aa", "bb", "a " + chr(0x2014) + " b")
            _con = connect()
            _row = _con.execute("SELECT body FROM messages WHERE id=?",
                                (_mid,)).fetchone()
            _con.close()
            r = _row is not None and _row[0] == "a -- b"
        except SmsError:
            r = False
        ok("em-dash becomes -- and sends", r)
        try:
            send("aa", "bb", chr(0x4f60) + chr(0x597d))
            r = False
        except SmsError:
            r = True
        ok("body with no ascii content still refused", r)
        os.environ["SMS_BODY_MAX"] = "8"
        try:
            send("aa", "bb", "123456789")
            r = False
        except SmsError:
            r = True
        ok("caps are env-configurable", r)
        del os.environ["SMS_BODY_MAX"]

        # delivery machine
        for i in range(6):
            send("aa", "bb", "note %d" % i)
        msgs, more = present("bb", "turn-A")
        ok("present caps at 5 oldest", len(msgs) == 5
           and msgs[0]["id"] == mid and more >= 2,
           "%d/%d" % (len(msgs), more))
        ndel = deliver("turn-A")
        ok("deliver finalizes the turn's set", ndel == 5)
        msgs2, _ = present("bb", "turn-B")
        got2 = sorted(m["id"] for m in msgs2)
        con = connect()
        with con:
            con.execute("UPDATE messages SET presented_ts="
                        "datetime('now','-100000 seconds') WHERE "
                        "state='PRESENTED'")
        con.close()
        msgs3, _ = present("bb", "turn-C")
        ok("failed-turn messages reclaimed, ids stable",
           sorted(m["id"] for m in msgs3) == got2)
        send("aa", "bb", "fresh")
        present("bb", "turn-D")
        msgs4, _ = present("bb", "turn-E")
        ok("live presentation not re-presented", len(msgs4) == 0)
        con = connect()
        kinds = set(r2["kind"] for r2 in
                    con.execute("SELECT DISTINCT kind FROM events"))
        con.close()
        ok("event trail covers machine",
           {"init", "sent", "denied", "policy-error", "presented",
            "delivered", "reclaimed"}.issubset(kinds), str(kinds))

        fails = [x for x in results if not x[1]]
        print("SELFTEST %d/%d PASS" % (len(results) - len(fails),
                                       len(results)))
        if fails:
            sys.exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sms")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("send")
    p.add_argument("sender")
    p.add_argument("recipient")
    p.add_argument("body", nargs="+")
    p.set_defaults(func=cmd_send)
    p = sub.add_parser("log")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(func=cmd_log)
    p = sub.add_parser("policy")
    p.set_defaults(func=cmd_policy)
    p = sub.add_parser("selftest")
    p.set_defaults(func=cmd_selftest)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
