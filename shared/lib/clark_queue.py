#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
clark_queue.py -- SQLite-backed FIFO overflow queue for Clark intake.

Integrated into the existing Clark inbound ledger (ledger.sqlite) as an
additive 'queue' table. Idempotent schema init on driver invocation.

Semantics locked by ruling 2026-08-18:
  - FIFO ordering by queue_id ASC (deterministic; queued_at is evidence).
  - Drain up to N=5 per tick, oldest first.
  - Overflow stays in queue, never discarded.
  - Dedupe by normalized_url (UNIQUE constraint at DB level).
  - No ranking, no reordering.
  - States: QUEUED, SENT, EXPIRED, FAILED. Never delete rows.
  - URL normalization strips known tracking params only.
  - 14-day TTL transitions to EXPIRED with explicit reason.

Public API used by driver:
  init_schema(conn)
  normalize_url(url) -> str
  enqueue(conn, candidate, provenance) -> (queue_id_or_None, note)
  seen_urls_in_queue(conn) -> set[str]  (states=QUEUED,SENT,FAILED,EXPIRED)
  drain(conn, limit=5) -> list[dict]  (each: queue_id, payload, provenance)
  mark_sent(conn, queue_id, reason='digest sent')
  mark_failed(conn, queue_id, reason)
  expire_old(conn, days=14) -> int (count expired this tick)
  queue_stats(conn) -> dict {state: count}
"""
import json
import re
import sqlite3
import time
import urllib.parse

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS queue (
    queue_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    payload_json   TEXT NOT NULL,
    provenance     TEXT NOT NULL,
    queued_at      TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'QUEUED',
    state_reason   TEXT,
    sent_at        TEXT,
    ticks_seen     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_state_id
    ON queue(state, queue_id);
CREATE INDEX IF NOT EXISTS idx_queue_normalized
    ON queue(normalized_url);
"""

VALID_STATES = ("QUEUED", "SENT", "EXPIRED", "FAILED")

