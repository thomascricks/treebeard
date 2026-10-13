#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
ask_contract.py -- the versioned MACHINE contract of `ask.py --json`.

FC04 (2026-10-01): regfix2 made ask.py's human output operator-clean (no
status line, no separator). Four automation callers had been parsing that
human output and broke: one read every turn as failed, one read a turn as
SUCCESS with an EMPTY body. Operator rulings 2026-10-01: human output and
machine contracts are separate channels; automation must not depend on
operator output; every consumer binds to a declared, versioned contract.

Contract v1. `ask.py --json` writes EXACTLY one JSON object to stdout and
nothing else (diagnostics follow the regfix2 rules: logs, not stdout):

  contract_version  int   1
  ok                bool  the turn produced an answer
  agent             str
  reply             str   the answer text ("" when not ok)
  error             str|null
  exit_code         int   equals the process exit code (0, 1, or 3)
  turn_id           str|null
  thread_id         str|null
  mission_id        str|null
  mission_sel       str|null
  blocked           bool  blocked operator assignment (exit 3)
  receipts          list  BOX-09 dispatch receipts presented by this turn,
                          as dispatch.peek_receipts() returns them ([] when
                          not ok). ask.py NEVER acknowledges them in --json
                          mode: the consumer acknowledges after it has
                          presented them (meet: after rendering; automation:
                          on machine delivery, recorded as such -- ruling
                          C3b(i) 2026-10-01).
  notices           list  truth notices (narration / unsourced-claim
                          stamps), strings ([] when not ok)
  actionable        list  remaining operator-actionable lines (dispatch
                          execution results, Meeseeks events, the
                          diagnostics-log pointer of a failed turn), strings
Diagnostics (status line, router/triage/selector telemetry, WARN lines,
tracebacks) never enter the object (line classes:
evidence/fc04-line-classes.md).

Consumers call run() / parse(). A turn is SUCCESSFUL only when every one of
these holds; anything else is a FAILED turn, never an empty success:
  - stdout is one JSON object with exactly the v1 keys and types;
  - contract_version == 1, ok is true, blocked is false;
  - exit_code == the process return code == 0;
  - reply is non-empty after stripping whitespace.
"""
import json
import subprocess
import sys

CONTRACT_VERSION = 1
FLAG = "--json"

# key -> allowed types
SCHEMA = (
    ("contract_version", (int,)),
    ("ok", (bool,)),
    ("agent", (str,)),
    ("reply", (str,)),
    ("error", (str, type(None))),
    ("exit_code", (int,)),
    ("turn_id", (str, type(None))),
    ("thread_id", (str, type(None))),
    ("mission_id", (str, type(None))),
    ("mission_sel", (str, type(None))),
    ("blocked", (bool,)),
    ("receipts", (list,)),
    ("notices", (list,)),
    ("actionable", (list,)),
)
# Receipt row keys, as dispatch._row_to_receipt() builds them.
RECEIPT_KEYS = ("id", "recorded_utc", "agent", "operation", "args", "state",
                "exit", "result", "executed_utc", "authority")
KEYS = tuple(k for k, _ in SCHEMA)

# Selftest / acceptance seam: fn(argv, input, timeout, env) ->
# (returncode, stdout, stderr). None = real subprocess.
_RUNNER = None
# Acknowledgement seam: fn(agent, thread_id, ids, presented_to) -> list of
# acknowledged ids. None = dispatch.acknowledge().
_ACK = None


def build(agent, ok, reply, error, exit_code, turn_id=None, thread_id=None,
          mission_id=None, mission_sel=None, blocked=False, receipts=None,
          notices=None, actionable=None):
    """The one v1 object ask.py emits. Producer side of the contract."""
    def _s(v):
        return None if v is None else str(v)
    return {
        "contract_version": CONTRACT_VERSION,
        "ok": bool(ok),
        "agent": str(agent or ""),
        "reply": str(reply or "") if ok else "",
        "error": _s(error),
        "exit_code": int(exit_code),
        "turn_id": _s(turn_id),
        "thread_id": _s(thread_id),
        "mission_id": _s(mission_id),
        "mission_sel": _s(mission_sel),
        "blocked": bool(blocked),
        "receipts": [dict((k, r.get(k)) for k in RECEIPT_KEYS)
                     for r in (receipts or [])] if ok else [],
        "notices": [str(n) for n in (notices or []) if n] if ok else [],
        "actionable": [str(a) for a in (actionable or []) if a],
    }


def dumps(obj):
    """Serialized form: one line, ASCII, newline-terminated."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True) + "\n"


