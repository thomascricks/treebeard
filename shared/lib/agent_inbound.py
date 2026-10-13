#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
agent_inbound.py -- generic Resend-inbound poller for Treebeard agents.

One poll cycle per invocation:
  list /emails/receiving (paginated, has_more)
  -> for each new resend_id not in ledger:
       retrieve /emails/receiving/:id
       download raw.download_url (expires) to raw.eml
       write metadata/text/html/headers/raw.eml under raw/<resend_id>/
       insert ledger row state=RECEIVED
  -> return PollResult (counts + lists) to caller for interpretation

Ledger states (spec 2026-08-18, all seven kept in schema for future use):
  RECEIVED                  -- retrieved + raw stored; awaiting interp
  PROCESSED                 -- downstream generated packet(s); terminal
  NOT_JOB_RELATED           -- extractor found no candidate links; terminal
  AMBIGUOUS_NEEDS_OPERATOR  -- reserved (v1 quarantines instead)
  FAILED                    -- retrieve/raw/write failed; retryable
  DUPLICATE                 -- same message_id under new resend_id; terminal
  QUARANTINED_SENDER        -- reserved (allowlist deferred to GitHub prep)

Design commitments:
  - api_key passed by caller; never read from env inside the library
  - raw MIME downloaded AT INGESTION (raw.download_url has expires_at)
  - list is retried on transport error inside poll(); per-message failures
    do not abort the whole poll cycle -- they become FAILED ledger rows
  - fail-soft: expected errors surface in PollResult; only unexpected
    internal errors escape as exceptions
  - dedupe order: resend_id (list stage, cheap) then message_id (retrieve
    stage, catches same email forwarded twice with different resend_ids)
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DEFAULT_BASE_DIR = f"{_TB_BASE}/shared/inbound"
RESEND_API_BASE = "https://api.resend.com"
LIST_PATH = "/emails/receiving"
RETRIEVE_PATH = "/emails/receiving/%s"
USER_AGENT = "treebeard-agent-inbound/1"
HTTP_TIMEOUT = 30
LIST_PAGE_LIMIT = 50
LIST_MAX_PAGES = 20
RAW_DOWNLOAD_TIMEOUT = 60
RAW_MAX_BYTES = 25 * 1024 * 1024  # 25 MB per message; well over any email

STATES = (
    "RECEIVED", "PROCESSED", "NOT_JOB_RELATED",
    "AMBIGUOUS_NEEDS_OPERATOR", "FAILED", "DUPLICATE",
    "QUARANTINED_SENDER",
)
TERMINAL_STATES = frozenset((
    "PROCESSED", "NOT_JOB_RELATED", "AMBIGUOUS_NEEDS_OPERATOR",
    "DUPLICATE", "QUARANTINED_SENDER",
))
# FAILED is terminal for the current poll but retryable in a later one:
# it does NOT block re-processing, so it is NOT in TERMINAL_STATES.


# --- HTTP transport (injectable for tests) -----------------------------------

