#!/usr/bin/env python3
"""
testrun.py -- bounded test execution for Treebeard's own turn.

The ONLY source of runnable suites is regression.SUITES (safe tier), and
the ONLY runner is regression._run_one. This module adds no command, no
shell, no second list: a suite NAME is the sole input that crosses the
interface, and it must match a registry key exactly.

Contract (boxread precedent): every call returns a dict with "status".
  OK          -- the suite(s) actually executed and produced a verdict;
                 "verdict" is PASS or FAIL and is authoritative.
  UNVERIFIED  -- nothing usable ran (denied name, timeout, launch error,
                 missing file, budget exhausted). Never a verdict of PASS.

Bounds: per-suite timeout = regression.PER_SUITE_TIMEOUT; a whole-run
wall budget (TESTRUN_BUDGET) after which remaining suites are reported
NOT RUN, never PASS; output clipped with an explicit marker.

Side effects: none beyond the suites' own temp-dir selftests. This module
does NOT call regression.run() and does NOT write record_log (PO ruling
2026-09-01; receipts are a later story).
"""
from __future__ import annotations
import os
import re
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import regression  # noqa: E402  -- the registry and the runner, nothing else

TESTRUN_CHARS = int(os.environ.get("TESTRUN_CHARS", "2000"))
TESTRUN_ALL_CHARS = int(os.environ.get("TESTRUN_ALL_CHARS", "6000"))
TESTRUN_BUDGET = float(os.environ.get("TESTRUN_BUDGET", "600"))

# One argument per ARGS line; no '<' or '[' on an op line (boxread lesson).
OPS_HELP = (
    "test.run     -- execute ONE approved suite by its registry name\n"
    "                ARGS: <suite name>\n"
    "test.all     -- execute EVERY approved (safe-tier) suite\n"
    "                ARGS: (leave empty)")

_NAME_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


def _ok(kind, **f):
    d = {"status": "OK", "kind": kind}
    d.update(f)
    return d


def _unverified(kind, reason, **f):
    """The ONLY failure shape. A reason, never a guess, never a verdict."""
    d = {"status": "UNVERIFIED", "kind": kind, "reason": reason}
    d.update(f)
    return d


def approved(suites=None):
    """Ordered (name, argv) for the SAFE tier only. Live suites do not
    exist as far as this interface is concerned (PO ruling 2026-09-01)."""
    src = regression.SUITES if suites is None else suites
    return [(n, a) for n, a, t in src if t == "safe"]


def _clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return ("[... %d characters omitted; showing the LAST %d ...]\n%s"
            % (omitted, limit, text[-limit:]))


def _verdict(res):
    r = res.get("result")
    if r == regression.RESULT_PASS:
        return "PASS"
    if r == regression.RESULT_FAIL:
        return "FAIL"
    return "UNVERIFIED"  # ERROR: missing file / timeout / launch error


def _timed_out(res):
    return (res.get("result") == regression.RESULT_ERROR
            and str(res.get("out", "")).startswith("timeout after"))


def run_suite(name, suites=None, timeout=None, chars=None):
    """One approved suite by exact registry name."""
    name = (name or "").strip()
    if not _NAME_OK.match(name):
        return _unverified("test.run",
                           "denied: %r is not a suite name (a single registry "
                           "token; no paths, flags, options or commands)"
                           % name[:80])
    table = dict(approved(suites))
    if name not in table:
        return _unverified("test.run", "denied: no approved suite named %r"
                           % name, approved=sorted(table))
    kw = {} if timeout is None else {"timeout": timeout}
    res = regression._run_one(name, table[name], **kw)
    v = _verdict(res)
    fields = dict(suite=name, verdict=v, exit_code=res.get("code"),
                  counts=res.get("counts"),
                  elapsed_s=round(res.get("secs", 0.0), 2),
                  timed_out=_timed_out(res),
                  output=_clip(res.get("out"), chars or TESTRUN_CHARS))
    if v == "UNVERIFIED":
        return _unverified("test.run", "the suite produced no result: %s"
                           % str(res.get("out", ""))[:200], **fields)
    return _ok("test.run", **fields)