# Known tracking-only query params to strip during normalization.
# Conservative allowlist per ruling 2026-08-18: never strip generic
# ref/source/id/jid/req -- ATS and employer sites use those as real
# routing identifiers. Better a few duplicate queue entries than
# collapsing two distinct job URLs into one.
_TRACKING_PARAMS = frozenset((
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_name", "utm_reader",
    "gh_src", "gh_jid",  # Greenhouse tracking (real job id is in path)
    "mc_cid", "mc_eid",  # Mailchimp
    "_hsenc", "_hsmi", "hsctatracking",  # HubSpot
    "fbclid", "gclid", "msclkid",  # ad platforms
    "yclid",  # Yandex
    "dclid",  # DoubleClick
    "trk", "trkcampaign",  # LinkedIn tracking
    "src", "source_type",  # ambiguous but common tracking; keep 'source' bare
    "campaign_id",
))

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_url(url):
    """Deterministic URL normalization for dedupe keys.

    - lowercase scheme + host
    - strip default port (80 on http, 443 on https)
    - strip fragment
    - strip trailing slash from path (but preserve '/')
    - remove known tracking-only params (allowlist above)
    - preserve param order for surviving params

    Returns the normalized URL as a string. If the URL is unparseable,
    returns the input stripped of whitespace (never raises)."""
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url.strip())
    except (ValueError, AttributeError):
        return (url or "").strip()
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = p.port
    # Strip default port
    if port and _DEFAULT_PORTS.get(scheme) == port:
        netloc = host
    elif port:
        netloc = "%s:%d" % (host, port)
    else:
        netloc = host
    # Preserve userinfo if present (rare in job URLs; conservative keep)
    if p.username:
        auth = p.username
        if p.password:
            auth = auth + ":" + p.password
        netloc = auth + "@" + netloc
    # Strip trailing slash from path unless path is just '/'
    path = p.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    # Filter tracking params from query
    if p.query:
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        kept = [(k, v) for (k, v) in pairs
                if k.lower() not in _TRACKING_PARAMS]
        query = urllib.parse.urlencode(kept)
    else:
        query = ""
    # Drop fragment entirely
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def init_schema(conn):
    """Idempotent: creates queue table + indices if absent."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def enqueue(conn, candidate, provenance):
    """Insert one candidate as QUEUED. Enforces UNIQUE(normalized_url)
    at DB level. Returns (queue_id, note) on success or (None, reason)
    if rejected (duplicate, invalid).

    candidate: dict with at minimum 'url'. Full dict is stored as
      payload_json for later reconstitution.
    provenance: string identifying source (typically resend_id)."""
    if not isinstance(candidate, dict):
        return None, "candidate not a dict"
    url = candidate.get("url")
    if not url or not isinstance(url, str):
        return None, "candidate has no url"
    norm = normalize_url(url)
    if not norm:
        return None, "url unparseable"
    try:
        cur = conn.execute(
            "INSERT INTO queue (url, normalized_url, payload_json, "
            "provenance, queued_at, state) "
            "VALUES (?, ?, ?, ?, ?, 'QUEUED')",
            (url, norm, json.dumps(candidate, sort_keys=True),
             str(provenance), _now_utc()))
        conn.commit()
        return cur.lastrowid, "enqueued"
    except sqlite3.IntegrityError as e:
        # UNIQUE constraint on normalized_url -- duplicate.
        # Roll back: the failed INSERT opened a write txn and holds
        # the write lock; without rollback, other connections get
        # "database is locked".
        conn.rollback()
        return None, "duplicate: %s" % str(e)[:120]


def seen_urls_in_queue(conn):
    """Return set of normalized URLs present in queue regardless of
    state. Used by driver's dedupe union so that URLs previously
    QUEUED/SENT/FAILED/EXPIRED are not re-enqueued."""
    seen = set()
    for row in conn.execute("SELECT normalized_url FROM queue"):
        seen.add(row[0])
    return seen


def drain(conn, limit=5):
    """Return oldest 'limit' QUEUED rows as list of dicts. Order is
    queue_id ASC (FIFO). Does NOT change state -- caller must call
    mark_sent / mark_failed after processing. Increments ticks_seen
    on each drained row so we can detect stuck rows.

    Each returned dict:
      {queue_id, url, normalized_url, payload, provenance, queued_at}
    payload is the parsed candidate dict."""
    rows = list(conn.execute(
        "SELECT queue_id, url, normalized_url, payload_json, "
        "provenance, queued_at FROM queue "
        "WHERE state='QUEUED' "
        "ORDER BY queue_id ASC LIMIT ?", (limit,)))
    if not rows:
        return []
    # Bump ticks_seen for observability
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        "UPDATE queue SET ticks_seen = ticks_seen + 1 "
        "WHERE queue_id IN (%s)" % placeholders, ids)
    conn.commit()
    return [
        {"queue_id": r[0], "url": r[1], "normalized_url": r[2],
         "payload": json.loads(r[3]), "provenance": r[4],
         "queued_at": r[5]}
        for r in rows
    ]


def _transition(conn, queue_id, new_state, reason, set_sent_at=False):
    if new_state not in VALID_STATES:
        raise ValueError("bad state: %s" % new_state)
    if set_sent_at:
        conn.execute(
            "UPDATE queue SET state=?, state_reason=?, sent_at=? "
            "WHERE queue_id=? AND state='QUEUED'",
            (new_state, reason, _now_utc(), queue_id))
    else:
        conn.execute(
            "UPDATE queue SET state=?, state_reason=? "
            "WHERE queue_id=? AND state='QUEUED'",
            (new_state, reason, queue_id))
    conn.commit()


def mark_sent(conn, queue_id, reason="digest sent"):
    """Transition QUEUED -> SENT. Only if row is still QUEUED (defends
    against double-transition)."""
    _transition(conn, queue_id, "SENT", reason, set_sent_at=True)


def mark_failed(conn, queue_id, reason):
    """Transition QUEUED -> FAILED with explicit reason."""
    _transition(conn, queue_id, "FAILED", reason)


def mark_expired(conn, queue_id, reason):
    """Transition QUEUED -> EXPIRED with explicit reason."""
    _transition(conn, queue_id, "EXPIRED", reason)


def expire_old(conn, days=14):
    """Transition QUEUED rows older than N days to EXPIRED with reason.
    Returns count of expired rows. Does NOT delete."""
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - days * 86400))
    reason = "TTL exceeded (>%d days queued)" % days
    cur = conn.execute(
        "UPDATE queue SET state='EXPIRED', state_reason=? "
        "WHERE state='QUEUED' AND queued_at < ?", (reason, cutoff))
    conn.commit()
    return cur.rowcount


def queue_stats(conn):
    """Return dict of {state: count} across all queue rows."""
    stats = {}
    for row in conn.execute(
        "SELECT state, count(*) FROM queue GROUP BY state"):
        stats[row[0]] = row[1]
    return stats


# --- selftest (offline, in-memory SQLite) -----------------------------------

def selftest():
    fails = 0

    def check(label, cond):
        nonlocal fails
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        fails += 0 if cond else 1

    def fresh_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        init_schema(conn)
        return conn

    # --- URL normalization ---
    print("URL normalization:")
    check("lowercase host",
          normalize_url("https://Example.COM/path")
          == "https://example.com/path")
    check("strip default port https",
          normalize_url("https://x.com:443/p")
          == "https://x.com/p")
    check("strip default port http",
          normalize_url("http://x.com:80/p")
          == "http://x.com/p")
    check("keep non-default port",
          normalize_url("https://x.com:8443/p")
          == "https://x.com:8443/p")
    check("strip fragment",
          normalize_url("https://x.com/p#anchor")
          == "https://x.com/p")
    check("strip trailing slash",
          normalize_url("https://x.com/p/") == "https://x.com/p")
    check("preserve bare root slash",
          normalize_url("https://x.com/") == "https://x.com/")
    check("strip utm params",
          normalize_url("https://x.com/p?utm_source=g&job=42")
          == "https://x.com/p?job=42")
    check("strip gh_jid (tracking)",
          normalize_url("https://x.com/jobs/42?gh_jid=42")
          == "https://x.com/jobs/42")
    check("PRESERVE generic 'source' param",
          normalize_url("https://x.com/j?source=email")
          == "https://x.com/j?source=email")
    check("PRESERVE generic 'ref' param",
          normalize_url("https://x.com/j?ref=abc123")
          == "https://x.com/j?ref=abc123")
    check("PRESERVE generic 'id' param",
          normalize_url("https://x.com/j?id=xyz")
          == "https://x.com/j?id=xyz")
    check("empty query drops cleanly",
          normalize_url("https://x.com/p?utm_source=g")
          == "https://x.com/p")

    # --- Schema is idempotent ---
    print("Schema:")
    conn = fresh_conn()
    init_schema(conn)  # second call
    init_schema(conn)  # third call
    check("idempotent init", True)  # would have raised otherwise

    # --- Enqueue basic ---
    print("Enqueue:")
    conn = fresh_conn()
    qid1, note1 = enqueue(conn,
        {"url": "https://x.com/j/1", "title": "A", "company": "X"},
        "r-001")
    check("first enqueue succeeds", qid1 is not None)
    qid2, note2 = enqueue(conn,
        {"url": "https://x.com/j/2", "title": "B", "company": "X"},
        "r-001")
    check("second distinct url enqueue succeeds",
          qid2 is not None and qid2 > qid1)
    # Duplicate normalized URL (same URL)
    qid_dup, note_dup = enqueue(conn,
        {"url": "https://x.com/j/1", "title": "A", "company": "X"},
        "r-002")
    check("duplicate url rejected", qid_dup is None)
    check("duplicate note mentions duplicate",
          "duplicate" in (note_dup or "").lower())
    # Duplicate via normalization (trailing slash + tracking param)
    qid_norm, note_norm = enqueue(conn,
        {"url": "https://x.com/j/1/?utm_source=email",
         "title": "A", "company": "X"},
        "r-003")
    check("duplicate via normalization rejected",
          qid_norm is None)

    # --- Enqueue rejects bad input ---
    q_no, n_no = enqueue(conn, {"title": "no url"}, "r-004")
    check("no-url rejected", q_no is None)
    q_bad, n_bad = enqueue(conn, "not a dict", "r-005")
    check("non-dict rejected", q_bad is None)

    # --- FIFO drain: 3 items, drain 5 -> get 3 ---
    print("FIFO drain (3 items, limit=5):")
    conn = fresh_conn()
    for i in range(3):
        enqueue(conn,
            {"url": "https://e.com/j/%d" % i, "title": "T%d" % i,
             "company": "C"},
            "r-%d" % i)
    drained = drain(conn, limit=5)
    check("3 drained (all)", len(drained) == 3)
    check("FIFO order 0",
          drained[0]["payload"]["url"] == "https://e.com/j/0")
    check("FIFO order 2",
          drained[2]["payload"]["url"] == "https://e.com/j/2")

    # --- 5 items, drain 5 ---
    print("Drain 5 of 5:")
    conn = fresh_conn()
    for i in range(5):
        enqueue(conn,
            {"url": "https://e.com/j/%d" % i, "title": "T%d" % i,
             "company": "C"},
            "r-%d" % i)
    drained = drain(conn, limit=5)
    check("5 drained", len(drained) == 5)

    # --- 8 items, drain 5, then next drain 3 ---
    print("Drain 8 in two ticks (5 then 3):")
    conn = fresh_conn()
    for i in range(8):
        enqueue(conn,
            {"url": "https://e.com/j/%d" % i, "title": "T%d" % i,
             "company": "C"},
            "r-%d" % i)
    first = drain(conn, limit=5)
    check("first tick: 5 drained", len(first) == 5)
    # Simulate successful send
    for row in first:
        mark_sent(conn, row["queue_id"], "digest sent")
    stats1 = queue_stats(conn)
    check("after tick1: 5 SENT",
          stats1.get("SENT") == 5)
    check("after tick1: 3 QUEUED remain",
          stats1.get("QUEUED") == 3)
    second = drain(conn, limit=5)
    check("second tick: 3 drained (remaining)", len(second) == 3)
    check("second tick FIFO order matches queue_id 6/7/8",
          [r["queue_id"] for r in second] == [6, 7, 8])
    for row in second:
        mark_sent(conn, row["queue_id"], "digest sent")
    stats2 = queue_stats(conn)
    check("after tick2: 8 SENT total, 0 QUEUED",
          stats2.get("SENT") == 8 and stats2.get("QUEUED", 0) == 0)

    # --- Queued work stays ahead of newer work ---
    print("FIFO invariant across ticks with new arrivals:")
    conn = fresh_conn()
    # 3 old queued
    for i in range(3):
        enqueue(conn, {"url": "https://old.com/%d" % i}, "r-old-%d" % i)
    # simulate one tick that DIDN'T send (network issue etc.)
    d1 = drain(conn, limit=5)
    check("tick1: drains 3 old", len(d1) == 3)
    # Don't mark_sent -- simulate send failure. Rows stay QUEUED.
    check("stayed QUEUED after undrained failure",
          queue_stats(conn).get("QUEUED") == 3)
    # 7 new arrivals
    for i in range(7):
        enqueue(conn, {"url": "https://new.com/%d" % i}, "r-new-%d" % i)
    check("10 QUEUED total", queue_stats(conn).get("QUEUED") == 10)
    d2 = drain(conn, limit=5)
    check("tick2: drains 5", len(d2) == 5)
    check("oldest 3 are the original OLD",
          all("old.com" in r["url"] for r in d2[:3]))
    check("next 2 are NEW (FIFO order)",
          all("new.com" in r["url"] for r in d2[3:5]))
    for row in d2:
        mark_sent(conn, row["queue_id"], "digest sent")
    d3 = drain(conn, limit=5)
    check("tick3: drains remaining 5 new", len(d3) == 5)
    check("all remaining are NEW",
          all("new.com" in r["url"] for r in d3))

    # --- Persistence across "restart" (close/reopen file-backed DB) ---
    print("Persistence across restart:")
    import tempfile
    import os
    tmpdir = tempfile.mkdtemp(prefix="clark-queue-selftest-")
    dbpath = os.path.join(tmpdir, "ledger.sqlite")
    try:
        c1 = sqlite3.connect(dbpath)
        init_schema(c1)
        enqueue(c1, {"url": "https://p.com/j/1"}, "r-p-1")
        enqueue(c1, {"url": "https://p.com/j/2"}, "r-p-2")
        c1.close()
        # Reopen
        c2 = sqlite3.connect(dbpath)
        # init_schema is idempotent even on preexisting file
        init_schema(c2)
        d = drain(c2, limit=5)
        check("after reopen: 2 rows drainable", len(d) == 2)
        check("order preserved",
              d[0]["url"] == "https://p.com/j/1"
              and d[1]["url"] == "https://p.com/j/2")
        c2.close()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- mark_sent / mark_failed / mark_expired transitions ---
    print("State transitions:")
    conn = fresh_conn()
    q1, _ = enqueue(conn, {"url": "https://t.com/1"}, "r-t-1")
    q2, _ = enqueue(conn, {"url": "https://t.com/2"}, "r-t-2")
    q3, _ = enqueue(conn, {"url": "https://t.com/3"}, "r-t-3")
    drain(conn, limit=5)  # bump ticks_seen
    mark_sent(conn, q1, "digest ok")
    mark_failed(conn, q2, "factuality rejected: fabricated metric")
    mark_expired(conn, q3, "TTL exceeded")
    stats = queue_stats(conn)
    check("SENT count 1", stats.get("SENT") == 1)
    check("FAILED count 1", stats.get("FAILED") == 1)
    check("EXPIRED count 1", stats.get("EXPIRED") == 1)
    check("QUEUED count 0", stats.get("QUEUED", 0) == 0)
    # Verify transitions don't re-fire on non-QUEUED rows
    mark_sent(conn, q2, "should not happen")
    row = conn.execute(
        "SELECT state, state_reason FROM queue WHERE queue_id=?",
        (q2,)).fetchone()
    check("mark_sent on FAILED row is no-op",
          row[0] == "FAILED" and "factuality" in row[1])

    # --- expire_old ---
    print("expire_old TTL:")
    conn = fresh_conn()
    # Insert one old and one new by manual timestamp
    conn.execute(
        "INSERT INTO queue (url, normalized_url, payload_json, "
        "provenance, queued_at, state) "
        "VALUES (?, ?, ?, ?, ?, 'QUEUED')",
        ("https://old.com/1", "https://old.com/1",
         '{"url":"https://old.com/1"}', "r-old",
         "2026-01-01T00:00:00Z"))
    enqueue(conn, {"url": "https://new.com/1"}, "r-new")
    n = expire_old(conn, days=14)
    check("1 expired", n == 1)
    stats = queue_stats(conn)
    check("1 EXPIRED after TTL", stats.get("EXPIRED") == 1)
    check("1 QUEUED still fresh", stats.get("QUEUED") == 1)
    # Row is present, not deleted
    old_row = conn.execute(
        "SELECT state, state_reason FROM queue "
        "WHERE url='https://old.com/1'").fetchone()
    check("EXPIRED row still present with reason",
          old_row is not None and old_row[0] == "EXPIRED"
          and "TTL" in old_row[1])

    # --- No ranking / reordering: drain is deterministic FIFO ---
    print("No reordering:")
    conn = fresh_conn()
    # Enqueue with intentionally "bad" tier first, then "good" tier.
    # Drain must return in insertion order regardless.
    enqueue(conn,
        {"url": "https://bad.com/j/1", "source_tier": 0,
         "title": "sludge"}, "r-a")
    enqueue(conn,
        {"url": "https://excellent.com/j/1", "source_tier": 4,
         "title": "great"}, "r-b")
    enqueue(conn,
        {"url": "https://medium.com/j/1", "source_tier": 2,
         "title": "meh"}, "r-c")
    d = drain(conn, limit=5)
    check("drain order strictly by queue_id (no tier reorder)",
          [r["url"] for r in d]
          == ["https://bad.com/j/1", "https://excellent.com/j/1",
              "https://medium.com/j/1"])

    print("selftest: %d failures" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit("usage: clark_queue.py --selftest")
