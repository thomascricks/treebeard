#!/usr/bin/env python3
# -*- coding: ascii -*-
"""dispatch_exec.py -- executes dispatches recorded by dispatch.py.

Runs AFTER the turn, never inside it. Owns NO policy: dispatch.py already
refused anything not permitted, and each underlying command owns its own
gates (Meeseeks candidate approval, budget hard-stop, constitution).
This module only maps a permitted operation name to the real command an
operator would run, executes it as a subprocess with a bounded timeout,
and records the outcome.

Operations that approve, deposit, publish, export, delete, or mutate
configuration have no entry here AND no policy line. Both absences are
deliberate; either alone would be sufficient.
"""
import os
import subprocess
import sys

LIB = os.environ.get("TB_LIB", "/opt/treebeard/shared/lib")
sys.path.insert(0, LIB)
import dispatch  # noqa: E402

# 2026-08-31 (RC4): default to the PUBLISHED board module beside this file.
# The previous default named an operator home-directory helper that no
# public install creates, so every board dispatch on a clean install
# recorded FAILED "executor error", and the path tripped the publication
# scan (G4). On the production host the two copies are byte-identical by
# ruling (board sync pair), so behaviour there is unchanged.
BOARD = os.environ.get("DISPATCH_BOARD", os.path.join(LIB, "board.py"))
MEESEEKS = os.environ.get("DISPATCH_MEESEEKS",
                          "/opt/treebeard/shared/meeseeks/meeseeks")
PY = os.environ.get("DISPATCH_PY", "/opt/treebeard/shared/venv/bin/python3")
SERPENS = os.environ.get("DISPATCH_SERPENS", os.path.join(LIB, "serpens.py"))
TIMEOUT = int(os.environ.get("DISPATCH_TIMEOUT", "120"))


# meeseeks.spawn REMOVED 2026-08-31 (S-BRIDGE-1 ruling b). The dispatch
# policy authorised it, but meeseeks.py has no `spawn` subcommand and the
# wrapper passes "$@" through untouched -- so an authorised operation named
# a command that does not exist. It failed loudly and created zero projects,
# but an authorised-yet-nonexistent operation is a policy defect.
#
# Do NOT reintroduce it. Commissioning belongs to the reflector, which
# maintains board<->project identity; a second commissioning path that does
# not is exactly what the S-BRIDGE-1 architectural invariant forbids.
#
# NOTE: with this gone, the MEESEEKS global has no executor mapping and the
# fake_meeseeks fixture in _selftest is unexercised. Both are left in place
# deliberately -- removing them is unrelated cleanup, not this correction.
def _argv(operation, args, agent):
    """Map a permitted operation to a real command, or None if this
    executor has no mapping (recorded as REFUSED, never guessed at)."""
    if operation == "board.task":
        # Provenance is NOT optional: the board sorts by source and
        # operator work outranks agent work. A dispatched task must
        # never claim operator origin. `agent` comes from the dispatch
        # record, never from block content.
        return [PY, BOARD, "task", args, "--source", "agent:%s" % agent]
    if operation == "board.list":
        return [PY, BOARD, "list"]
    if operation == "serpens.search":
        # READ-ONLY web search. Authority (mission grant or scoped operator
        # exception) was checked at record time and is re-checked in
        # run_one() before this argv is executed.
        return [PY, SERPENS, "search", args]
    if operation == "serpens.grant":
        # Issuer is the dispatching agent from the RECORD, never from args.
        return [PY, SERPENS, "grant", args, "--issuer", agent]
    return None