class Result(object):
    """Consumer view of one turn. ok=True only for a contract-valid,
    successful, non-empty reply."""
    def __init__(self, ok, reply="", error="", obj=None, returncode=None,
                 timed_out=False, stderr="", stdout=""):
        self.ok = bool(ok)
        self.reply = reply if self.ok else ""
        self.error = "" if self.ok else (error or "failed")
        self.obj = obj
        self.returncode = returncode
        self.timed_out = timed_out
        self.stderr = stderr or ""
        self.stdout = stdout or ""   # raw, for diagnostics only; never parsed
        o = obj if (self.ok and isinstance(obj, dict)) else {}
        self.receipts = list(o.get("receipts") or [])
        self.notices = list(o.get("notices") or [])
        self.actionable = list(o.get("actionable") or [])
        self._acked = None

    def receipt_ids(self):
        return [r["id"] for r in self.receipts]

    def acknowledge(self, presented_to):
        """C3b(i)/C3(a): acknowledge this turn's receipts ONCE, and only for
        a successful contract parse. presented_to names who they were
        presented to (e.g. "machine:will", "operator:meet"). Returns the ids
        acknowledged; never raises (an acknowledgement failure leaves the
        receipts unacknowledged, so they re-present: at-least-once)."""
        if not self.ok or self._acked is not None:
            return []
        self._acked = []
        ids = self.receipt_ids()
        if not ids:
            return []
        o = self.obj or {}
        try:
            fn = _ACK
            if fn is None:
                import dispatch  # noqa: PLC0415
                fn = (lambda a, t, i, p:
                      dispatch.acknowledge(a, t, i, presented_to=p))
            self._acked = list(fn(o.get("agent"), o.get("thread_id"), ids,
                                  presented_to) or [])
        except Exception as e:  # noqa: BLE001
            print("WARN: receipt acknowledgement failed (%s)" % (e,),
                  file=sys.stderr)
        return self._acked

    def status(self):
        """One-line status for caller logs (not the human status line)."""
        o = self.obj or {}
        return ("[ask-json v%s ok=%s rc=%s agent=%s turn=%s mission=%s "
                "sel=%s err=%s]" % (
                    o.get("contract_version"), self.ok, self.returncode,
                    o.get("agent"), o.get("turn_id"), o.get("mission_id"),
                    o.get("mission_sel"), self.error[:160] or "none"))

    def triple(self):
        """(ok, status, body) -- the han_turn/clark_turn shape."""
        return self.ok, self.status(), self.reply


def _fail(why, obj=None, rc=None, stderr=""):
    return Result(False, error=why, obj=obj, returncode=rc, stderr=stderr)


def parse(stdout, returncode, stderr=""):
    """Validate one `ask.py --json` run. Never raises."""
    r = _parse(stdout, returncode, stderr)
    r.stdout = stdout or ""
    return r