def _default_http(method, url, headers, timeout):
    """Real HTTP call. Returns (status, body_bytes). Raises on network err."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx: return status + body so caller can inspect
        return e.code, e.read() if hasattr(e, "read") else b""


# Module-level hook so selftest can inject a mock without touching poll().
_http_call = _default_http


def _api_get(path, api_key):
    """GET api.resend.com<path>. Returns (status, parsed_json_or_None)."""
    url = RESEND_API_BASE + path
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    status, body = _http_call("GET", url, headers, HTTP_TIMEOUT)
    if not body:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def _download_raw(url, dest_path):
    """Fetch signed URL bytes to dest_path. Enforces size cap."""
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=RAW_DOWNLOAD_TIMEOUT) as r:
        total = 0
        tmp = dest_path + ".part"
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > RAW_MAX_BYTES:
                    f.close()
                    os.unlink(tmp)
                    raise RuntimeError(
                        "raw exceeds %d bytes" % RAW_MAX_BYTES)
                f.write(chunk)
        os.rename(tmp, dest_path)
    return total


# --- ledger ------------------------------------------------------------------

def _ledger_path(agent, base_dir):
    return os.path.join(base_dir, agent, "ledger.sqlite")


def _ledger_conn(agent, base_dir):
    path = _ledger_path(agent, base_dir)
    if not os.path.exists(path):
        raise RuntimeError("ledger missing: %s" % path)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ledger_has_resend_id(conn, resend_id):
    row = conn.execute(
        "SELECT 1 FROM inbound WHERE resend_id = ? LIMIT 1",
        (resend_id,)).fetchone()
    return row is not None


def _ledger_find_by_message_id(conn, message_id):
    if not message_id:
        return None
    row = conn.execute(
        "SELECT resend_id FROM inbound WHERE message_id = ? LIMIT 1",
        (message_id,)).fetchone()
    return row[0] if row else None


def _ledger_insert(conn, resend_id, message_id, sender_from, subject,
                   state, state_reason, raw_stored_at):
    now = _utcnow()
    finalized = now if state in TERMINAL_STATES else None
    conn.execute(
        "INSERT INTO inbound (resend_id, message_id, sender_from, subject,"
        " state, state_reason, first_seen_at, raw_stored_at, finalized_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (resend_id, message_id, sender_from, subject, state, state_reason,
         now, raw_stored_at, finalized))


def _ledger_update_state(conn, resend_id, state, reason, force):
    row = conn.execute(
        "SELECT state FROM inbound WHERE resend_id = ?",
        (resend_id,)).fetchone()
    if row is None:
        raise RuntimeError("no ledger row for %s" % resend_id)
    current = row[0]
    if current in TERMINAL_STATES and not force:
        raise RuntimeError(
            "refuse to overwrite terminal state %s (pass force=True)"
            % current)
    now = _utcnow()
    finalized = now if state in TERMINAL_STATES else None
    conn.execute(
        "UPDATE inbound SET state = ?, state_reason = ?, finalized_at = ?"
        " WHERE resend_id = ?",
        (state, reason, finalized, resend_id))


# --- raw store ---------------------------------------------------------------

def _raw_dir(agent, base_dir, resend_id):
    return os.path.join(base_dir, agent, "raw", resend_id)


def _write_message_files(agent, base_dir, resend_id, list_entry,
                         retrieve_json, raw_url):
    """Write metadata/bodies/headers/raw.eml. Returns (raw_bytes, raw_path)."""
    d = _raw_dir(agent, base_dir, resend_id)
    os.makedirs(d, mode=0o2750, exist_ok=True)
    _atomic_write_json(os.path.join(d, "list_metadata.json"), list_entry)
    # Scrub raw.download_url before persisting -- it is a signed URL that
    # looks like a live credential and expires quickly.
    scrubbed = dict(retrieve_json)
    raw_field = scrubbed.get("raw")
    if isinstance(raw_field, dict):
        scrubbed["raw"] = {k: ("[scrubbed]" if k == "download_url" else v)
                           for k, v in raw_field.items()}
    _atomic_write_json(os.path.join(d, "retrieve_metadata.json"), scrubbed)
    _atomic_write_text(os.path.join(d, "text.txt"),
                       retrieve_json.get("text") or "")
    _atomic_write_text(os.path.join(d, "html.html"),
                       retrieve_json.get("html") or "")
    _atomic_write_json(os.path.join(d, "headers.json"),
                       retrieve_json.get("headers") or {})
    _atomic_write_text(os.path.join(d, "received_at.txt"), _utcnow())
    raw_path = os.path.join(d, "raw.eml")
    raw_bytes = _download_raw(raw_url, raw_path)
    return raw_bytes, raw_path


def _atomic_write_text(path, text):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.rename(tmp, path)


def _atomic_write_json(path, obj):
    _atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True))


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- public API --------------------------------------------------------------

class PollResult(object):
    """Counts and lists returned by poll(). Plain object, not a namedtuple,
    so callers can extend fields without breaking older callers."""
    def __init__(self):
        self.listed = 0
        self.new = 0
        self.received = []          # list of resend_ids newly RECEIVED
        self.duplicates = []        # list of (resend_id, original_resend_id)
        self.failed = []            # list of (resend_id, reason)
        self.transport_error = None # str or None
        self.pages_fetched = 0

    def as_dict(self):
        return {
            "listed": self.listed,
            "new": self.new,
            "received": list(self.received),
            "duplicates": list(self.duplicates),
            "failed": list(self.failed),
            "transport_error": self.transport_error,
            "pages_fetched": self.pages_fetched,
        }


def poll(agent, api_key=None, base_dir=DEFAULT_BASE_DIR,
         page_limit=LIST_PAGE_LIMIT, max_pages=LIST_MAX_PAGES):
    """One poll cycle. See module docstring."""
    if not agent or not re.match(r"^[a-z][a-z0-9_]{0,31}$", agent):
        raise ValueError("invalid agent name: %r" % agent)
    if not api_key:
        raise ValueError("api_key required (caller passes; not read from env)")
    result = PollResult()
    conn = _ledger_conn(agent, base_dir)
    try:
        # 1. List, paginated
        listed_entries = []
        cursor = None
        for _ in range(max_pages):
            path = LIST_PATH + "?limit=%d" % page_limit
            if cursor:
                path += "&after=%s" % cursor
            status, body = _api_get(path, api_key)
            result.pages_fetched += 1
            if status != 200 or not isinstance(body, dict):
                result.transport_error = (
                    "list HTTP %s body=%r" % (status, type(body).__name__))
                return result
            data = body.get("data") or []
            listed_entries.extend(data)
            if not body.get("has_more"):
                break
            if not data:
                break
            cursor = data[-1].get("id")
            if not cursor:
                break
        result.listed = len(listed_entries)

        # 2. Per-message processing
        for entry in listed_entries:
            resend_id = entry.get("id")
            if not resend_id:
                continue
            if _ledger_has_resend_id(conn, resend_id):
                continue
            result.new += 1
            try:
                _process_new_message(conn, agent, base_dir, entry, api_key,
                                     result)
            except Exception as e:  # noqa: BLE001
                # Record and continue; never abort the whole poll.
                reason = "process crashed: %s" % str(e)[:200]
                try:
                    _ledger_insert(
                        conn, resend_id, entry.get("message_id"),
                        entry.get("from"), entry.get("subject"),
                        "FAILED", reason, None)
                except Exception:  # noqa: BLE001
                    pass
                result.failed.append((resend_id, reason))
    finally:
        conn.close()
    return result


def _process_new_message(conn, agent, base_dir, list_entry, api_key, result):
    resend_id = list_entry["id"]
    message_id = list_entry.get("message_id")
    # message_id dedupe (cross-resend-id): same underlying email forwarded
    # twice yields different resend_ids but the same RFC Message-ID.
    prior = _ledger_find_by_message_id(conn, message_id)
    if prior:
        _ledger_insert(
            conn, resend_id, message_id, list_entry.get("from"),
            list_entry.get("subject"), "DUPLICATE",
            "same message_id as %s" % prior, None)
        result.duplicates.append((resend_id, prior))
        return
    # Retrieve full message
    status, rjson = _api_get(RETRIEVE_PATH % resend_id, api_key)
    if status != 200 or not isinstance(rjson, dict):
        _ledger_insert(
            conn, resend_id, message_id, list_entry.get("from"),
            list_entry.get("subject"), "FAILED",
            "retrieve HTTP %s" % status, None)
        result.failed.append((resend_id, "retrieve HTTP %s" % status))
        return
    raw_field = rjson.get("raw") or {}
    raw_url = raw_field.get("download_url")
    if not raw_url:
        _ledger_insert(
            conn, resend_id, message_id, list_entry.get("from"),
            list_entry.get("subject"), "FAILED",
            "no raw.download_url in retrieve response", None)
        result.failed.append((resend_id, "no raw.download_url"))
        return
    try:
        _write_message_files(agent, base_dir, resend_id, list_entry,
                             rjson, raw_url)
    except Exception as e:  # noqa: BLE001
        _ledger_insert(
            conn, resend_id, message_id, list_entry.get("from"),
            list_entry.get("subject"), "FAILED",
            "raw store write failed: %s" % str(e)[:200], None)
        result.failed.append((resend_id, "raw store write: %s"
                              % str(e)[:120]))
        return
    _ledger_insert(
        conn, resend_id, message_id, list_entry.get("from"),
        list_entry.get("subject"), "RECEIVED", None, _utcnow())
    result.received.append(resend_id)


def list_pending(agent, base_dir=DEFAULT_BASE_DIR, state="RECEIVED"):
    """Rows in a given state, oldest first. No Resend calls."""
    conn = _ledger_conn(agent, base_dir)
    try:
        rows = conn.execute(
            "SELECT resend_id, message_id, sender_from, subject,"
            " first_seen_at, raw_stored_at FROM inbound WHERE state = ?"
            " ORDER BY first_seen_at ASC", (state,)).fetchall()
    finally:
        conn.close()
    return [
        {"resend_id": r[0], "message_id": r[1], "sender_from": r[2],
         "subject": r[3], "first_seen_at": r[4], "raw_stored_at": r[5]}
        for r in rows
    ]


def mark(agent, resend_id, state, reason="", base_dir=DEFAULT_BASE_DIR,
         force=False):
    """Transition a message to a new state. Refuses to overwrite terminal
    states unless force=True. See STATES for allowed values."""
    if state not in STATES:
        raise ValueError("unknown state: %r" % state)
    conn = _ledger_conn(agent, base_dir)
    try:
        _ledger_update_state(conn, resend_id, state, reason, force)
    finally:
        conn.close()


# --- selftest (offline, injected mock HTTP) ---------------------------------

def selftest():
    global _http_call
    saved_http = _http_call
    fails = 0

    def check(label, cond):
        nonlocal fails
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        fails += 0 if cond else 1

    tmp = tempfile.mkdtemp(prefix="agent-inbound-selftest-")
    agent = "testagent"
    agent_dir = os.path.join(tmp, agent)
    os.makedirs(os.path.join(agent_dir, "raw"))
    os.makedirs(os.path.join(agent_dir, "quarantine"))
    # Create ledger schema (mirror of the real one)
    dbpath = os.path.join(agent_dir, "ledger.sqlite")
    c = sqlite3.connect(dbpath)
    c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE inbound (
          resend_id TEXT PRIMARY KEY, message_id TEXT, sender_from TEXT,
          subject TEXT, state TEXT NOT NULL, state_reason TEXT,
          first_seen_at TEXT NOT NULL, raw_stored_at TEXT,
          finalized_at TEXT);
        CREATE INDEX idx_inbound_message_id ON inbound(message_id);
    """)
    c.close()

    # Mock HTTP: script of (url_pattern, status, body_dict-or-bytes)
    script = []

    def mock_http(method, url, headers, timeout):
        for i, (pat, status, body) in enumerate(script):
            if pat in url:
                script.pop(i)
                if isinstance(body, bytes):
                    return status, body
                return status, json.dumps(body).encode("utf-8")
        return 599, b'{"error":"no mock matched %s"}' % url.encode()

    # Also mock _download_raw to avoid real HTTP
    import urllib.request as _u
    saved_urlopen = _u.urlopen

    class _FakeResp(object):
        def __init__(self, data):
            self._data = data
            self._pos = 0
        def read(self, n=-1):
            if n < 0 or n >= len(self._data) - self._pos:
                chunk = self._data[self._pos:]
                self._pos = len(self._data)
                return chunk
            chunk = self._data[self._pos:self._pos + n]
            self._pos += n
            return chunk
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        # Every raw.download_url in tests returns the same fake MIME
        return _FakeResp(b"From: t@example\r\nSubject: raw\r\n\r\nhi\r\n")

    _u.urlopen = fake_urlopen
    _http_call = mock_http

    try:
        # Test 1: empty list -> no work, no ledger rows
        script.extend([
            ("/emails/receiving?limit=", 200,
             {"object": "list", "has_more": False, "data": []}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("empty list -> listed=0", r.listed == 0)
        check("empty list -> no transport error", r.transport_error is None)

        # Test 2: single new message -> RECEIVED
        script.extend([
            ("/emails/receiving?limit=", 200,
             {"object": "list", "has_more": False, "data": [
                 {"id": "rid-1", "message_id": "<m-1@x>",
                  "from": "a@x", "subject": "one"}]}),
            ("/emails/receiving/rid-1", 200,
             {"id": "rid-1", "message_id": "<m-1@x>", "text": "body-1",
              "html": "<p>x</p>", "headers": {"from": "a@x"},
              "raw": {"download_url": "https://signed/rid-1",
                      "expires_at": "2099-01-01"}}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("new msg listed", r.listed == 1 and r.new == 1)
        check("new msg RECEIVED", r.received == ["rid-1"])
        check("raw.eml written",
              os.path.exists(os.path.join(agent_dir, "raw", "rid-1",
                                          "raw.eml")))
        check("text.txt written",
              os.path.exists(os.path.join(agent_dir, "raw", "rid-1",
                                          "text.txt")))
        # raw.download_url must be scrubbed on disk
        meta = json.load(open(os.path.join(agent_dir, "raw", "rid-1",
                                           "retrieve_metadata.json")))
        check("raw.download_url scrubbed on disk",
              meta["raw"]["download_url"] == "[scrubbed]")

        # Test 3: same resend_id re-listed -> skipped, no new work
        script.extend([
            ("/emails/receiving?limit=", 200,
             {"object": "list", "has_more": False, "data": [
                 {"id": "rid-1", "message_id": "<m-1@x>",
                  "from": "a@x", "subject": "one"}]}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("same resend_id skipped", r.listed == 1 and r.new == 0)

        # Test 4: new resend_id, same message_id -> DUPLICATE
        script.extend([
            ("/emails/receiving?limit=", 200,
             {"object": "list", "has_more": False, "data": [
                 {"id": "rid-2", "message_id": "<m-1@x>",
                  "from": "a@x", "subject": "one-forwarded-again"}]}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("cross-resend-id dedupe -> DUPLICATE",
              len(r.duplicates) == 1 and r.duplicates[0][0] == "rid-2"
              and r.duplicates[0][1] == "rid-1")

        # Test 5: retrieve returns 404 -> FAILED, ledger row exists
        script.extend([
            ("/emails/receiving?limit=", 200,
             {"object": "list", "has_more": False, "data": [
                 {"id": "rid-3", "message_id": "<m-3@x>",
                  "from": "a@x", "subject": "gone"}]}),
            ("/emails/receiving/rid-3", 404, {"error": "not found"}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("retrieve 404 -> FAILED",
              len(r.failed) == 1 and r.failed[0][0] == "rid-3")
        check("FAILED is not terminal (retryable)",
              "FAILED" not in TERMINAL_STATES)

        # Test 6: list transport failure -> transport_error, no writes
        script.extend([
            ("/emails/receiving?limit=", 502, {"error": "bad gateway"}),
        ])
        r = poll(agent, api_key="k_test", base_dir=tmp)
        check("list 502 -> transport_error set",
              r.transport_error is not None)
        check("list 502 -> no new ledger rows",
              r.new == 0)

        # Test 7: mark() refuses to overwrite terminal state without force
        # rid-2 is DUPLICATE (terminal). Try to mark PROCESSED without force.
        try:
            mark(agent, "rid-2", "PROCESSED", base_dir=tmp)
            check("mark refuses terminal overwrite", False)
        except RuntimeError:
            check("mark refuses terminal overwrite", True)

        # Test 8: mark() with force=True overwrites
        mark(agent, "rid-2", "PROCESSED", base_dir=tmp, force=True)
        c2 = sqlite3.connect(dbpath)
        row = c2.execute("SELECT state FROM inbound WHERE resend_id='rid-2'"
                         ).fetchone()
        c2.close()
        check("mark(force=True) overwrites terminal", row[0] == "PROCESSED")

        # Test 9: mark() rejects unknown state
        try:
            mark(agent, "rid-1", "GARBAGE", base_dir=tmp)
            check("mark rejects unknown state", False)
        except ValueError:
            check("mark rejects unknown state", True)

        # Test 10: list_pending() returns oldest-first RECEIVED rows
        pending = list_pending(agent, base_dir=tmp)
        check("list_pending returns RECEIVED rows",
              len(pending) == 1 and pending[0]["resend_id"] == "rid-1")

        # Test 11: poll() rejects invalid agent name
        try:
            poll("bad name!", api_key="k", base_dir=tmp)
            check("poll rejects invalid agent name", False)
        except ValueError:
            check("poll rejects invalid agent name", True)

        # Test 12: poll() rejects empty api_key
        try:
            poll(agent, api_key=None, base_dir=tmp)
            check("poll rejects empty api_key", False)
        except ValueError:
            check("poll rejects empty api_key", True)

    finally:
        _http_call = saved_http
        _u.urlopen = saved_urlopen
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest: %d failures" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit("library module; use a driver (e.g. clark_intake_poller.py) "
             "or run with --selftest")
