#!/usr/bin/env python3
"""
optmode.py -- optimized-mode selftest integrity for the suite registry.

Target path: /opt/treebeard/shared/lib/optmode.py   (root:treebeard 640)

Why this exists
---------------
Under `python -O` every `assert` statement is removed. A selftest whose
checks are bare assertions therefore verifies nothing, reaches its normal
success path, and exits 0 -- which regression.py, being exit-code primary,
records as PASS. That is a false green: a real failure reported as success.
Three registered suites (shortterm, sprint1, telemetry) were proven to do
exactly this on 2026-09-02 and were repaired to fail closed.

Author discipline is not a mechanism. This module makes the property
DECLARED and BEHAVIOURALLY ENFORCED:

  refuse     -- this suite's selftest MUST terminate under -O with RC 2 and
                a diagnostic naming optimized mode. Assertion-dependent
                suites declare this. So do suites that choose to refuse for
                any other reason (testrun has no asserts and refuses anyway).
  supported  -- this suite imposes no refusal requirement. It may run under
                -O. The enforcement layer never probes it.

Declaration is a CONTRACT, not an inference. Counting `assert` AST nodes was
tried during the survey and produced a false positive (sun: assert-bearing,
but its selftest verdict comes from an explicit failure counter, so it is
correctly `supported`). Every registered suite must appear in MODES; an
undeclared suite is an integrity failure, which forces whoever registers a
new suite to choose its optimized-mode contract rather than inherit one.

What is NOT checked
-------------------
Textual identity. Four wordings and both output streams are in use across
the ten refusing suites. The property is deliberate refusal, not
implementation style, so the matcher is permissive about phrasing while
requiring BOTH the prescribed exit code AND an intentional diagnostic. An
unrelated crash satisfies neither.

Usage
-----
  optmode.py --selftest   declarations + SAFE-tier enforcement + fixtures
                          (this is the registered suite entry point)
  optmode.py              same as --selftest
  optmode.py --all        additionally enforce the LIVE tier. Probing is
                          cheap (refusal precedes all suite work) but live
                          execution stays opt-in per PO ruling 2026-09-02.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys

_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
LIB = f"{_TB_BASE}/shared/lib"
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import regression  # noqa: E402  (registry is the single source of suites)

SELF_NAME = "optmode"
REFUSE = "refuse"
SUPPORTED = "supported"
VALID_MODES = (REFUSE, SUPPORTED)

# The contract: an optimized-mode refusal is exit code 2 plus a diagnostic.
REFUSAL_RC = 2
PROBE_TIMEOUT = 60

# --- Declarations ----------------------------------------------------------
# Seeded 2026-09-02 from measured behaviour, not from source inspection.
# Every name here is a regression.SUITES key. No default: absence is failure.
MODES = {
    # --- refuse: proven to terminate under -O with RC 2 + diagnostic -------
    "shortterm": REFUSE,      # guarded 2026-09-02 (was false-green)
    "sprint1": REFUSE,        # guarded 2026-09-02 (was false-green)
    "telemetry": REFUSE,      # guarded 2026-09-02 (was false-green), live
    "uistate": REFUSE,
    "devnotes": REFUSE,
    "boxread": REFUSE,
    "opshell": REFUSE,        # RC normalized 1->2 on 2026-09-02
    "opsrv": REFUSE,          # RC normalized 1->2 on 2026-09-02
    "opchat": REFUSE,         # RC normalized 1->2 on 2026-09-02
    "testrun": REFUSE,        # no asserts; refuses by choice. Contract, not count.
    SELF_NAME: REFUSE,        # this module. Excluded from its own probe loop.
    # --- supported: no refusal requirement --------------------------------
    "record_log": SUPPORTED,
    "ltm_clark": SUPPORTED,
    "draft_store": SUPPORTED,
    "triage": SUPPORTED,
    "memscan": SUPPORTED,     # explicit pass/fail state, exits from result
    "selector": REFUSE,       # assert-based selftest, guarded 2026-09-04 (A+ Stage 0 corpus)
    "router": SUPPORTED,
    "meeseeks": SUPPORTED,
    "board": SUPPORTED,
    "reflector": SUPPORTED,
    "board_lifecycle": SUPPORTED,  # BOARD-TRUTH L 2026-10-02: unittest
                                   # assertions (not stripped by -O)
    "perf": SUPPORTED,
    "policy": SUPPORTED,
    "budget": SUPPORTED,
    "clark_queue": SUPPORTED,
    "clark_fit_review": SUPPORTED,
    "agent_inbound": SUPPORTED,
    "moon_retention": SUPPORTED,
    "career_hunter": SUPPORTED,
    "clark_patrol": SUPPORTED,
    "clark_patrol_drv": SUPPORTED,
    "missions": SUPPORTED,
    "career_packets": SUPPORTED,
    "anvil": SUPPORTED,
    "toolbox": SUPPORTED,
    "sun": SUPPORTED,         # assert-bearing but verdict is a failure counter
    "meetmem": SUPPORTED,
    "dispatch": SUPPORTED,
    "ask": SUPPORTED,
    "meet": SUPPORTED,
    "dispatch_exec": SUPPORTED,
    "sms": SUPPORTED,
    "ask_contract": SUPPORTED,   # FC04 2026-10-01: explicit failure counter, no asserts
    "clark_intake": SUPPORTED,   # FC04 2026-10-01: explicit failure counter, no asserts
    "longterm": SUPPORTED,
    "subconscious": SUPPORTED,
    "dream": SUPPORTED,
    "cognition_tools": SUPPORTED,
    "governance_audit": SUPPORTED,
}

# Requires BOTH an optimized-mode token AND an assertion/refusal token.
# Validated against all ten live wordings and eight negative controls.
_REFUSAL_RE = re.compile(r"(?is)(?=.*(?:-O\b|optimi[sz])).*(?:assert|refus)")


def is_refusal_diagnostic(text):
    """True if text reads as a deliberate optimized-mode refusal."""
    return bool(_REFUSAL_RE.search(text or ""))


def check_declarations(suites=None, modes=None):
    """Static: every registered suite declared, every declaration used.
    Returns a list of problem strings; empty means clean."""
    suites = regression.SUITES if suites is None else suites
    modes = MODES if modes is None else modes
    names = [s[0] for s in suites]
    problems = []
    for n in names:
        if n not in modes:
            problems.append("undeclared registered suite: %s" % n)
        elif modes[n] not in VALID_MODES:
            problems.append("invalid mode %r for %s" % (modes[n], n))
    for n in sorted({x for x in names if names.count(x) > 1}):
        problems.append("duplicate registry name: %s" % n)
    for n in sorted(set(modes) - set(names)):
        problems.append("orphan declaration (not in registry): %s" % n)
    return problems


def _probe(argv, timeout=PROBE_TIMEOUT):
    """Re-invoke a suite's own registry argv with -O inserted.
    Returns (rc, combined_output) or (None, reason)."""
    cmd = [argv[0], "-O"] + list(argv[1:])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout after %ss under -O" % timeout
    except Exception as ex:                      # launch failure is not a pass
        return None, "launch error under -O: %s" % ex
    return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")


def check_refusals(tier="safe", suites=None, modes=None, skip=(SELF_NAME,)):
    """Behavioural: every suite in `tier` declared refuse must exit
    REFUSAL_RC under -O with a recognizable diagnostic.
    Returns (checked_count, problems)."""
    suites = regression.SUITES if suites is None else suites
    modes = MODES if modes is None else modes
    checked, problems = 0, []
    for name, argv, t in suites:
        if t != tier or modes.get(name) != REFUSE or name in skip:
            continue
        checked += 1
        rc, blob = _probe(argv)
        if rc is None:
            problems.append("%s: %s" % (name, blob))
        elif rc != REFUSAL_RC:
            problems.append("%s: RC %s under -O, expected %s -- declared "
                            "refuse but did not deliberately refuse"
                            % (name, rc, REFUSAL_RC))
        elif not is_refusal_diagnostic(blob):
            problems.append("%s: RC %s but no recognizable optimized-mode "
                            "refusal diagnostic" % (name, REFUSAL_RC))
    return checked, problems


# --- selftest --------------------------------------------------------------
def _selftest():
    if not __debug__:
        import sys as _sys
        print("optmode: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    import tempfile  # noqa: PLC0415
    n = [0, 0]

    def check(label, cond):
        n[0] += 1
        if cond:
            n[1] += 1
            print("  ok    %s" % label)
        else:
            print("  FAIL  %s" % label)

    STUB = {
        "good": ('import sys\n'
                 'if not __debug__:\n'
                 '    print("stub: selftest requires assertions; refusing to '
                 'run under -O", file=sys.stderr)\n'
                 '    raise SystemExit(2)\n'
                 'assert True\n'),
        "zero": 'assert 1 == 2, "would fail"\nprint("stub OK")\n',
        "one": ('import sys\n'
                'if not __debug__:\n'
                '    raise SystemExit("stub: unrelated crash")\n'),
        "silent": 'if not __debug__:\n    raise SystemExit(2)\n',
    }

    with tempfile.TemporaryDirectory() as td:
        paths = {}
        for k, body in STUB.items():
            p = os.path.join(td, "stub_%s.py" % k)
            open(p, "w", encoding="utf-8").write(body)
            paths[k] = p

        def fx(key, mode=REFUSE, name=None):
            name = name or ("stub_" + key)
            return ([(name, [sys.executable, paths[key], "--selftest"], "safe")],
                    {name: mode})

        # --- declarations, against fixtures -------------------------------
        ok_s = [("a", [], "safe")]
        check("declarations clean when every suite is declared",
              check_declarations(ok_s, {"a": SUPPORTED}) == [])
        check("undeclared registered suite is a problem",
              any("undeclared" in p
                  for p in check_declarations(ok_s, {})))
        check("invalid mode value is a problem",
              any("invalid mode" in p
                  for p in check_declarations(ok_s, {"a": "maybe"})))
        check("orphan declaration is a problem",
              any("orphan" in p
                  for p in check_declarations(ok_s, {"a": SUPPORTED,
                                                     "ghost": REFUSE})))
        check("duplicate registry name is a problem",
              any("duplicate" in p
                  for p in check_declarations([("a", [], "safe"),
                                               ("a", [], "safe")],
                                              {"a": SUPPORTED})))

        # --- behavioural enforcement, against fixtures --------------------
        s, m = fx("good")
        c, probs = check_refusals("safe", s, m)
        check("compliant refusal passes (RC 2 + diagnostic)",
              c == 1 and probs == [])

        s, m = fx("zero")
        c, probs = check_refusals("safe", s, m)
        check("CRIT-6 false green (RC 0 under -O) is caught",
              c == 1 and len(probs) == 1 and "RC 0" in probs[0])

        s, m = fx("one")
        c, probs = check_refusals("safe", s, m)
        check("CRIT-7 unrelated nonzero (RC 1) is caught, not accepted",
              c == 1 and len(probs) == 1 and "RC 1" in probs[0])

        s, m = fx("silent")
        c, probs = check_refusals("safe", s, m)
        check("RC 2 without a diagnostic is caught",
              c == 1 and len(probs) == 1 and "diagnostic" in probs[0])

        s, m = fx("zero", mode=SUPPORTED)
        c, probs = check_refusals("safe", s, m)
        check("CRIT-8 a supported suite is never probed or required to refuse",
              c == 0 and probs == [])

        s, m = fx("good")
        s = [(nm, a, "live") for nm, a, _t in s]
        c, probs = check_refusals("safe", s, m)
        check("CRIT-10 live suites are not probed by the safe tier",
              c == 0 and probs == [])
        c, probs = check_refusals("live", s, m)
        check("CRIT-11 live suites ARE probed on the live path",
              c == 1 and probs == [])

    # --- matcher -----------------------------------------------------------
    for good in ("x: selftest requires asserts (no -O)",
                 "boxread selftest REFUSES to run under -O: assertions are "
                 "stripped, so a pass would prove nothing",
                 "testrun selftest: refusing to run under -O (asserts disabled)",
                 "y: selftest requires assertions; refusing to run under -O",
                 "z: refusing to run under optimized mode"):
        check("matcher accepts: %s" % good[:38], is_refusal_diagnostic(good))
    for bad in ("Traceback (most recent call last):\nValueError: bad",
                "sprint1: 21/21 passed",
                "opsrv selftest: ALL PASS",
                "assertion count is 13",
                "running with -O for speed"):
        check("matcher rejects: %s" % bad[:38].replace("\n", " "),
              not is_refusal_diagnostic(bad))

    # --- own guard, with a hard recursion stop -----------------------------
    check("self is excluded from its own probe loop",
          SELF_NAME in check_refusals.__defaults__[-1])
    if os.environ.get("OPTMODE_SELF_PROBE") != "1":
        env = dict(os.environ, OPTMODE_SELF_PROBE="1")
        p = subprocess.run([sys.executable, "-O", os.path.abspath(__file__),
                            "--selftest"], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, env=env)
        blob = (p.stdout or "") + "\n" + (p.stderr or "")
        check("own selftest refuses under -O with RC 2 + diagnostic",
              p.returncode == REFUSAL_RC and is_refusal_diagnostic(blob))

    check("refusal contract is exit code 2", REFUSAL_RC == 2)

    # --- the real registry -------------------------------------------------
    probs = check_declarations()
    check("PRODUCTION: every registered suite is declared (%d problems)"
          % len(probs), probs == [])
    if probs:
        for p in probs:
            print("        | %s" % p)
    c, probs = check_refusals("safe")
    check("PRODUCTION: all %d safe refuse-suites deliberately refuse" % c,
          probs == [] and c > 0)
    for p in probs:
        print("        | %s" % p)

    print("\n%d/%d passed" % (n[1], n[0]))
    return 0 if n[0] == n[1] else 1


def main(argv):
    include_live = "--all" in argv
    if "--selftest" in argv or not argv or argv == ["--all"]:
        if not include_live:
            return _selftest()
    rc = 0
    probs = check_declarations()
    print("optmode: declarations -- %s"
          % ("clean" if not probs else "%d problem(s)" % len(probs)))
    for p in probs:
        print("  %s" % p)
        rc = 1
    for tier in (["safe", "live"] if include_live else ["safe"]):
        c, probs = check_refusals(tier)
        print("optmode: %s tier -- %d refuse-suites probed, %d problem(s)"
              % (tier, c, len(probs)))
        for p in probs:
            print("  %s" % p)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