def _parse(stdout, returncode, stderr):
    text = (stdout or "").strip()
    if not text:
        return _fail("contract: empty stdout", rc=returncode, stderr=stderr)
    try:
        obj = json.loads(text)
    except ValueError as e:
        # Covers non-JSON, a second object, and extra stdout noise.
        return _fail("contract: stdout is not one JSON object (%s)"
                     % str(e)[:80], rc=returncode, stderr=stderr)
    if not isinstance(obj, dict):
        return _fail("contract: top level is not an object", rc=returncode,
                     stderr=stderr)
    missing = [k for k in KEYS if k not in obj]
    extra = sorted(k for k in obj if k not in KEYS)
    if missing or extra:
        return _fail("contract: keys missing=%s extra=%s" % (missing, extra),
                     obj, returncode, stderr)
    for k, types in SCHEMA:
        v = obj[k]
        # bool is an int subclass: an int field must not accept a bool.
        if (bool not in types and isinstance(v, bool)) \
                or not isinstance(v, types):
            return _fail("contract: bad type for %s" % k, obj, returncode,
                         stderr)
    for i, r in enumerate(obj["receipts"]):
        if not isinstance(r, dict) or set(r) != set(RECEIPT_KEYS) \
                or isinstance(r.get("id"), bool) \
                or not isinstance(r.get("id"), int):
            return _fail("contract: bad receipt %d" % i, obj, returncode,
                         stderr)
    for k in ("notices", "actionable"):
        if not all(isinstance(x, str) for x in obj[k]):
            return _fail("contract: bad %s entry" % k, obj, returncode,
                         stderr)
    if obj["contract_version"] != CONTRACT_VERSION:
        return _fail("contract: version %r != %d"
                     % (obj["contract_version"], CONTRACT_VERSION),
                     obj, returncode, stderr)
    if obj["exit_code"] != returncode:
        return _fail("contract: exit_code %r != process rc %r"
                     % (obj["exit_code"], returncode), obj, returncode,
                     stderr)
    if obj["blocked"]:
        return _fail("blocked: %s" % (obj["error"] or "assignment refused"),
                     obj, returncode, stderr)
    if not obj["ok"]:
        return _fail("not ok: %s" % (obj["error"] or "no error given"),
                     obj, returncode, stderr)
    if returncode != 0:
        return _fail("ok with nonzero rc %r" % returncode, obj, returncode,
                     stderr)
    if not obj["reply"].strip():
        return _fail("empty reply", obj, returncode, stderr)
    return Result(True, reply=obj["reply"], obj=obj, returncode=returncode,
                  stderr=stderr)


def run(argv, input=None, timeout=600, env=None):
    """Run ask.py with --json and parse. Never raises for turn failures.
    Result.timed_out / Result.returncode let callers keep their existing
    handling of timeouts and nonzero exits."""
    argv = list(argv)
    if FLAG not in argv:
        # Insert before any trailing positional prompt: right after ask.py.
        idx = next((i + 1 for i, a in enumerate(argv)
                    if str(a).endswith("ask.py")), len(argv))
        argv.insert(idx, FLAG)
    try:
        if _RUNNER is not None:
            rc, out, err = _RUNNER(argv, input, timeout, env)
        else:
            p = subprocess.run(argv, input=input, capture_output=True,
                               text=True, timeout=timeout, env=env)
            rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        r = _fail("timed out after %ss" % timeout)
        r.timed_out = True
        return r
    except Exception as e:  # noqa: BLE001
        return _fail("launch failed: %s: %s" % (type(e).__name__,
                                                str(e)[:160]))
    return parse(out, rc, err)


# --- selftest (offline; explicit failure counter, no asserts) ---------------