def run_all(suites=None, budget=None, timeout=None, chars=None):
    """Every approved suite, in registry order, under a wall budget."""
    budget = TESTRUN_BUDGET if budget is None else budget
    kw = {} if timeout is None else {"timeout": timeout}
    t0 = time.monotonic()
    lines, detail, not_run = [], [], []
    passed = failed = unver = 0
    for name, argv in approved(suites):
        if time.monotonic() - t0 > budget:
            not_run.append(name)
            continue
        res = regression._run_one(name, argv, **kw)
        v = _verdict(res)
        if v == "PASS":
            passed += 1
        elif v == "FAIL":
            failed += 1
        else:
            unver += 1
        lines.append("%-11s %-18s exit=%s %.1fs%s"
                     % (v, name, res.get("code"), res.get("secs", 0.0),
                        " TIMEOUT" if _timed_out(res) else ""))
        if v != "PASS":
            detail.append("--- %s (%s) ---\n%s"
                          % (name, v, _clip(res.get("out"),
                                            chars or TESTRUN_CHARS)))
    for name in not_run:
        lines.append("%-11s %-18s (budget of %.0fs exhausted before start)"
                     % ("NOT RUN", name, budget))
    if failed:
        overall = "FAIL"
    elif unver or not_run:
        overall = "UNVERIFIED"
    else:
        overall = "PASS"
    fields = dict(verdict=overall, ran=passed + failed + unver, passed=passed,
                  failed=failed, unverified=unver, not_run=len(not_run),
                  approved=len(approved(suites)),
                  elapsed_s=round(time.monotonic() - t0, 1),
                  lines="\n".join(lines),
                  detail=_clip("\n".join(detail), TESTRUN_ALL_CHARS))
    if overall == "UNVERIFIED":
        return _unverified("test.all",
                           "no failures, but %d suite(s) produced no result "
                           "and %d were not run; the run is NOT green"
                           % (unver, len(not_run)), **fields)
    return _ok("test.all", **fields)


def run_op(op, args=""):
    """Map a TEST operation to its function. This is the ONLY entry point
    ask.py calls; it exposes no suites/timeout/budget parameters, so the
    production registry and bounds are not selectable from a reply."""
    o = (op or "").strip().lower()
    a = (args or "").strip()
    if o == "test.run":
        return run_suite(a)
    if o == "test.all":
        if a:
            return _unverified("test.all", "test.all takes no ARGS; got %r"
                               % a[:80])
        return run_all()
    return _unverified("test", "no such test operation: %r" % (op,))


def render(d):
    """Text form. UNVERIFIED renders as UNVERIFIED with its reason and never
    as a result; OK leads with the VERDICT so FAIL cannot be buried."""
    if not isinstance(d, dict) or "status" not in d:
        return "UNVERIFIED: malformed result"
    out = []
    if d["status"] != "OK":
        out.append("UNVERIFIED: %s" % d.get("reason"))
    else:
        out.append("VERDICT: %s" % d.get("verdict"))
    for k in ("suite", "exit_code", "counts", "elapsed_s", "timed_out",
              "ran", "passed", "failed", "unverified", "not_run", "approved"):
        if k in d and d[k] is not None and not isinstance(d[k], list):
            out.append("%s: %s" % (k, d[k]))
    if isinstance(d.get("approved"), list):
        out.append("approved suites: %s" % ", ".join(d["approved"]))
    for k in ("lines", "detail", "output"):
        if d.get(k):
            out.append("%s:\n%s" % (k, d[k]))
    return "\n".join(out)