def run_one(d):
    """Execute one recorded dispatch. Never raises; every outcome is
    recorded as a terminal state with its output. GOVERNED operations are
    re-authorized here from the stored provenance (mission may have closed,
    grant may have been revoked, exception may have expired since record);
    an exception basis is consumed ONLY on DONE."""
    auth = None
    if dispatch.governed(d["operation"]):
        try:
            prov = dispatch.provenance_of(d["id"]) or {}
            auth = dispatch.authorize(d["agent"], d["operation"], d["args"],
                                      prov.get("mission_id"), prov.get("sel"))
        except Exception as e:
            auth = {"ok": False, "reason": "authority-check-error: %s" % e}
        if not auth.get("ok"):
            dispatch.complete(d["id"], "REFUSED",
                              "authority check failed at execution: %s"
                              % auth.get("reason"))
            return "REFUSED"
    argv = _argv(d["operation"], d["args"], d["agent"])
    if argv is None:
        dispatch.complete(d["id"], "REFUSED",
                          "no executor mapping for %s" % d["operation"])
        return "REFUSED"
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=TIMEOUT)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        state = "DONE" if p.returncode == 0 else "FAILED"
        dispatch.complete(d["id"], state, "rc=%d %s" % (p.returncode, out))
        if state == "DONE" and auth and auth.get("basis") == "exception":
            try:
                dispatch.consume_exception(auth.get("exception_id"), d["id"])
            except Exception as e:
                print("WARN: exception consume failed (%s)" % (e,), file=sys.stderr)
        return state
    except subprocess.TimeoutExpired:
        dispatch.complete(d["id"], "FAILED", "timeout after %ds" % TIMEOUT)
        return "FAILED"
    except Exception as e:
        dispatch.complete(d["id"], "FAILED", "executor error: %s" % (e,))
        return "FAILED"


def drain(limit=50):
    """Execute every eligible pending dispatch, oldest first, once each.

    STORY 1: THE shared drain. Every transport -- the CLI and the web
    workspace -- calls this one function, so dispatch execution cannot
    drift into two behaviours. This is the loop that used to live inline
    in ask.main(), relocated rather than rewritten.

    It adds no policy and no execution logic of its own. Eligibility is
    exactly dispatch.pending()'s PENDING filter, and every terminal state
    is run_one()'s -- which always completes the record, on every path,
    so a drained record can never be returned by pending() again.

    Returns [(record, terminal_state)]. Presentation belongs to the
    caller, because it is the only transport-specific part.
    """
    return [(d, run_one(d)) for d in dispatch.pending(limit)]


def main():
    """CLI presentation over the shared drain."""
    results = drain()
    if not results:
        print("dispatch: nothing pending")
        return 0
    for d, state in results:
        print("dispatch #%d %s %s -> %s"
              % (d["id"], d["agent"], d["operation"], state))
    return 0