def selftest():
    fails = 0
    n = 0

    def check(label, cond):
        nonlocal fails, n
        n += 1
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        fails += 0 if cond else 1

    good = build("clark", True, "ANSWER", None, 0, "t1", "th", "m1", "lex")
    s = dumps(good)
    check("build emits exactly the v1 keys", tuple(sorted(good))
          == tuple(sorted(KEYS)))
    check("dumps is one line", s.count("\n") == 1 and s.endswith("\n"))
    r = parse(s, 0)
    check("valid success parses ok", r.ok and r.reply == "ANSWER")
    check("triple shape", r.triple()[0] is True and r.triple()[2] == "ANSWER")

    def bad(label, out, rc=0):
        rr = parse(out, rc)
        check(label, rr.ok is False and rr.reply == "" and rr.error)

    bad("empty stdout fails", "")
    bad("malformed JSON fails", "{not json")
    bad("extra stdout noise fails", "[agent=clark ok=True]\n" + s)
    bad("second JSON line fails", s + s)
    bad("regfix2 diagnostics line fails",
        s + "(diagnostics: /x/cli-2026-10-01.log)\n")
    bad("human answer text fails", "ANSWER-TEXT\n")
    bad("ok with empty reply fails",
        dumps(build("clark", True, "", None, 0)))
    bad("ok with whitespace reply fails",
        dumps(build("clark", True, " \n\t", None, 0)))
    bad("not ok fails", dumps(build("clark", False, "x", "boom", 0)))
    bad("blocked fails", dumps(build("clark", False, "", "BLOCKED", 3,
                                     blocked=True)), rc=3)
    bad("exit_code/rc mismatch fails", s, rc=1)
    o = dict(good); o.pop("turn_id")
    bad("missing key fails", json.dumps(o))
    o = dict(good); o["extra"] = 1
    bad("extra key fails", json.dumps(o))
    o = dict(good); o["contract_version"] = 2
    bad("unknown version fails", json.dumps(o))
    o = dict(good); o["ok"] = 1
    bad("non-bool ok fails", json.dumps(o))
    o = dict(good); o["exit_code"] = False
    bad("bool exit_code fails", json.dumps(o))
    o = dict(good); o["reply"] = None
    bad("null reply fails", json.dumps(o))
    bad("top-level array fails", "[1]")

    # C3 / C3b: receipts, notices, actionable; acknowledgement discipline.
    rc1 = dict((k, None) for k in RECEIPT_KEYS)
    rc1.update(id=7, operation="serpens.search", state="DONE")
    rich = build("clark", True, "R", None, 0, "t", "th-1", receipts=[rc1],
                 notices=["[NOTICE]"], actionable=["DISPATCH #7 x -> DONE"])
    rr = parse(dumps(rich), 0)
    check("receipts/notices/actionable round-trip",
          rr.ok and rr.receipt_ids() == [7] and rr.notices == ["[NOTICE]"]
          and rr.actionable == ["DISPATCH #7 x -> DONE"])
    o = dict(rich); o["receipts"] = [{"id": "7"}]
    bad("malformed receipt fails", json.dumps(o))
    o = dict(rich); o["notices"] = [3]
    bad("non-string notice fails", json.dumps(o))
    o = dict(rich); o.pop("receipts")
    bad("missing receipts key fails", json.dumps(o))
    check("not-ok object carries no receipts/notices",
          build("c", False, "", "e", 0, receipts=[rc1],
                notices=["n"])["receipts"] == []
          and build("c", False, "", "e", 0, notices=["n"])["notices"] == [])
    global _ACK
    saved_ack = _ACK
    calls = []
    _ACK = lambda a, t, i, p: calls.append((a, t, list(i), p)) or list(i)
    try:
        rr = parse(dumps(rich), 0)
        first = rr.acknowledge("machine:test")
        second = rr.acknowledge("machine:test")
        check("acknowledge: once, with agent/thread/ids/presented_to",
              first == [7] and second == [] and calls
              == [("clark", "th-1", [7], "machine:test")])
        del calls[:]
        rf = parse(dumps(build("clark", True, "", None, 0, "t", "th-1",
                               receipts=[rc1])), 0)
        rf.acknowledge("machine:test")
        rm = parse("{oops", 0)
        rm.acknowledge("machine:test")
        check("acknowledge: never on a failed or unparseable turn",
              calls == [] and rf.ok is False and rm.ok is False)
        rn = parse(dumps(build("clark", True, "R", None, 0, "t", "th")), 0)
        rn.acknowledge("machine:test")
        check("acknowledge: no receipts -> no call", calls == [])

        def _boom(a, t, i, p):
            raise RuntimeError("store down")
        _ACK = _boom
        rb = parse(dumps(rich), 0)
        check("acknowledge failure -> [] and no raise",
              rb.acknowledge("machine:test") == [])
    finally:
        _ACK = saved_ack

    global _RUNNER
    saved = _RUNNER
    try:
        seen = {}

        def fake(argv, inp, timeout, env):
            seen["argv"] = argv
            return 0, s, ""
        _RUNNER = fake
        r = run(["py", "/x/ask.py", "--agent", "clark", "hello"])
        check("run inserts --json after ask.py",
              seen["argv"] == ["py", "/x/ask.py", "--json", "--agent",
                               "clark", "hello"] and r.ok)
        run(["py", "/x/ask.py", "--json", "x"])
        check("run does not duplicate --json",
              seen["argv"].count("--json") == 1)

        def to(argv, inp, timeout, env):
            raise subprocess.TimeoutExpired(argv, timeout)
        _RUNNER = to
        r = run(["py", "ask.py"], timeout=5)
        check("timeout -> failed, timed_out set", not r.ok and r.timed_out)

        def boom(argv, inp, timeout, env):
            raise OSError("no such file")
        _RUNNER = boom
        r = run(["py", "ask.py"])
        check("launch error -> failed, no raise", not r.ok and r.error)
    finally:
        _RUNNER = saved
    print("%d/%d passed" % (n - fails, n))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit("library module; run with --selftest")
