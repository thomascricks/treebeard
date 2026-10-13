#!/usr/bin/env python3
# -*- coding: ascii -*-
"""s3_verify_harness.py -- S3-1a step 4: independent completion verification.

Runs the HASH-LOCKED acceptance suite against a completed section in a
clean temp jail (meeseeks._jail_run, same rlimits), independent of the
implementation agent's own test assertions. Refuses to run if the suite
on disk does not match its LOCK.sha256 manifest, and refuses filename
collisions so the artifact can never shadow a locked test. Receipt is
the full record; exit 0 pass / 1 fail / 2 lock-or-usage error.
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meeseeks  # noqa: E402

LOCK_NAME = "LOCK.sha256"


def acceptance_dir(pid):
    return os.path.join(meeseeks.root(), "acceptance", pid)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _suite_files(adir):
    return sorted(f for f in os.listdir(adir)
                  if f != LOCK_NAME and not f.startswith(".")
                  and os.path.isfile(os.path.join(adir, f)))


def _read_lock(adir):
    lock = {}
    with open(os.path.join(adir, LOCK_NAME), encoding="ascii") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                digest, name = ln.split(None, 1)
                lock[name] = digest
    return lock


def _check(adir):
    """Returns (ok, message)."""
    if not os.path.isdir(adir):
        return False, "no acceptance dir: %s" % adir
    if not os.path.exists(os.path.join(adir, LOCK_NAME)):
        return False, "suite not locked (no %s)" % LOCK_NAME
    lock = _read_lock(adir)
    on_disk = _suite_files(adir)
    if sorted(lock) != on_disk:
        return False, ("lock/disk file-set mismatch: lock=%s disk=%s"
                       % (sorted(lock), on_disk))
    for name, digest in sorted(lock.items()):
        d = _sha(os.path.join(adir, name))
        if d != digest:
            return False, "hash mismatch on %s" % name
    return True, "lock verified: %d file(s)" % len(lock)


def cmd_lock(args):
    adir = acceptance_dir(args.project)
    if os.path.exists(os.path.join(adir, LOCK_NAME)):
        sys.stderr.write("refusing: %s already locked; unlocking is a "
                         "deliberate operator act (remove %s by hand)\n"
                         % (args.project, LOCK_NAME))
        return 2
    if not os.path.isdir(adir) or not _suite_files(adir):
        sys.stderr.write("no suite files in %s\n" % adir)
        return 2
    names = _suite_files(adir)
    tests = [n for n in names if n.startswith("test_")
             and n.endswith(".py")]
    if not tests:
        sys.stderr.write("suite has no test_*.py; refusing to lock\n")
        return 2
    lines = ["%s  %s" % (_sha(os.path.join(adir, n)), n) for n in names]
    meeseeks.atomic_write(os.path.join(adir, LOCK_NAME),
                          "\n".join(lines) + "\n")
    print("locked %s: %d file(s), %d test module(s)"
          % (args.project, len(names), len(tests)))
    return 0


def cmd_check(args):
    ok, msg = _check(acceptance_dir(args.project))
    print(msg)
    return 0 if ok else 2


def cmd_run(args):
    pid, sid = args.project, args.section
    adir = acceptance_dir(pid)
    ok, msg = _check(adir)
    if not ok:
        sys.stderr.write("refusing to run: %s\n" % msg)
        return 2
    files = meeseeks.section_files(pid, sid)
    if not files:
        sys.stderr.write("section %s/%s has no files\n" % (pid, sid))
        return 2
    suite = _suite_files(adir)
    collide = sorted(set(files) & set(suite))
    if collide:
        sys.stderr.write("refusing: artifact/suite filename collision %s\n"
                         % collide)
        return 2
    lockline = _sha(os.path.join(adir, LOCK_NAME))
    runs = []
    meeseeks.group_dirs(meeseeks.staging_dir())
    jail = tempfile.mkdtemp(prefix="verify-", dir=meeseeks.staging_dir())
    try:
        for name, body in files.items():
            dest = os.path.join(jail, name)
            d = os.path.dirname(dest)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(dest, "w", encoding="ascii", errors="replace") as fh:
                fh.write(body)
        for n in suite:
            shutil.copy2(os.path.join(adir, n), jail)
        tmo = max(30, int(os.environ.get("S3_VERIFY_TIMEOUT", "180") or 180))
        allpy = sorted([n for n in files if n.endswith(".py")]
                       + [n for n in suite if n.endswith(".py")])
        passed = True
        for n in allpy:
            rc, out, err = meeseeks._jail_run(
                [sys.executable, "-m", "py_compile", n], jail, timeout=tmo)
            runs.append(("py_compile %s" % n, rc, out, err))
            if rc != 0:
                passed = False
        if passed:
            for n in suite:
                if not (n.startswith("test_") and n.endswith(".py")):
                    continue
                rc, out, err = meeseeks._jail_run(
                    [sys.executable, "-m", "unittest", "-v", n[:-3]],
                    jail, timeout=tmo)
                runs.append(("unittest %s" % n, rc, out, err))
                if rc != 0:
                    passed = False
    finally:
        shutil.rmtree(jail, ignore_errors=True)
    parts = ["INDEPENDENT VERIFICATION RECEIPT -- project %s section %s -- %s"
             % (pid, sid, meeseeks.utcnow()),
             "SUITE: %s" % adir,
             "LOCK sha256: %s" % lockline,
             "ARTIFACT FILES: %s" % ", ".join(sorted(files)),
             "RESULT: %s" % ("PASS" if passed else "FAIL"), ""]
    for label, rc, out, err in runs:
        parts += ["== %s rc=%d ==" % (label, rc),
                  "-- stdout --", out if out else "(empty)",
                  "-- stderr --", err if err else "(empty)", ""]
    rdir = os.path.join(meeseeks.proj_dir(pid), "receipts")
    meeseeks.group_dirs(rdir)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rpath = os.path.join(rdir, "verify-%s-%s-%03d.txt"
                         % (sid, stamp, int(time.time() * 1000) % 1000))
    meeseeks.atomic_write(rpath, "\n".join(parts), encoding="utf-8")
    print("verification %s; receipt: %s"
          % ("PASS" if passed else "FAIL", rpath))
    return 0 if passed else 1


def cmd_selftest(args):
    results = []

    def ok(name, cond, note=""):
        results.append((name, bool(cond)))
        print("%-46s %s" % (name, "ok" if cond else "FAIL"))
        if not cond and note:
            print("    detail: %s" % note)

    saved = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix="s3verify-selftest-")
    try:
        os.environ["MEESEEKS_ROOT"] = os.path.join(tmp, "data")
        pid, sid = "p1", "s01"
        sdir = meeseeks.section_dir(pid, sid)
        meeseeks.group_dirs(sdir)
        meeseeks.atomic_write(os.path.join(sdir, "task.md"), "task\n")
        meeseeks.atomic_write(os.path.join(sdir, "calc.py"),
                              "# adds\ndef add(a, b):\n    return a + b\n")
        meeseeks.atomic_write(os.path.join(sdir, "test_calc.py"),
                              "# impl's own test\nimport calc\n")
        adir = acceptance_dir(pid)
        meeseeks.group_dirs(adir)
        meeseeks.atomic_write(
            os.path.join(adir, "test_acc.py"),
            "import unittest\n# locked acceptance\nimport calc\n\n"
            "class T(unittest.TestCase):\n    def test(self):\n"
            "        self.assertEqual(calc.add(2, 2), 4)\n")

        rc = cmd_run(argparse.Namespace(project=pid, section=sid))
        ok("run refuses unlocked suite", rc == 2)
        rc = cmd_lock(argparse.Namespace(project=pid))
        ok("lock succeeds", rc == 0
           and os.path.exists(os.path.join(adir, LOCK_NAME)))
        rc = cmd_lock(argparse.Namespace(project=pid))
        ok("relock refused", rc == 2)
        rc = cmd_check(argparse.Namespace(project=pid))
        ok("check passes on intact suite", rc == 0)

        rc = cmd_run(argparse.Namespace(project=pid, section=sid))
        ok("verification passes good artifact", rc == 0)
        rdir = os.path.join(meeseeks.proj_dir(pid), "receipts")
        rfs = sorted(f for f in os.listdir(rdir) if f.startswith("verify-"))
        rtxt = open(os.path.join(rdir, rfs[-1]), encoding="utf-8").read()
        ok("receipt has full run + lock hash",
           "== unittest test_acc.py rc=0 ==" in rtxt
           and "LOCK sha256:" in rtxt and "RESULT: PASS" in rtxt)

        meeseeks.atomic_write(os.path.join(sdir, "calc.py"),
                              "# broken\ndef add(a, b):\n    return a - b\n")
        rc = cmd_run(argparse.Namespace(project=pid, section=sid))
        ok("verification fails broken artifact", rc == 1)
        rfs = sorted(f for f in os.listdir(rdir) if f.startswith("verify-"))
        rtxt = open(os.path.join(rdir, rfs[-1]), encoding="utf-8").read()
        ok("fail receipt records failure", "RESULT: FAIL" in rtxt)

        with open(os.path.join(adir, "test_acc.py"), "a") as fh:
            fh.write("# tampered\n")
        rc = cmd_check(argparse.Namespace(project=pid))
        ok("tampered suite detected", rc == 2)
        rc = cmd_run(argparse.Namespace(project=pid, section=sid))
        ok("run refuses tampered suite", rc == 2)

        pid2 = "p2"
        s2 = meeseeks.section_dir(pid2, sid)
        meeseeks.group_dirs(s2)
        meeseeks.atomic_write(os.path.join(s2, "test_acc.py"),
                              "# shadow attempt\n")
        a2 = acceptance_dir(pid2)
        meeseeks.group_dirs(a2)
        meeseeks.atomic_write(os.path.join(a2, "test_acc.py"),
                              "import unittest\n# acc\n")
        cmd_lock(argparse.Namespace(project=pid2))
        rc = cmd_run(argparse.Namespace(project=pid2, section=sid))
        ok("filename collision refused", rc == 2)

        fails = [r for r in results if not r[1]]
        print("SELFTEST %d/%d PASS" % (len(results) - len(fails),
                                       len(results)))
        if fails:
            sys.exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="s3_verify_harness")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, fn, wants_sid in (("lock", cmd_lock, False),
                                ("check", cmd_check, False),
                                ("run", cmd_run, True),
                                ("selftest", cmd_selftest, False)):
        p = sub.add_parser(name)
        if name != "selftest":
            p.add_argument("project")
        if wants_sid:
            p.add_argument("section")
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
