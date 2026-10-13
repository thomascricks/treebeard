#!/usr/bin/env python3
# opchat.py -- UI-CHAT increment A: authenticated operator session, direct
# agent conversations, and history over the EXISTING durable record.
#
# Conversation truth lives in shortterm's day log (the same record ask.py
# writes on every governed turn, CLI or web). This module adds an INDEX for
# navigation, never a second memory system: if the index is deleted the
# conversations still exist, and rebuilding it loses nothing but ordering
# convenience.
#
# Auth: single operator, loopback only. A token is generated at first start
# and printed to the service log; the operator pastes it once. Sessions are
# signed cookies. Every state-changing request needs a CSRF token. No agent
# invocation, and therefore no model spend, happens before authentication.
import hashlib
import hmac
import html
import json
import os as _tb_os
import secrets
import re
import urllib.parse
import sys
import time

_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_LIB = f"{_TB_BASE}/shared/lib"
_TREEBEARD = f"{_TB_BASE}/treebeard"
for _p in (_LIB, _TREEBEARD):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import agents

UI_DIR = f"{_TB_BASE}/shared/ui"
SECRET_PATH = f"{UI_DIR}/session_secret"
TOKEN_PATH = f"{UI_DIR}/operator_token"
INDEX_PATH = f"{UI_DIR}/conversations.json"
THREADS_PATH = f"{UI_DIR}/threads.json"
SESSION_TTL = 12 * 3600

THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def default_thread(agent):
    """Stable current-workspace thread until the thread UI lands."""
    return "web-default:%s" % str(agent or "").strip().lower()


def resolve_thread(agent, supplied=None):
    """Resolve an explicit thread or the agent's current default.

    Thread identity is independent of agent identity; the agent is used
    only to choose today's default when no explicit thread was supplied.
    """
    t = str(supplied or "").strip()
    if t and THREAD_RE.fullmatch(t):
        return t
    return default_thread(agent)


def chat_url(agent, thread_id=None):
    t = resolve_thread(agent, thread_id)
    return "/chat?" + urllib.parse.urlencode({"agent": agent, "thread": t})


# --- auth ------------------------------------------------------------------

def _read_or_create(path, mode=0o600):
    """Secrets are created once, 0600, owned by the service user."""
    try:
        with open(path) as fh:
            v = fh.read().strip()
            if v:
                return v, False
    except OSError:
        pass
    v = secrets.token_urlsafe(32)
    _tb_os.makedirs(_tb_os.path.dirname(path), exist_ok=True)
    fd = _tb_os.open(path, _tb_os.O_WRONLY | _tb_os.O_CREAT | _tb_os.O_TRUNC, mode)
    with _tb_os.fdopen(fd, "w") as fh:
        fh.write(v + "\n")
    return v, True


def session_secret():
    return _read_or_create(SECRET_PATH)[0]


def operator_token():
    return _read_or_create(TOKEN_PATH)[0]


def _sign(payload):
    return hmac.new(session_secret().encode(), payload.encode(),
                    hashlib.sha256).hexdigest()[:32]


def make_session():
    """Signed, expiring session value. Operator identity is durable and
    auditable: every governed action records actor 'operator'."""
    exp = str(int(time.time()) + SESSION_TTL)
    return "%s.%s" % (exp, _sign(exp))


def valid_session(cookie):
    if not cookie or "." not in str(cookie):
        return False
    exp, sig = str(cookie).rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def csrf_token(session):
    return _sign("csrf:" + str(session))


def valid_csrf(session, token):
    return bool(token) and hmac.compare_digest(str(token),
                                               csrf_token(session))


def check_token(supplied):
    return bool(supplied) and hmac.compare_digest(str(supplied).strip(),
                                                  operator_token())


# --- conversation index ----------------------------------------------------

