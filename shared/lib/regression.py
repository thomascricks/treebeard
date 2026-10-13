#!/usr/bin/env python3
"""
regression.py — Treebeard substrate regression runner.

Target path: /opt/treebeard/shared/lib/regression.py   (root:treebeard 750)

Purpose
-------
One on-demand command that runs the substrate's existing selftests and reports
a single per-suite PASS / FAIL / ERROR summary, then logs the run to
record_log. This is the post-build safety net: after any change, run it and
know in one shot whether a store or the router or the meeseeks box broke.

Treebeard #9. Assembly of parts that already exist — it invents no tests, it
invokes the ones each module already ships.

Correctness invariants (why this is careful, not just a for-loop)
-----------------------------------------------------------------
1. SUITES USE DIFFERENT INVOCATIONS. Confirmed on the box:
     - stores:   python3 <file> --selftest
     - router:   python3 router.py        (bare; __main__ runs _selftest, exit=code)
     - meeseeks: python3 meeseeks.py selftest   (a SUBCOMMAND, not --selftest)
   Each suite carries its own argv. No single convention is assumed.

2. ERROR != FAIL. A suite that cannot be INVOKED (import blows up, wrong argv,
   missing file, timeout) is reported ERROR, never PASS and never silently
   skipped. A regression runner that turns "couldn't run the suite" into green
   is worse than no runner. Exit code reflects: any FAIL or ERROR => nonzero.

3. SAFE TIER BY DEFAULT. Suites that are pure-logic and self-contained
   (temp-dir stores, no model calls, no prod writes) run by default. Suites
   that may hit a model, spend tokens, or mutate a shared production DB are
   OPT-IN via --all. A net that costs money or writes prod on every pull is a
   net you won't pull.

Usage
-----
  regression.py               run the SAFE tier, print summary, log, exit
  regression.py --all         also run the LIVE tier (may cost tokens / touch prod)
  regression.py --quiet       summary only, suppress per-suite captured output
  regression.py --no-log      do not write the run to record_log
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

LIB = f"{_TB_BASE}/shared/lib"
MEESEEKS = f"{_TB_BASE}/shared/meeseeks/meeseeks.py"
# Production runs every consumer through the venv -- chromadb, reportlab
# and pypdf are installed THERE and not in system python. Inheriting
# sys.executable made this gate mean different things depending on how it
# was launched: under bare python3, career_packets reported 3 failures + 1
# error for a reportlab that is present where it actually matters. Pin the
# interpreter so "regression green" is one fact, not two. (2026-08-24)
_VENV_PY = f"{_TB_BASE}/shared/venv/bin/python"
PY = _VENV_PY if _tb_os.path.exists(_VENV_PY) else (sys.executable or "python3")

# --- Suite registry --------------------------------------------------------
# Each suite: (name, argv, tier). tier in {"safe","live"}.
# argv is the FULL command; invocation style is explicit per suite.
SUITES = [
    # safe: self-contained, temp-dir, no model, no prod write
    ("record_log",   [PY, f"{LIB}/record_log.py",   "--selftest"], "safe"),
    ("ltm_clark",    [PY, f"{LIB}/ltm_clark.py",    "--selftest"], "safe"),
    ("draft_store",  [PY, f"{LIB}/draft_store.py",  "--selftest"], "safe"),
    ("triage",       [PY, f"{LIB}/triage.py",       "--selftest"], "safe"),
    ("shortterm",    [PY, f"{LIB}/shortterm.py",    "--selftest"], "safe"),
    ("memscan",      [PY, f"{LIB}/memscan.py",      "--selftest"], "safe"),
    # selector: --selftest = the edition-independent check (synthetic index,
    # miss detection, missing-corpus raises). The PRIVATE corpus run
    # (selector_regress.py with no flag) needs this box's missions store and
    # corpus, which the public edition does not ship, so it is an internal
    # gate run explicitly, like internal_gates.py (release prep E5).
    ("selector",     [PY, f"{LIB}/selector_regress.py", "--selftest"], "safe"),
    # router: bare run; __main__ -> raise SystemExit(_selftest())
    ("router",       [PY, f"{LIB}/router.py"],                      "safe"),
    # meeseeks: SUBCOMMAND, not a flag
    ("meeseeks",     [PY, MEESEEKS, "selftest"],                    "safe"),
    # 2026-08-31 (S-BRIDGE-1 S6, PO ruling): board and reflector ARE
    # aggregated here. Both modules and both suites are publication-manifest
    # copy entries as of this story, so naming them is legal under the
    # CR-3/CR-4 rule above, and the published edition ships the control plane
    # rather than omitting it. Both are unittest files: each builds temp roots
    # via TASKBOARD_PATH / MEESEEKS_ROOT, stubs step_once, and touches no
    # model, network, or production store. This SUPERSEDES the 2026-08-28 note
    # that a public harness could not claim coverage of them.
    #
    # test_reflector_unit.py is deliberately NOT registered. It asserts the
    # INSTALLED systemd service and timer -- install-time artifacts of one
    # host, not portable software. It is published so publication/install
    # verification can run it, and it is run there, not as runtime regression.
    #
    # Host-layout invariants stay OUT of this harness entirely: the board.py
    # sync pair, and the rule that no installed unit may invoke the
    # operator-only wrapper, live in shared/lib/internal_gates.py, which is
    # neither manifested nor registered.
    ("board",        [PY, f"{LIB}/test_board.py"],                   "safe"),
    ("reflector",    [PY, f"{LIB}/test_reflector.py"],               "safe"),
    # 2026-10-02 BOARD-TRUTH L: the one transition, the lock, approval,
    # exec_ref-no-authority, sticky status failure, reconcile sidecar, and
    # all-surface resolver equality. TASKBOARD_PATH/MEESEEKS_ROOT fixtures
    # only (R3); never a production card.
    ("board_lifecycle", [PY, f"{LIB}/test_board_lifecycle.py"],      "safe"),
    # sprint1: U9 OPEN PLATE + U10 Moon summary fidelity. Fixture-based,
    # read-only against prod; no model call, no email, no prod write.
    ("sprint1",      [PY, f"{LIB}/sprint1_regression.py", "--selftest"], "safe"),
    # 2026-08-16 routing policy change: derived performance + policy proofs.
    ("perf",         [PY, f"{LIB}/perf.py"],                          "safe"),
    ("policy",       [PY, f"{LIB}/policy_regression.py"],             "safe"),
    # 2026-08-16 bug #3 cost derivation: budget sums stored cost_usd.
    ("budget",       [PY, f"{LIB}/budget.py", "--selftest"],           "safe"),
    # 2026-08-20 S2-H2: Sprint 1 suites registered (were standalone-only).
    # All temp-dir / in-memory, exit non-zero on failure, no model, no prod
    # write. clark_patrol reads prod plan_lists.json (read-only, like sprint1).
    ("clark_queue",       [PY, f"{LIB}/clark_queue.py",        "--selftest"], "safe"),
    ("clark_fit_review",  [PY, f"{LIB}/clark_fit_review.py",   "--selftest"], "safe"),
    ("agent_inbound",     [PY, f"{LIB}/agent_inbound.py",      "--selftest"], "safe"),
    ("moon_retention",    [PY, f"{LIB}/moon_retention.py",     "--selftest"], "safe"),
    ("career_hunter",     [PY, f"{LIB}/career_hunter_daily.py","--selftest"], "safe"),
    ("clark_patrol",      [PY, f"{LIB}/clark_patrol.py",       "--selftest"], "safe"),
    ("clark_patrol_drv",  [PY, f"{LIB}/clark_patrol_driver.py","--selftest"], "safe"),
    ("missions",          [PY, f"{LIB}/missions.py",           "--selftest"], "safe"),
    # unittest file: unittest.main() exits non-zero on any failure
    ("career_packets",    [PY, f"{LIB}/test_career_packets.py"],             "safe"),
    # 2026-08-31 UI-02: canonical UI state vocabulary + badge render
    # helper. Pure logic, no I/O, no model, no prod write. Manifest copy
    # entry added in the same story (CR-3/CR-4: the public harness names
    # only shipped modules).
    ("uistate",           [PY, f"{LIB}/uistate.py",            "--selftest"], "safe"),
    # 2026-09-01 dev-notes reader. The dn journal had a writer and no
    # reader since 2026-06-30; this is the read half. Pure file I/O over a
    # temp dir in the selftest, never the live journal.
    ("devnotes",          [PY, f"{LIB}/devnotes.py"],                      "safe"),
    # Own-box read interface. Safe tier: read-only by construction,
    # no model call, and its denied-path assertions are the proof
    # that the boundary holds after any later change.
    ("boxread",           [PY, f"{LIB}/boxread.py"],                       "safe"),
    # 2026-09-01 test-execution story: bounded runner over THIS registry.
    # Its selftest uses temp fixture scripts only -- no recursion.
    ("testrun",           [PY, f"{LIB}/testrun.py"],                       "safe"),
    # Optimized-mode integrity: every registered suite must DECLARE
    # whether its selftest refuses under -O, and refuse-suites are
    # behaviourally probed. Prevents the 2026-09-02 false-green class.
    ("optmode",           [PY, f"{LIB}/optmode.py", "--selftest"],       "safe"),
    # 2026-08-31 UI-01: shell header/nav/badge + the single attention
    # computation the badge and Home queue both consume. Fixture-based
    # selftest, never reads the live board.
    ("opshell",           [PY, f"{LIB}/opshell.py"],                       "safe"),
    # 2026-08-31 UI-01: read-only operator console service. Selftest binds
    # only an ephemeral loopback socket (proving the bind cannot reach the
    # public address), mocks the board for the 404/200/503 branches, and
    # never serves or writes anything.
    ("opsrv",             [PY, f"{LIB}/opsrv.py"],                         "safe"),
    # STORY 1: opchat now owns the web-path dispatch drain. Standing rule
    # 2026-08-30 -- a module the harness cannot test does not ship.
    ("opchat",            [PY, f"{LIB}/opchat.py"],                        "safe"),
    # loop, wrappers, publish_export are NOT aggregated here: loop.py,
    # test_wrappers.py and publish_export.py are not in the publication
    # manifest (loop private for the MVP; sms PUBLIC since 2026-08-31 --
    # see below; publish_export + pubident are
    # publication machinery, not public runtime), and a public harness must
    # not name files the edition does not ship (clean-room CR-3/CR-4). Their
    # selftests remain authoritative and run explicitly as internal gates.
    # (export-coherence-20260828)
    # 2026-08-25: subsystem suites registered (were standalone-only). Each
    # builds its own temp roots -- anvil/toolbox documented as never touching
    # real paths; toolbox sets its own env incl. TOOLBOX_UNSAFE_ROOM + fake
    # router; sun redirects DREAMS_DIR to mkdtemp, no network, no email.
    ("anvil",    [PY, f"{_TB_BASE}/shared/anvil/anvil.py",     "selftest"],   "safe"),
    ("toolbox",  [PY, f"{_TB_BASE}/shared/toolbox/toolbox.py", "selftest"],   "safe"),
    ("sun",      [PY, f"{_TB_BASE}/sun/agent.py",              "--selftest"], "safe"),
    # 2026-08-30: registered with their modules. Both are public runtime
    # (meet.py and ask.py import them) and both are in the manifest, so
    # naming them here is legal under the CR-3/CR-4 rule above. Both build
    # their own temp roots and touch no real store, model, or policy.
    ("meetmem",  [PY, f"{LIB}/meetmem.py",  "--selftest"], "safe"),
    ("dispatch", [PY, f"{LIB}/dispatch.py", "--selftest"], "safe"),
    # 2026-08-31: the two CLI entry points and the dispatch executor. On
    # 2026-08-30 ask.py was installed with a module-level NameError (an
    # undefined `_re`) and this harness reported 26/26 green, because
    # nothing registered here imported ask.py. All three are manifest copy
    # entries, so naming them is legal under the CR-3/CR-4 rule above.
    # Each selftest is documented offline: no router, no network, temp dir
    # only; dispatch_exec patches dispatch.complete/pending in-process so
    # the real dispatch store is never opened. Exit code = failure count.
    ("ask",           [PY, f"{_TB_BASE}/treebeard/ask.py",  "--selftest"], "safe"),
    ("meet",          [PY, f"{_TB_BASE}/treebeard/meet.py", "--selftest"], "safe"),
    ("dispatch_exec", [PY, f"{LIB}/dispatch_exec.py",       "--selftest"], "safe"),
    # 2026-08-31: sms.py made PUBLIC by PO ruling because published meet.py
    # F35 depends on it. Selftest is an argparse SUBCOMMAND (like meeseeks);
    # it points SMS_ROOT and SMS_PAIRS_CONF at a temp dir, so the root-owned
    # production store and policy are never touched; no network. Prints
    # "N/N PASS" (not "passed"), so the exit code is the harness signal.
    ("sms",           [PY, f"{LIB}/sms.py",                 "selftest"],   "safe"),
    # 2026-10-01 FC04: ask.py's machine contract (--json v1) and the Clark
    # intake driver's binding to it. Both offline: ask_contract tests the
    # parser against literal outputs and a runner seam; clark_intake's
    # selftest drives clark_turn through that seam only -- it never polls
    # Resend, opens the ledger, or calls run(). Exit code = failure count.
    ("ask_contract",  [PY, f"{LIB}/ask_contract.py",        "--selftest"], "safe"),
    ("clark_intake",  [PY, f"{LIB}/clark_intake_driver.py", "--selftest"], "safe"),
    # live: may call a model, spend tokens, or mutate a shared prod store.
    # Opt-in only. Classification is conservative — if unsure, it's live.
    ("telemetry",    [PY, f"{LIB}/telemetry.py",    "--selftest"], "live"),
    ("longterm",     [PY, f"{LIB}/longterm.py",     "--selftest"], "live"),
    ("subconscious", [PY, f"{LIB}/subconscious.py", "--selftest"], "live"),
    ("dream",        [PY, f"{LIB}/dream.py",        "--selftest"], "live"),
    ("cognition_tools", [PY, f"{LIB}/cognition_tools.py", "--selftest"], "live"),
    ("governance_audit", [PY, f"{LIB}/governance_audit.py", "--selftest"], "live"),
]

# Parse "N/M passed" if a suite prints it; else fall back to exit code only.
_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+passed", re.IGNORECASE)

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_ERROR = "ERROR"

PER_SUITE_TIMEOUT = 180  # seconds; a hung suite is an ERROR, not a hang


def _run_one(name, argv, timeout=PER_SUITE_TIMEOUT):
    """Invoke one suite. Returns dict: name, result, code, counts, secs, out.
    ERROR covers: file missing, import/launch failure, timeout. FAIL is a
    clean run that returned nonzero. PASS is exit 0 (and, if the suite prints
    N/M, N==M as a cross-check that catches a suite that exits 0 while
    printing failures)."""
    path = argv[1]
    if not os.path.exists(path):
        return {"name": name, "result": RESULT_ERROR, "code": None,
                "counts": None, "secs": 0.0, "out": f"missing file: {path}"}
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"name": name, "result": RESULT_ERROR, "code": None,
                "counts": None, "secs": time.monotonic() - t0,
                "out": f"timeout after {timeout}s"}
    except Exception as e:  # launch failure (perm, interpreter, etc.)
        return {"name": name, "result": RESULT_ERROR, "code": None,
                "counts": None, "secs": time.monotonic() - t0,
                "out": f"launch error: {e}"}
    secs = time.monotonic() - t0
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")

    counts = None
    m = _COUNT_RE.search(p.stdout or "")
    if m:
        counts = (int(m.group(1)), int(m.group(2)))

    # Decide result. Exit code is primary. If counts exist, they must agree —
    # a suite that exits 0 but printed "3/5 passed" is a FAIL (guards against a
    # __main__ that forgets to propagate the failing code).
    if p.returncode == 0:
        if counts and counts[0] != counts[1]:
            result = RESULT_FAIL
        else:
            result = RESULT_PASS
    else:
        result = RESULT_FAIL
    return {"name": name, "result": result, "code": p.returncode,
            "counts": counts, "secs": secs, "out": out}


def run(include_live=False, quiet=False):
    suites = [s for s in SUITES if s[2] == "safe" or include_live]
    results = []
    print(f"regression: running {len(suites)} suites "
          f"({'safe+live' if include_live else 'safe only'})\n")
    for name, argv, tier in suites:
        r = _run_one(name, argv)
        results.append(r)
        badge = {"PASS": "PASS ", "FAIL": "FAIL ", "ERROR": "ERROR"}[r["result"]]
        cnt = f" {r['counts'][0]}/{r['counts'][1]}" if r["counts"] else ""
        code = f" exit={r['code']}" if r["code"] is not None else ""
        print(f"  {badge} {name:<16}{cnt:<8}{code}  ({r['secs']:.1f}s)  [{tier}]")
        if not quiet and r["result"] != RESULT_PASS:
            # show captured output only for non-pass, indented
            for line in (r["out"] or "").splitlines():
                print(f"        | {line}")
    return results


def summarize(results):
    p = sum(1 for r in results if r["result"] == RESULT_PASS)
    f = sum(1 for r in results if r["result"] == RESULT_FAIL)
    e = sum(1 for r in results if r["result"] == RESULT_ERROR)
    return p, f, e


def _log_run(results, include_live):
    """Write one governance record of this regression run. Best-effort; a
    logging failure never changes the runner's exit code."""
    try:
        sys.path.insert(0, LIB)
        import record_log as rl
        p, f, e = summarize(results)
        rl.log_event(
            "action", "treebeard",
            subject="regression run",
            basis=f"substrate regression: {p} pass, {f} fail, {e} error "
                  f"({'safe+live' if include_live else 'safe'})",
            payload={
                "pass": p, "fail": f, "error": e,
                "tier": "safe+live" if include_live else "safe",
                "suites": {r["name"]: {
                    "result": r["result"],
                    "counts": r["counts"], "code": r["code"],
                } for r in results},
            },
        )
    except Exception as ex:
        print(f"regression: WARNING could not log run: {ex}", file=sys.stderr)


def main(argv):
    include_live = "--all" in argv
    quiet = "--quiet" in argv
    do_log = "--no-log" not in argv

    results = run(include_live=include_live, quiet=quiet)
    p, f, e = summarize(results)

    print(f"\nregression: {p} PASS, {f} FAIL, {e} ERROR  "
          f"(of {len(results)} suites)")
    if f or e:
        bad = [r["name"] for r in results if r["result"] != RESULT_PASS]
        print(f"regression: not green -> {', '.join(bad)}")

    if do_log:
        _log_run(results, include_live)

    # Nonzero exit on ANY fail or error, so callers/CI can gate on it.
    return 0 if (f == 0 and e == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