def _selftest():
    """Offline. Fake executables in a temp dir; dispatch.complete and
    dispatch.pending are patched in-process so the real dispatch store is
    never opened. Exit code = failure count. (2026-08-31: added when the
    regression harness was found not to cover this module.)"""
    import io, stat, tempfile, shutil, contextlib
    global BOARD, MEESEEKS, PY, TIMEOUT
    failures = 0

    def check(label, cond):
        nonlocal failures
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            failures += 1

    tmp = tempfile.mkdtemp(prefix="dispatch_exec_selftest_")
    saved = (BOARD, MEESEEKS, PY, TIMEOUT, dispatch.complete, dispatch.pending)
    saved_env = {k: os.environ.get(k) for k in ("FAKE_LOG", "FAKE_MODE")}
    completed = []
    log = os.path.join(tmp, "argv.log")
    try:
        board = os.path.join(tmp, "fake_board.py")
        with open(board, "w") as f:
            f.write("\n".join([
                "import os, sys, time",
                "lg = os.environ.get('FAKE_LOG')",
                "if lg:",
                "    open(lg, 'a').write(' '.join(sys.argv[1:]) + chr(10))",
                "mode = os.environ.get('FAKE_MODE', 'ok')",
                "if mode == 'sleep':",
                "    time.sleep(5)",
                "if mode == 'fail':",
                "    print('boom'); sys.exit(3)",
                "print('ok')", ""]))
        mk = os.path.join(tmp, "fake_meeseeks")
        with open(mk, "w") as f:
            f.write("#!/bin/sh\necho spawn \"$@\"\n")
        os.chmod(mk, stat.S_IRWXU)
        BOARD, MEESEEKS, PY, TIMEOUT = board, mk, sys.executable, 1
        dispatch.complete = lambda did, state, result: completed.append((did, state, result))
        os.environ["FAKE_LOG"] = log
        os.environ["FAKE_MODE"] = "ok"

        # --- mapping table ---
        check("board.task maps with agent provenance, never operator",
              _argv("board.task", "fix it", "clark")
              == [PY, board, "task", "fix it", "--source", "agent:clark"])
        check("board.list maps",
              _argv("board.list", "", "clark") == [PY, board, "list"])
        check("meeseeks.spawn has NO executor mapping",
              _argv("meeseeks.spawn", "job", "clark") is None)
        for op in ("meeseeks.spawn", "meeseeks.approve", "persona.deposit",
                   "publish", "export", "board.delete", "config.set", ""):
            check("no executor mapping for %r" % op,
                  _argv(op, "x", "clark") is None)

        # --- run_one terminal states ---
        d = {"id": 1, "agent": "clark", "operation": "board.delete", "args": "x"}
        check("unmapped op -> REFUSED and recorded with the op name",
              run_one(d) == "REFUSED" and completed[-1][0] == 1
              and completed[-1][1] == "REFUSED" and "board.delete" in completed[-1][2])
        check("refusal executed nothing", not os.path.exists(log))

        d = {"id": 2, "agent": "clark", "operation": "board.task", "args": "fix it"}
        check("board.task rc=0 -> DONE with rc recorded",
              run_one(d) == "DONE" and completed[-1][1] == "DONE"
              and completed[-1][2].startswith("rc=0"))
        check("subprocess received agent provenance verbatim",
              open(log).read().strip() == "task fix it --source agent:clark")

        d = {"id": 3, "agent": "clark", "operation": "meeseeks.spawn", "args": "job"}
        check("meeseeks.spawn -> REFUSED, recorded with the op name",
              run_one(d) == "REFUSED" and completed[-1][0] == 3
              and completed[-1][1] == "REFUSED"
              and "meeseeks.spawn" in completed[-1][2])
        check("refused spawn executed nothing",
              "spawn" not in open(log).read())

        os.environ["FAKE_MODE"] = "fail"
        d = {"id": 4, "agent": "clark", "operation": "board.list", "args": ""}
        check("nonzero rc -> FAILED with rc and output",
              run_one(d) == "FAILED" and completed[-1][2].startswith("rc=3")
              and "boom" in completed[-1][2])

        os.environ["FAKE_MODE"] = "sleep"
        check("timeout -> FAILED recorded as timeout",
              run_one(d) == "FAILED" and "timeout" in completed[-1][2])
        os.environ["FAKE_MODE"] = "ok"

        PY = os.path.join(tmp, "no-such-interpreter")
        check("launch failure -> FAILED as executor error, never raised",
              run_one(d) == "FAILED" and "executor error" in completed[-1][2])
        PY = sys.executable

        # --- governed ops (Serpens story): re-authorize, consume on DONE only ---
        global SERPENS
        _serp_prev = SERPENS
        SERPENS = board   # the fake logs argv and honours FAKE_MODE
        _saved_auth = (dispatch.governed, dispatch.authorize,
                       dispatch.provenance_of, dispatch.consume_exception)
        consumed = []
        dispatch.governed = lambda op: op == "serpens.search"
        dispatch.provenance_of = lambda did: {"mission_id": "m-x", "sel": "m-x:model/2"}
        dispatch.consume_exception = lambda eid, did=None: consumed.append((eid, did)) or True
        try:
            check("serpens.search maps to the read-only executor",
                  _argv("serpens.search", "find it", "clark") == [PY, board, "search", "find it"])
            check("serpens.grant maps with issuer from the record, never args",
                  _argv("serpens.grant", "agent=clark op=serpens.search purpose=x", "treebeard")
                  == [PY, board, "grant", "agent=clark op=serpens.search purpose=x",
                      "--issuer", "treebeard"])
            dispatch.authorize = lambda *a, **k: {"ok": False, "reason": "no-authority"}
            open(log, "w").close()
            d = {"id": 21, "agent": "clark", "operation": "serpens.search", "args": "find it"}
            check("governed op refused at execution when authority lapsed; nothing ran",
                  run_one(d) == "REFUSED" and "authority check failed" in completed[-1][2]
                  and open(log).read().strip() == "")
            dispatch.authorize = lambda *a, **k: {"ok": True, "basis": "exception",
                                                  "exception_id": 7, "reason": "operator-exception"}
            os.environ["FAKE_MODE"] = "fail"
            check("FAILED execution consumes NO exception",
                  run_one(d) == "FAILED" and consumed == [])
            os.environ["FAKE_MODE"] = "ok"
            check("DONE on exception basis consumes exactly once",
                  run_one(d) == "DONE" and consumed == [(7, 21)])
            dispatch.authorize = lambda *a, **k: {"ok": True, "basis": "mission",
                                                  "mission_id": "m-x", "exception_id": None}
            check("DONE on mission basis consumes nothing",
                  run_one(d) == "DONE" and consumed == [(7, 21)])
        finally:
            (dispatch.governed, dispatch.authorize,
             dispatch.provenance_of, dispatch.consume_exception) = _saved_auth
            SERPENS = _serp_prev

        # --- main ---
        dispatch.pending = lambda limit=50: []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main()
        check("main with nothing pending -> 0",
              rc == 0 and "nothing pending" in buf.getvalue())
        dispatch.pending = lambda limit=50: [
            {"id": 9, "agent": "han", "operation": "board.list", "args": ""}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main()
        check("main runs pending and reports the terminal state",
              rc == 0 and "dispatch #9 han board.list -> DONE" in buf.getvalue())

        # --- drain: THE shared loop both transports use (STORY 1) ---
        # Eligibility is dispatch.pending()'s PENDING filter, so a record
        # that has completed can never be handed back. The fake below
        # reproduces exactly that contract: completing removes the record.
        _done = set()

        def _fake_complete(did, state, result):
            completed.append((did, state, result))
            _done.add(did)

        served = [
            {"id": 11, "agent": "han", "operation": "board.list", "args": ""},
            {"id": 12, "agent": "clark", "operation": "board.task", "args": "t"},
            {"id": 13, "agent": "clark", "operation": "board.delete", "args": "x"},
        ]
        dispatch.complete = _fake_complete
        dispatch.pending = lambda limit=50: [
            d for d in served if d["id"] not in _done][:limit]
        os.environ["FAKE_MODE"] = "ok"
        del completed[:]
        res = drain()
        check("G drain handles every pending record, oldest first",
              [(d["id"], s) for d, s in res]
              == [(11, "DONE"), (12, "DONE"), (13, "REFUSED")])
        check("G each record executed exactly once",
              sorted(c[0] for c in completed) == [11, 12, 13]
              and len(completed) == 3)
        check("B a completed record is never drained a second time",
              drain() == [] and len(completed) == 3)

        # H: a failing command must record FAILED, never a silent success.
        _done.clear()
        del completed[:]
        served[:] = [{"id": 14, "agent": "han", "operation": "board.list",
                      "args": ""}]
        os.environ["FAKE_MODE"] = "fail"
        res = drain()
        check("H drained failure records FAILED with the rc, not success",
              [s for _r, s in res] == ["FAILED"]
              and completed[-1][1] == "FAILED"
              and completed[-1][2].startswith("rc=3"))
        os.environ["FAKE_MODE"] = "ok"
    finally:
        BOARD, MEESEEKS, PY, TIMEOUT, dispatch.complete, dispatch.pending = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("ALL PASS" if failures == 0 else "%d FAILED" % failures))
    return failures


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(_selftest())
    sys.exit(main())