def _load_index():
    try:
        with open(INDEX_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_index(rows):
    _tb_os.makedirs(UI_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rows[-500:], fh, indent=1)
    _tb_os.replace(tmp, INDEX_PATH)


def record_turn(agent, prompt, reply, ok=True, tier=None, model=None,
                error=None, thread_id=None, turn_id=None,
                parent_turn_id=None, source="web",
                receipt_evidence=None, receipt_ids=None, status=None,
                origin=None):
    """Append one direct workspace turn to the navigation index.

    Returns the persisted row on success and None if persistence FAILED.
    BOX-09 acknowledgment depends on knowing the evidence actually
    reached disk: acknowledging a receipt whose only operator-visible
    copy was never written would destroy it. The caller must not infer
    success from the absence of an exception."""
    thread_id = resolve_thread(agent, thread_id)
    rows = _load_index()
    rows.append({
        "kind": "direct",
        "agent": agent,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "parent_turn_id": parent_turn_id,
        "source": source,
        "origin": origin,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prompt": prompt,
        "reply": reply,
        "ok": bool(ok),
        "tier": tier,
        "model": model,
        "error": error,
        # BOX-09: deterministic evidence persisted WITH the turn, so a
        # browser disconnect after the POST cannot cost the operator
        # their only copy -- it renders again when the page reloads.
        "receipt_evidence": receipt_evidence or "",
        "receipt_ids": list(receipt_ids or []),
        # WEB-STATUS: the canonical status string ask.run_turn()
        # already composes, persisted VERBATIM. No parsing, no
        # reformatting, no second grammar -- the CLI prints this same
        # string, and the web operator must see the same evidence.
        # Rows written before WEB-STATUS have no key at all; readers
        # use .get(), so existing history renders exactly as before.
        "status": status or "",
    })
    try:
        _save_index(rows)
    except Exception as exc:
        print("WARN: conversation index not persisted (%s: %s)"
              % (type(exc).__name__, exc), file=sys.stderr)
        return None
    return rows[-1]


def conversation(agent, thread_id=None, limit=50):
    thread_id = resolve_thread(agent, thread_id)
    return [
        r for r in _load_index()
        if r.get("kind") == "direct"
        and r.get("agent") == agent
        and resolve_thread(agent, r.get("thread_id")) == thread_id
    ][-limit:]


def conversations():
    """Most recent activity per agent, for the sidebar."""
    latest = {}
    for r in _load_index():
        if r.get("agent"):
            latest[r["agent"]] = r
    return sorted(latest.values(), key=lambda r: r.get("ts") or "",
                  reverse=True)


def search(query, limit=40):
    """Search the conversation index. Honest scope: this searches what the
    operator said and what agents replied through this workspace."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    hits = [r for r in _load_index()
            if q in str(r.get("prompt", "")).lower()
            or q in str(r.get("reply", "")).lower()]
    return list(reversed(hits))[:limit]


# --- thread registry -------------------------------------------------------
# THREADS story: durable thread identity + human labels for the web UI.
# Registry is display/navigation metadata ONLY. Conversation truth stays
# in shortterm; authority stays in mission state; nothing here is
# consulted by ask.py or any governed path. A thread that exists only in
# history (no registry row) still lists, labeled by its id -- the
# registry can be lost and rebuilt without losing a conversation.

def _load_threads():
    try:
        with open(THREADS_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_threads(rows):
    _tb_os.makedirs(UI_DIR, exist_ok=True)
    tmp = THREADS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rows, fh, indent=1)
    _tb_os.replace(tmp, THREADS_PATH)


def _default_label(agent, thread_id):
    if agent and thread_id == default_thread(agent):
        return "Workspace"
    return thread_id


def thread_label(agent, thread_id):
    """Human label for a thread. Registry wins; the default workspace
    thread has a fixed name; anything else honestly shows its id."""
    for r in _load_threads():
        if r.get("thread_id") == thread_id and r.get("label"):
            return r["label"]
    return _default_label(agent, thread_id)


def create_thread(agent, label=None):
    """Mint a durable thread for one agent. Returns the registry row on
    success and None if the agent is unknown, the id cannot be formed,
    or persistence FAILED -- a thread the operator cannot return to
    after a reload was never created, and the caller must not redirect
    into it."""
    if agents.get(agent) is None:
        return None
    a = str(agent).strip().lower()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    existing = {r.get("thread_id") for r in _load_threads()}
    tid, n = "web:%s:%s" % (a, ts), 1
    while tid in existing:
        n += 1
        tid = "web:%s:%s-%d" % (a, ts, n)
    if not THREAD_RE.fullmatch(tid):
        return None
    lab = str(label or "").strip()[:80] or time.strftime(
        "%Y-%m-%d %H:%M", time.gmtime())
    row = {"thread_id": tid, "agent": a, "label": lab,
           "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rows = _load_threads()
    rows.append(row)
    try:
        _save_threads(rows)
    except Exception as exc:
        print("WARN: thread registry not persisted (%s: %s)"
              % (type(exc).__name__, exc), file=sys.stderr)
        return None
    return row


def list_threads(agent=None):
    """Sidebar entries, one per (agent, thread), newest activity first.

    Union of three sources: registry rows (labels), threads observed in
    the conversation index (activity), and every roster agent's default
    workspace thread (legacy conversations stay first-class). Uniform
    entry shape -- kind/agent/thread_id/label/ts/url -- so a future
    meeting kind can share this same list."""
    latest = {}
    for r in _load_index():
        a, t = r.get("agent"), r.get("thread_id")
        if not a:
            continue
        t = resolve_thread(a, t)
        prev = latest.get((a, t), "")
        latest[(a, t)] = max(prev, r.get("ts") or "")
    entries = {}
    for r in _load_threads():
        a, t = r.get("agent"), r.get("thread_id")
        if not a or not t:
            continue
        entries[(a, t)] = {
            "kind": "direct", "agent": a, "thread_id": t,
            "label": r.get("label") or t,
            "ts": latest.get((a, t), ""),
            "created_ts": r.get("created_ts") or ""}
    for (a, t), ts in latest.items():
        entries.setdefault((a, t), {
            "kind": "direct", "agent": a, "thread_id": t,
            "label": _default_label(a, t), "ts": ts, "created_ts": ""})
    seed = [agent] if agent else [x["id"] for x in agents.roster()]
    for a in seed:
        t = default_thread(a)
        entries.setdefault((a, t), {
            "kind": "direct", "agent": a, "thread_id": t,
            "label": "Workspace", "ts": "", "created_ts": ""})
    rows = [e for (a, _), e in entries.items() if not agent or a == agent]
    for e in rows:
        e["url"] = chat_url(e["agent"], e["thread_id"])
    # Two-part ordering. Threads with real activity always rank above
    # never-used ones: "ts" and "created_ts" are different clocks and
    # must not compete on one axis, or a stale created thread can
    # outrank a live conversation. Ties fall back to creation, then
    # id, so the order is total and stable across renders.
    return sorted(
        rows,
        key=lambda e: (1 if e.get("ts") else 0, e.get("ts") or "",
                       e.get("created_ts") or "", e.get("thread_id")),
        reverse=True)


# --- turn ------------------------------------------------------------------

def _drain_dispatch(t):
    """Execute what this turn recorded, through the SAME shared drain the
    CLI uses.

    STORY 1 root cause: ask.run_turn() deliberately leaves dispatch
    execution to its caller, and this transport never did it -- so every
    dispatch recorded from the web stayed PENDING forever while the
    identical CLI turn executed normally.

    The gate is the CLI's, unchanged: drain only when this turn recorded a
    dispatch successfully, then take the pending queue exactly as the
    existing lifecycle contract defines it. No policy and no execution
    logic lives here -- dispatch.py already refused anything off-policy
    before recording, and dispatch_exec.run_one owns every terminal state.

    Fail-soft, as on the CLI: a drain failure must never cost the operator
    their reply. It is reported to the service log, never swallowed.
    """
    if not t.get("ok"):
        return []
    if not str(t.get("dispatch_note") or "").startswith("dispatch:ok"):
        return []
    try:
        import dispatch_exec  # noqa: PLC0415
        return [{"id": d["id"], "agent": d["agent"],
                 "operation": d["operation"], "state": st}
                for d, st in dispatch_exec.drain()]
    except Exception as exc:
        print("WARN: dispatch execution unavailable (%s: %s)"
              % (type(exc).__name__, exc), file=sys.stderr)
        return []


def send(agent, prompt, thread_id=None, origin=None):
    """One governed web turn with explicit conversation identity.
    origin (G2, 2026-09-29) is supplied by the authenticated transport
    (opsrv passes operator-web only after session + CSRF); send() never
    invents it. run_turn records anything else as automation:unknown."""
    if agents.get(agent) is None:
        return {"ok": False, "error": "no such agent: %s" % agent,
                "agent": agent, "text": None}

    thread_id = resolve_thread(agent, thread_id)

    import ask
    try:
        t = ask.run_turn(
            prompt, agent=agent,
            thread_id=thread_id,
            source="web",
            origin=origin,
        )
    except Exception as exc:
        t = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
             "agent": agent, "text": None, "tier": None, "model": None,
             "thread_id": thread_id, "turn_id": None,
             "parent_turn_id": None, "source": "web",
             "origin": origin or "automation:unknown"}

    # Deferred execution, AFTER the turn -- the same ordering as the CLI.
    t["dispatch"] = _drain_dispatch(t)

    persisted = record_turn(
        agent, prompt, t.get("text"),
        ok=t.get("ok"),
        tier=t.get("tier"),
        model=t.get("model"),
        error=t.get("error"),
        thread_id=t.get("thread_id") or thread_id,
        turn_id=t.get("turn_id"),
        parent_turn_id=t.get("parent_turn_id"),
        source=t.get("source") or "web",
        origin=t.get("origin"),
        receipt_evidence=t.get("receipt_evidence"),
        receipt_ids=t.get("receipt_ids"),
        status=t.get("status"),
    )
    # BOX-09 acknowledgment. BOTH conditions are required: a failed or
    # empty turn must acknowledge nothing even though record_turn wrote
    # a failure row, and evidence that never reached disk must stay
    # unacknowledged so it re-presents. Losing a receipt is the one
    # outcome this ordering exists to prevent.
    if t.get("ok") and persisted and t.get("receipt_ids"):
        try:
            import dispatch as _dsp  # noqa: PLC0415
            _dsp.acknowledge(agent, t.get("thread_id") or thread_id,
                             t["receipt_ids"])
        except Exception as exc:
            print("WARN: receipt acknowledgment failed, will re-present "
                  "(%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
    return t


def _selftest():
    import tempfile
    global SECRET_PATH, TOKEN_PATH, INDEX_PATH, THREADS_PATH
    tmp = tempfile.mkdtemp(prefix="opchat_")
    SECRET_PATH = tmp + "/secret"
    TOKEN_PATH = tmp + "/token"
    INDEX_PATH = tmp + "/index.json"
    THREADS_PATH = tmp + "/threads.json"

    # Secrets: created once, stable, 0600.
    s1, tok = session_secret(), operator_token()
    assert s1 and tok and session_secret() == s1
    assert _tb_os.stat(SECRET_PATH).st_mode & 0o777 == 0o600
    assert _tb_os.stat(TOKEN_PATH).st_mode & 0o777 == 0o600

    # Sessions: valid only when signed by this secret and unexpired.
    sess = make_session()
    assert valid_session(sess)
    assert not valid_session("") and not valid_session(None)
    assert not valid_session("garbage") and not valid_session("123.bad")
    exp, sig = sess.rsplit(".", 1)
    assert not valid_session("%s.%s" % (int(exp) + 1, sig)), "signature bound"
    past = str(int(time.time()) - 10)
    assert not valid_session("%s.%s" % (past, _sign(past))), "expiry enforced"

    # CSRF: bound to the session, required for state change.
    c = csrf_token(sess)
    assert valid_csrf(sess, c) and not valid_csrf(sess, "x")
    assert not valid_csrf(sess, None)
    other = "%s.%s" % (int(exp) + 60, _sign(str(int(exp) + 60)))
    assert csrf_token(other) != c, "csrf must be session-bound"
    assert not valid_csrf(other, c), "another session's csrf is rejected"

    # Token check is constant-time and exact.
    assert check_token(tok) and not check_token(tok + "x")
    assert not check_token("") and not check_token(None)

    # Index: append, read back, per-agent scoping, search.
    real = agents.existing_agents()[0]
    record_turn(real, "hello there", "hi back", tier="free", model="m")
    record_turn(real, "second", "reply two", tier="free", model="m")
    conv = conversation(real)
    assert len(conv) == 2 and conv[-1]["prompt"] == "second"
    assert conversation("nobody") == []
    assert [r["agent"] for r in conversations()] == [real]
    assert len(search("hello")) == 1 and search("")  == []
    assert len(search("reply two")) == 1
    assert search("nothingmatchesthis") == []

    # WEB-STATUS: the canonical status string round-trips VERBATIM.
    _canon = "[agent=t ok=True tier=free model=m box:unverified-no-read "\
             "memory-promise:withheld(1) mem=0,ok,none]"
    row = record_turn(real, "status turn", "reply", tier="free",
                      model="m", status=_canon)
    assert row["status"] == _canon, "status must persist unmodified"
    assert conversation(real)[-1]["status"] == _canon, "survives reload"
    # New-format quiet turn: key present, empty.
    quiet = record_turn(real, "quiet", "reply")
    assert quiet["status"] == ""
    # Actual legacy-format row: no key at all. Readers use .get().
    legacy = {"reply": "old"}
    assert not legacy.get("status")

    # A turn to an agent that does not exist never reaches the router.
    r = send("nonexistent_agent_xyz", "spend money please")
    assert r["ok"] is False and "no such agent" in r["error"]
    # --- web dispatch drain (STORY 1) ---------------------------------
    # Fake ask/dispatch_exec: the point under test is the GATE and the
    # delegation, so neither the router nor the real dispatch store is
    # touched. Execution semantics themselves are proven by the
    # dispatch_exec suite, which owns them.
    import types

    def _boom(rows):
        raise OSError("disk gone")

    turn = {}
    fake_ask = types.ModuleType("ask")
    # 2026-09-02: the fake mirrors run_turn's CURRENT signature on
    # purpose. **kw would absorb any future parameter and make this
    # suite permanently blind to a send()/run_turn() mismatch -- which
    # is the drift it exists to catch (it went red at the thread change
    # for exactly that reason, correctly).
    _sent = {}
    fake_ask.run_turn = (
        lambda prompt, agent=None, capsule=None, thread_id=None,
        source=None, parent_turn_id=None, origin=None:
        (_sent.update(origin=origin) or dict(turn)))
    sys.modules["ask"] = fake_ask
    fake_de = types.ModuleType("dispatch_exec")
    calls = []
    drained = [({"id": 7, "agent": real, "operation": "board.list",
                 "args": ""}, "DONE")]
    fake_de.drain = lambda: (calls.append(1), drained)[1]
    sys.modules["dispatch_exec"] = fake_de
    try:
        # A: a web turn that recorded an allowed dispatch drains it, once.
        turn.update({"ok": True, "text": "hi", "tier": "free", "model": "m",
                     "error": None, "dispatch_note": "dispatch:ok#7"})
        r = send(real, "do a thing")
        assert calls == [1], "web turn recording a dispatch must drain once"
        assert r["dispatch"] == [{"id": 7, "agent": real,
                                  "operation": "board.list",
                                  "state": "DONE"}], r.get("dispatch")
        assert r["text"] == "hi", "draining must not alter the reply"

        # G2 origin: send() hands the transport's origin to run_turn and
        # persists what run_turn returns; it never invents operator-web.
        _sent.clear()
        turn["origin"] = "operator-web"
        send(real, "o1", origin="operator-web")
        assert _sent.get("origin") == "operator-web", \
            "send() must hand the transport's origin to run_turn"
        assert conversation(real)[-1].get("origin") == "operator-web", \
            "origin must persist with the web turn"
        _sent.clear()
        turn.pop("origin", None)
        send(real, "o2")
        assert _sent.get("origin") is None, \
            "send() without an origin must not claim operator-web"

        # BOX-09 web lifecycle.
        turn.update({"ok": True, "text": "hi", "dispatch_note": "",
                     "receipt_evidence": "RCPT-BLOCK",
                     "receipt_ids": [41],
                     # WEB-STATUS: run_turn always returns this; send()
                     # must carry it to persistence unmodified.
                     "status": "[agent=t ok=True box:dispatch-pending]"})
        r = send(real, "x")
        _row = conversation(real)[-1]
        assert _row["status"] == "[agent=t ok=True box:dispatch-pending]",\
            "send() must persist run_turn's status verbatim"
        assert _row.get("receipt_evidence") == "RCPT-BLOCK", \
            "evidence must persist WITH the turn"
        assert _row.get("receipt_ids") == [41], "ids must persist"

        # A failed turn acknowledges nothing even though a row is written.
        turn.update({"ok": False, "text": None, "error": "boom",
                     "receipt_evidence": "RCPT-BLOCK",
                     "receipt_ids": [42]})
        r = send(real, "y")
        assert conversation(real)[-1]["ok"] is False, \
            "a failed turn must still be recorded"

        # record_turn reports persistence failure rather than raising.
        _real_save = _save_index
        try:
            globals()["_save_index"] = _boom
            assert record_turn(real, "p", "r") is None, \
                "record_turn must return None when persistence fails"
        finally:
            globals()["_save_index"] = _real_save
        turn.update({"ok": True, "text": "hi", "error": None,
                     "receipt_evidence": "", "receipt_ids": []})

        # E: a turn with no dispatch does not drain and is unchanged.
        del calls[:]
        turn["dispatch_note"] = ""
        r = send(real, "just talking")
        assert calls == [], "a turn with no dispatch must never drain"
        assert r["dispatch"] == [] and r["text"] == "hi"

        # The gate is the CLI's exactly: refused, malformed, or a failed
        # turn never drains.
        for note, ok in (("dispatch:refused", True),
                         ("dispatch:malformed", True),
                         ("dispatch:example", True),
                         ("dispatch:ok#9", False)):
            del calls[:]
            turn["dispatch_note"], turn["ok"] = note, ok
            send(real, "x")
            assert calls == [], "gate fired for %r ok=%s" % (note, ok)

        # Fail-soft: a broken drain must never cost the operator the reply.
        del calls[:]
        turn.update({"ok": True, "dispatch_note": "dispatch:ok#7"})

        def _boom():
            raise RuntimeError("store gone")

        fake_de.drain = _boom
        r = send(real, "x")
        assert r["ok"] is True and r["text"] == "hi", "reply survives"
        assert r["dispatch"] == []
    finally:
        sys.modules.pop("ask", None)
        sys.modules.pop("dispatch_exec", None)


    # --- THREADS story assertions ---------------------------------------
    a1 = agents.roster()[0]["id"]
    # Unknown agent refused; nothing persisted.
    assert create_thread("nobody-xyz") is None, "unknown agent minted"
    assert _load_threads() == [], "refused create left registry rows"
    # Create: valid id, label default, registry persisted.
    t1 = create_thread(a1)
    assert t1 and THREAD_RE.fullmatch(t1["thread_id"]), "bad minted id"
    assert t1["agent"] == a1 and t1["label"], "row incomplete"
    assert any(r["thread_id"] == t1["thread_id"] for r in _load_threads())
    # Same-second create must not collide.
    t2 = create_thread(a1, label="Named thread")
    assert t2["thread_id"] != t1["thread_id"], "id collision"
    assert thread_label(a1, t2["thread_id"]) == "Named thread"
    # Default workspace thread: fixed label, always listed.
    dt = default_thread(a1)
    assert thread_label(a1, dt) == "Workspace"
    ls = list_threads(a1)
    ids = [e["thread_id"] for e in ls]
    assert dt in ids and t1["thread_id"] in ids and t2["thread_id"] in ids, \
        "listing missing a thread"
    assert all(e["agent"] == a1 for e in ls), "foreign agent listed"
    assert all(e["url"].startswith("/chat?") and "thread=" in e["url"]
               for e in ls), "entry lacks addressable url"
    # A fresh thread has EMPTY isolated history -- never a fallback.
    assert conversation(a1, t2["thread_id"]) == [], \
        "new thread inherited history"
    # History observed in the index lists even without a registry row,
    # labeled honestly by id (registry loss loses labels, not threads).
    record_turn(a1, "p", "r", thread_id="web:%s:orphan" % a1)
    ls2 = list_threads(a1)
    orph = [e for e in ls2 if e["thread_id"] == "web:%s:orphan" % a1]
    assert orph and orph[0]["label"] == "web:%s:orphan" % a1, \
        "unregistered thread lost from listing"
    # Activity sorts above never-used threads.
    assert ls2[0]["thread_id"] == "web:%s:orphan" % a1, \
        "latest activity not first"
    print("opchat selftest: ALL PASS")


if __name__ == "__main__":
    if not __debug__:
        import sys as _sys
        print("opchat: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    _selftest()