# --- selftest: fixture registry only, never the production runner --------
def _selftest():
    if not __debug__:
        print("testrun selftest: refusing to run under -O (asserts disabled)")
        raise SystemExit(2)
    import tempfile  # noqa: PLC0415
    n = [0, 0]

    def check(label, cond):
        n[0] += 1
        if cond:
            n[1] += 1
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))

    PY = regression.PY
    td = tempfile.mkdtemp(prefix="testrun-fx-")

    def fx(fname, body):
        p = os.path.join(td, fname)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    ok = fx("ok.py", "print('3/3 passed')\n")
    bad = fx("bad.py", "import sys; print('boom'); sys.exit(1)\n")
    lie = fx("lie.py", "print('2/3 passed')\n")
    slow = fx("slow.py", "import time; time.sleep(30)\n")
    noisy = fx("noisy.py", "import sys; print('x'*5000); "
                           "print('err-line', file=sys.stderr)\n")
    S = [("fx_ok", [PY, ok], "safe"), ("fx_bad", [PY, bad], "safe"),
         ("fx_lie", [PY, lie], "safe"), ("fx_slow", [PY, slow], "safe"),
         ("fx_noisy", [PY, noisy], "safe"),
         ("fx_missing", [PY, os.path.join(td, "nope.py")], "safe"),
         ("fx_live", [PY, ok], "live")]

    r = run_suite("fx_ok", suites=S)
    check("passing suite -> OK/PASS exit 0 counts (3,3)",
          r["status"] == "OK" and r["verdict"] == "PASS"
          and r["exit_code"] == 0 and r["counts"] == (3, 3))
    r = run_suite("fx_bad", suites=S)
    check("failing suite stays FAIL, exit 1, output kept",
          r["status"] == "OK" and r["verdict"] == "FAIL"
          and r["exit_code"] == 1 and "boom" in r["output"])
    r = run_suite("fx_lie", suites=S)
    check("exit 0 with printed failures is FAIL, not PASS",
          r["verdict"] == "FAIL" and r["exit_code"] == 0)
    t0 = time.monotonic()
    r = run_suite("fx_slow", suites=S, timeout=1)
    check("timeout -> UNVERIFIED, timed_out=True, never PASS",
          r["status"] == "UNVERIFIED" and r["verdict"] == "UNVERIFIED"
          and r["timed_out"] is True and time.monotonic() - t0 < 10)
    r = run_suite("fx_missing", suites=S)
    check("missing file -> UNVERIFIED with the runner's reason",
          r["status"] == "UNVERIFIED" and "missing file" in r["reason"])
    r = run_suite("fx_live", suites=S)
    check("live-tier suite is unreachable (denied, not run)",
          r["status"] == "UNVERIFIED" and "no approved suite" in r["reason"])
    r = run_suite("no_such_suite", suites=S)
    check("unknown name denied with the approved list",
          r["status"] == "UNVERIFIED" and "fx_ok" in r["approved"]
          and "fx_live" not in r["approved"])
    for evil in ("fx_ok; rm -rf /", "fx_ok --all", "/bin/sh", "$(id)",
                 "fx_ok fx_bad", "", "   ", "../regression.py"):
        r = run_suite(evil, suites=S)
        check("shell/flag/path disguised as a suite denied: %r" % evil[:20],
              r["status"] == "UNVERIFIED" and "verdict" not in r)
    r = run_suite("fx_noisy", suites=S, chars=500)
    check("output bounded with an explicit omission marker",
          len(r["output"]) < 700 and "omitted" in r["output"]
          and "[stderr]" in r["output"])

    a = run_all(suites=[S[0]])
    check("full run of passing suites -> PASS", a["status"] == "OK"
          and a["verdict"] == "PASS" and a["passed"] == 1)
    a = run_all(suites=S[:3])
    check("any FAIL makes the full run FAIL",
          a["status"] == "OK" and a["verdict"] == "FAIL" and a["failed"] == 2
          and "fx_lie" in a["detail"])
    a = run_all(suites=[S[0], S[3]], timeout=1)
    check("a timeout with no failures -> UNVERIFIED, not PASS",
          a["status"] == "UNVERIFIED" and a["unverified"] == 1
          and "TIMEOUT" in a["lines"])
    a = run_all(suites=S, budget=0)
    check("exhausted budget -> nothing runs, all NOT RUN, UNVERIFIED",
          a["status"] == "UNVERIFIED" and a["ran"] == 0
          and a["not_run"] == len(approved(S)) and "NOT RUN" in a["lines"])
    check("live entries are outside the full run entirely",
          all(name != "fx_live" for name, _ in approved(S)))

    check("run_op: unknown op",
          run_op("test.exec", "")["status"] == "UNVERIFIED")
    check("run_op: test.all refuses ARGS",
          run_op("test.all", "--all")["status"] == "UNVERIFIED")
    check("run_op: command disguised as name denied",
          run_op("test.run", "ls -la")["status"] == "UNVERIFIED")
    check("run_op: unknown production name denied without running",
          "denied" in run_op("test.run", "definitely_not_a_suite")["reason"])

    check("render: UNVERIFIED leads with UNVERIFIED",
          render(_unverified("x", "why")).startswith("UNVERIFIED: why"))
    check("render: OK leads with the verdict",
          render(run_suite("fx_bad", suites=S)).startswith("VERDICT: FAIL"))
    check("render: garbage is UNVERIFIED",
          render(None).startswith("UNVERIFIED"))
    for _l in OPS_HELP.splitlines():
        if _l.startswith("test."):
            check("OPS_HELP op line carries no placeholder: %s"
                  % _l.split()[0], "<" not in _l and "[" not in _l)
    check("OPS_HELP puts arguments on ARGS lines", "ARGS:" in OPS_HELP)

    prod = approved()
    check("production registry: at least one approved suite", len(prod) >= 1)
    check("production registry: every approved entry is safe tier",
          all(t == "safe" for n, _, t in regression.SUITES
              if n in dict(prod)))
    check("runner accepts a timeout keyword (pinned)",
          "timeout" in regression._run_one.__code__.co_varnames)

    print("%d/%d passed" % (n[1], n[0]))
    return 0 if n[0] == n[1] else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
