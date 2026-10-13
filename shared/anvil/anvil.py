#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
anvil.py -- Treebeard Phase 2, Anvil v1 (BRIDGE MODE)

Anvil is an OBJECT, not an agent (settled 2026-06-29). It holds the hot dev
sandbox and, later, the Meeseeks box. This build is the pre-Meeseeks slice:
a shared build service every agent can deposit into, with a compile/test
pipeline and a human-gated promotion path into the toolkit.

REQUIREMENTS TRACE (operator spec, 2026-07-02)
  R1 "any agent can write to it on their own"
     -> per-agent drop zones under ANVIL_ROOT/dropzones/<agent>/.
        Intake registers every deposit into the candidate pool with
        provenance: depositor, file list, per-file sha256, manifest hash,
        UTC timestamp. Unattributed candidates cannot exist.
  R2 "any agent can use the meeseeks box"
     -> DEFERRED. Triggers land with the Meeseeks box build (operator
        ruling 2026-07-02: "wait til we get to the meeseeks box to enact
        the triggers"). No spawn code in this file.
  R3 "treebeard must have access to change the anvil, the box or the
      toolkit at will"
     -> Ownership layer, enforced at deploy time (chown/chmod), not in
        code: treebeard user/group holds write over ANVIL_ROOT and
        TOOLKIT_ROOT; agents hold write only on their own drop zone.
        Autonomous runtime self-modification ("will") is Phase 3 by
        operator ruling; nothing here grants it.
  R4 "must be able to compile code written in it and test it and have it
      ready for acceptance"
     -> Pipeline: REGISTERED -> build (syntax parse + py_compile +
        import-in-jail) -> BUILT -> test (unittest discover in jail)
        -> TESTED -> READY. Reports with hashes under reports/<id>/.
  R5 "code treebeard can lock in when he deems it done ... for now
      presenting it when done as ready in the daily email survey"
     -> Human-gated lock (Will is Phase 3): `lock <id> --approved-by X`
        copies the candidate into TOOLKIT_ROOT, pins hashes in a
        MANIFEST.json, sets files read-only, logs approver. The READY
        queue is written atomically to status/ready.txt after every
        state change; `digest` prints the same block. Sun grows a
        five-line section that reads it (separate, later deploy).
        Tier 4 final review: later phase, out of scope here.

BRIDGE MODE
  Every subcommand is invoked manually from the treebeard shell. The
  operator is the trigger, the scheduler, and (per last session's
  settlement) the interim validator on the lock gate. No daemon, no
  timer, no LLM calls, no credentials. Anvil v1 makes zero cognition
  calls, so no constitution gate and no telemetry rows are required;
  Anvil keeps its own append-only event ledger in anvil.db.

STATES
  REGISTERED -> BUILT -> TESTED -> READY -> LOCKED
  Failure states: FAILED_BUILD, FAILED_TEST (terminal; fix = new deposit)
  TAMPERED: manifest hash mismatch detected after registration (terminal)
  DROPPED: operator discard. From LOCKED only via gated retire
           (drop <id> --approved-by X); clears toolkit dir + bare link.

LAYOUT (defaults; override with env ANVIL_ROOT / TOOLKIT_ROOT)
  /opt/treebeard/anvil/
    dropzones/<agent>/     agent-writable intake (treebeard,sun,moon,clark,han)
    candidates/<id>/       registered candidate pool (content-addressed)
    staging/               atomic build area (always empty between ops)
    reports/<id>/          build.txt / test.txt verdicts
    status/ready.txt       the Sun hook, rewritten atomically
    anvil.db               SQLite (WAL): candidates + append-only events
  /opt/treebeard/shared/toolkit/
    <name>-<id>/           locked tools, read-only, MANIFEST.json pinned
    <name> -> <name>-<id>   bare-name link; lock writes it, retire clears it

CONVENTIONS (inherited from the stack)
  Pure ASCII source (Fix 1 discipline). Atomic writes everywhere:
  write-temp-then-rename, same filesystem. Append-only events table,
  ts_utc column naming. Content-addressed candidate ids: first 12 hex of
  the manifest sha256, so a byte-identical duplicate deposit collapses to
  the same id and is skipped. Manifest re-verified before build, test,
  ready, and lock; any drift -> TAMPERED, refuse. In-place edits of a
  candidate are prohibited by construction: a fix is a new deposit.

ISOLATION NOTE (pre-Podman, on purpose)
  Build/test subprocesses run jailed by rlimits (CPU/address space/file
  size/nofile), a cleaned environment, a candidate-dir cwd, and hard
  timeouts. This is cooperative-code isolation, adequate for bridge mode
  where a human invokes every run and deposits are operator-curated. It
  is NOT the wall. The wall is rootless Podman and it arrives with the
  Meeseeks box. _sandbox_run() is the single seam where the Podman
  prefix will bolt on; nothing else changes.

SELFTEST
  `anvil.py selftest` runs the whole pipeline end to end in throwaway
  temp roots (never touches real paths) and reports N/N PASS.
"""

import argparse
import ast
import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

AGENTS = ("treebeard", "sun", "moon", "clark", "han")

BUILD_TIMEOUT_S = 60          # per build subprocess
TEST_TIMEOUT_S = 300          # whole test run
RLIMIT_CPU_S = 120            # hard CPU seconds per subprocess
RLIMIT_AS_BYTES = 1 * 1024 ** 3   # 1 GiB address space
RLIMIT_FSIZE_BYTES = 64 * 1024 ** 2  # 64 MiB max file a jailed proc may write
RLIMIT_NOFILE = 256
SETTLE_SECONDS = 2            # skip dropzone entries modified this recently

IGNORE_NAMES = {"__pycache__", ".DS_Store"}

TERMINAL_STATES = {"FAILED_BUILD", "FAILED_TEST", "TAMPERED", "DROPPED", "LOCKED"}


def anvil_root():
    return os.environ.get("ANVIL_ROOT", "/opt/treebeard/shared/anvil/data")


def toolkit_root():
    return os.environ.get("TOOLKIT_ROOT", "/opt/treebeard/shared/toolkit")


# --- CONTAINMENT T2 rev 8 (operator P1, 2026-10-04): production build/test --
# build and test EXECUTE candidate code (Meeseeks deposits) as uid treebeard.
# Operator identity is not yet separated from the service identity (Router
# 2.0), so no process fact can tell an operator launch from a service or
# agent launch.
# The rev-3 operator gate (an ancestor's real uid) is DELETED: it proved
# nothing. On the PRODUCTION pool build/test are NOT IMPLEMENTED for every
# caller, root included, until Router 2.0 separates the identities. The
# refusal is logged (syslog, ident tb-t2). Environment variables, flags and
# arguments change nothing. A temp (non-production) ANVIL_ROOT -- the
# selftest -- is not gated: it binds the production pipeline only.
PRODUCTION_ANVIL_ROOT = "/opt/treebeard/shared/anvil/data"
NOT_IMPLEMENTED = ("NOT_IMPLEMENTED: operator authentication deferred to "
                   "Router 2.0")


def _t2_log(msg):
    try:
        import syslog
        syslog.openlog("tb-t2", 0, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_WARNING, msg)
    except Exception:
        pass


def operator_gate(what):
    """None if allowed (non-production pool); else the logged refusal."""
    if os.path.realpath(anvil_root()) != os.path.realpath(PRODUCTION_ANVIL_ROOT):
        return None
    why = ("REFUSED: anvil %s on the production pool: %s (it executes "
           "candidate code, and no operator gate exists in T2)"
           % (what, NOT_IMPLEMENTED))
    _t2_log("anvil %s refused uid=%d: %s" % (what, os.getuid(), NOT_IMPLEMENTED))
    return why


def lock_users():
    # who may operate the lock gate (the human gate, until Tier 4 exists)
    raw = os.environ.get("ANVIL_LOCK_USERS", "treebeard,root")
    return {u.strip() for u in raw.split(",") if u.strip()}


def p_dropzones():
    return os.path.join(anvil_root(), "dropzones")


def p_candidates():
    return os.path.join(anvil_root(), "candidates")


def p_staging():
    return os.path.join(anvil_root(), "staging")


def p_reports():
    return os.path.join(anvil_root(), "reports")


def p_status():
    return os.path.join(anvil_root(), "status")


def p_db():
    return os.path.join(anvil_root(), "anvil.db")


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root):
    """Relative-path -> abs-path map of a candidate/deposit tree,
    skipping IGNORE_NAMES. Deterministic order."""
    out = {}
    if os.path.isfile(root):
        out[os.path.basename(root)] = root
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_NAMES)
        for fn in sorted(filenames):
            if fn in IGNORE_NAMES:
                continue
            ap = os.path.join(dirpath, fn)
            rp = os.path.relpath(ap, root)
            out[rp] = ap
    return out


def manifest_of(root):
    """{relpath: sha256} plus the manifest hash (sha256 of the sorted
    'relpath:hash' lines). The manifest hash is the candidate identity."""
    files = walk_files(root)
    fh = {rp: sha256_file(ap) for rp, ap in files.items()}
    lines = "\n".join("%s:%s" % (rp, fh[rp]) for rp in sorted(fh))
    mh = hashlib.sha256(lines.encode("ascii", "replace")).hexdigest()
    return fh, mh


def atomic_write_text(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp.")
    os.fchmod(fd, 0o640)  # mkstemp creates 0600 regardless of umask; enforce tree convention
    try:
        with os.fdopen(fd, "w", encoding="ascii", errors="replace") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def copy_tree_flat(src_root, files, dst_root):
    """Copy a manifest's files preserving relative paths."""
    for rp in files:
        sp = os.path.join(src_root, rp) if not os.path.isfile(src_root) else src_root
        dp = os.path.join(dst_root, rp)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(sp, dp)


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  depositor    TEXT NOT NULL,
  state        TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  files_json   TEXT NOT NULL,
  created_utc  TEXT NOT NULL,
  updated_utc  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc       TEXT NOT NULL,
  candidate_id TEXT,
  event        TEXT NOT NULL,
  actor        TEXT NOT NULL,
  detail       TEXT
);
"""


def db():
    os.makedirs(anvil_root(), exist_ok=True)
    con = sqlite3.connect(p_db(), timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.executescript(SCHEMA)
    return con


def log_event(con, cid, event, detail=""):
    con.execute(
        "INSERT INTO events(ts_utc, candidate_id, event, actor, detail) "
        "VALUES(?,?,?,?,?)",
        (now_utc(), cid, event, getpass.getuser(), detail[:2000]),
    )
    con.commit()


def get_candidate(con, cid):
    row = con.execute(
        "SELECT id, name, depositor, state, manifest_hash, files_json, "
        "created_utc, updated_utc FROM candidates WHERE id=?", (cid,)
    ).fetchone()
    if not row:
        return None
    keys = ("id", "name", "depositor", "state", "manifest_hash",
            "files_json", "created_utc", "updated_utc")
    return dict(zip(keys, row))


def transition(con, cid, state, event, detail=""):
    """Commit a state change and its audit row in ONE transaction.
    Either both land or neither does -- no state moves without a ledger
    row, no ledger row without the move. This is the integrity gate on
    the append-only events table: an unaudited transition cannot exist."""
    ts = now_utc()
    try:
        con.execute("BEGIN")
        con.execute(
            "UPDATE candidates SET state=?, updated_utc=? WHERE id=?",
            (state, ts, cid),
        )
        con.execute(
            "INSERT INTO events(ts_utc, candidate_id, event, actor, detail) "
            "VALUES(?,?,?,?,?)",
            (ts, cid, event, getpass.getuser(), detail[:2000]),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------

def verify_manifest(con, cand):
    """Re-hash the candidate dir against its registered manifest.
    Any drift -> TAMPERED (terminal), refuse to proceed."""
    cdir = os.path.join(p_candidates(), cand["id"])
    fh, mh = manifest_of(cdir)
    if mh != cand["manifest_hash"]:
        transition(con, cand["id"], "TAMPERED", "tamper_detected",
                   "expected %s got %s"
                   % (cand["manifest_hash"][:12], mh[:12]))
        write_status_card(con)
        return False
    return True


# --------------------------------------------------------------------------
# jailed subprocess (the Podman seam)
# --------------------------------------------------------------------------

def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (RLIMIT_CPU_S, RLIMIT_CPU_S))
    resource.setrlimit(resource.RLIMIT_AS, (RLIMIT_AS_BYTES, RLIMIT_AS_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE,
                       (RLIMIT_FSIZE_BYTES, RLIMIT_FSIZE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (RLIMIT_NOFILE, RLIMIT_NOFILE))
    os.setsid()


def _sandbox_run(cmd, cwd, timeout):
    """Single seam for execution isolation. Today: cleaned env + rlimits +
    timeout + candidate-dir cwd (cooperative-code jail, pre-Podman).
    The Meeseeks box build replaces the front of `cmd` with the rootless
    Podman invocation; callers do not change."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": cwd,
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "0",
    }
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, timeout=timeout,
            capture_output=True, text=True, preexec_fn=_limits,
        )
        return proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as te:
        out = te.stdout or ""
        err = te.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("ascii", "replace")
        if isinstance(err, bytes):
            err = err.decode("ascii", "replace")
        return -1, out, err, True


# --------------------------------------------------------------------------
# status card (the Sun hook)
# --------------------------------------------------------------------------

def render_digest(con):
    ready = con.execute(
        "SELECT id, name, depositor, updated_utc FROM candidates "
        "WHERE state='READY' ORDER BY updated_utc"
    ).fetchall()
    locked = con.execute(
        "SELECT id, name, depositor, updated_utc FROM candidates "
        "WHERE state='LOCKED' ORDER BY updated_utc DESC LIMIT 5"
    ).fetchall()
    lines = []
    lines.append("ANVIL READY QUEUE (%s)" % now_utc())
    if ready:
        for cid, name, dep, upd in ready:
            lines.append("  READY  %s  %s  from=%s  since=%s"
                         % (cid, name, dep, upd))
    else:
        lines.append("  (empty)")
    lines.append("RECENTLY LOCKED")
    if locked:
        for cid, name, dep, upd in locked:
            lines.append("  LOCKED %s  %s  from=%s  at=%s"
                         % (cid, name, dep, upd))
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def write_status_card(con):
    atomic_write_text(os.path.join(p_status(), "ready.txt"),
                      render_digest(con))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(_args):
    for d in (p_dropzones(), p_candidates(), p_staging(),
              p_reports(), p_status()):
        os.makedirs(d, exist_ok=True)
    for a in AGENTS:
        os.makedirs(os.path.join(p_dropzones(), a), exist_ok=True)
    os.makedirs(toolkit_root(), exist_ok=True)
    con = db()
    log_event(con, None, "init", "roots=%s|%s" % (anvil_root(), toolkit_root()))
    write_status_card(con)
    print("anvil initialized at %s" % anvil_root())
    print("toolkit at %s" % toolkit_root())
    print("dropzones: %s" % ", ".join(AGENTS))
    print("NOTE: ownership layer (R3) is set at deploy time: "
          "treebeard group rwx on anvil+toolkit, each agent rwx only on "
          "its own dropzone. chown/chmod happen in the deploy walk.")
    return 0


def _intake_one(con, depositor, entry_path, name):
    fh, mh = manifest_of(entry_path)
    if not fh:
        return None, "empty"
    cid = mh[:12]
    if get_candidate(con, cid):
        log_event(con, cid, "duplicate_skipped",
                  "byte-identical deposit from %s removed" % depositor)
        _remove_entry(entry_path)
        return cid, "duplicate"
    stage = os.path.join(p_staging(), cid)
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)
    copy_tree_flat(entry_path, fh, stage)
    # verify the staged copy byte-for-byte before it becomes a candidate
    _, staged_mh = manifest_of(stage)
    if staged_mh != mh:
        shutil.rmtree(stage)
        return None, "copy-verify-failed"
    final = os.path.join(p_candidates(), cid)
    os.rename(stage, final)  # atomic: same filesystem
    con.execute(
        "INSERT INTO candidates(id, name, depositor, state, manifest_hash, "
        "files_json, created_utc, updated_utc) VALUES(?,?,?,?,?,?,?,?)",
        (cid, name, depositor, "REGISTERED", mh,
         json.dumps(fh, sort_keys=True), now_utc(), now_utc()),
    )
    con.commit()
    log_event(con, cid, "registered",
              "name=%s files=%d manifest=%s" % (name, len(fh), mh[:12]))
    _remove_entry(entry_path)
    return cid, "registered"


def _remove_entry(path):
    # Best-effort cleanup of a processed deposit. By the time this runs the
    # candidate is already committed (F13): the dedup path logs+removes, the
    # register path commits then removes. A removal failure -- e.g. a dropzone
    # dir owned by the depositing agent that treebeard can only read as group --
    # must warn and leave the deposit, never crash the intake run. Durable fix
    # is group-writable deposits on the agent side; this only stops the crash.
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return True
    except OSError as exc:
        import sys
        print("WARN: anvil intake could not remove deposit %s: %s" % (path, exc),
              file=sys.stderr)
        return False


def cmd_intake(_args):
    con = db()
    found = 0
    for agent in AGENTS:
        zone = os.path.join(p_dropzones(), agent)
        if not os.path.isdir(zone):
            continue
        for entry in sorted(os.listdir(zone)):
            if entry in IGNORE_NAMES or entry.startswith(".tmp."):
                continue
            ep = os.path.join(zone, entry)
            # settle guard: skip entries still being written
            newest = os.path.getmtime(ep)
            if os.path.isdir(ep):
                for rp, ap in walk_files(ep).items():
                    newest = max(newest, os.path.getmtime(ap))
            if time.time() - newest < SETTLE_SECONDS:
                print("settling, skipped: %s/%s" % (agent, entry))
                continue
            name = os.path.splitext(entry)[0] if os.path.isfile(ep) else entry
            cid, verdict = _intake_one(con, agent, ep, name)
            found += 1
            print("%-10s %s/%s -> %s" % (verdict, agent, entry, cid or "-"))
    if not found:
        print("dropzones empty")
    write_status_card(con)
    return 0


def _require_state(cand, allowed):
    if cand is None:
        print("no such candidate")
        return False
    if cand["state"] not in allowed:
        print("refused: %s is %s, needs %s"
              % (cand["id"], cand["state"], " or ".join(allowed)))
        return False
    return True


def cmd_build(args):
    why = operator_gate("build")
    if why:
        print(why)
        return 3
    con = db()
    cand = get_candidate(con, args.id)
    if not _require_state(cand, {"REGISTERED"}):
        return 1
    if not verify_manifest(con, cand):
        print("TAMPERED: manifest drift, candidate frozen")
        return 1
    cdir = os.path.join(p_candidates(), cand["id"])
    files = json.loads(cand["files_json"])
    pyfiles = [rp for rp in sorted(files) if rp.endswith(".py")]
    report = ["ANVIL BUILD REPORT", "candidate=%s name=%s depositor=%s"
              % (cand["id"], cand["name"], cand["depositor"]),
              "manifest=%s" % cand["manifest_hash"], "ts=%s" % now_utc(), ""]
    ok = True
    if not pyfiles:
        ok = False
        report.append("FAIL: no .py files in candidate")
    # stage 1: syntax parse (in-process, safe -- never executes)
    for rp in pyfiles:
        try:
            with open(os.path.join(cdir, rp), "r",
                      encoding="utf-8", errors="replace") as f:
                ast.parse(f.read(), filename=rp)
            report.append("parse   OK    %s" % rp)
        except SyntaxError as e:
            ok = False
            report.append("parse   FAIL  %s :: %s" % (rp, e))
    # stage 2: py_compile (jailed subprocess)
    if ok:
        for rp in pyfiles:
            rc, out, err, to = _sandbox_run(
                [sys.executable, "-m", "py_compile", rp],
                cwd=cdir, timeout=BUILD_TIMEOUT_S)
            tag = "OK" if rc == 0 else ("TIMEOUT" if to else "FAIL")
            report.append("compile %-5s %s" % (tag, rp))
            if rc != 0:
                ok = False
                report.append((err or out).strip()[:1000])
    # stage 3: import check on non-test top-level modules (jailed)
    if ok:
        mods = [rp[:-3] for rp in pyfiles
                if "/" not in rp and not rp.startswith("test_")]
        for m in mods:
            rc, out, err, to = _sandbox_run(
                [sys.executable, "-c", "import %s" % m],
                cwd=cdir, timeout=BUILD_TIMEOUT_S)
            tag = "OK" if rc == 0 else ("TIMEOUT" if to else "FAIL")
            report.append("import  %-5s %s" % (tag, m))
            if rc != 0:
                ok = False
                report.append((err or out).strip()[:1000])
    verdict = "BUILT" if ok else "FAILED_BUILD"
    report.append("")
    report.append("VERDICT: %s" % verdict)
    atomic_write_text(os.path.join(p_reports(), cand["id"], "build.txt"),
                      "\n".join(report) + "\n")
    transition(con, cand["id"], verdict, "build", verdict)
    write_status_card(con)
    print(verdict)
    return 0 if ok else 1


def cmd_test(args):
    why = operator_gate("test")
    if why:
        print(why)
        return 3
    con = db()
    cand = get_candidate(con, args.id)
    if not _require_state(cand, {"BUILT"}):
        return 1
    if not verify_manifest(con, cand):
        print("TAMPERED: manifest drift, candidate frozen")
        return 1
    cdir = os.path.join(p_candidates(), cand["id"])
    files = json.loads(cand["files_json"])
    tests = [rp for rp in files
             if os.path.basename(rp).startswith("test_")
             and rp.endswith(".py")]
    report = ["ANVIL TEST REPORT", "candidate=%s" % cand["id"],
              "ts=%s" % now_utc(), ""]
    if not tests:
        # strict by design: tested means tested. Definition of done on this
        # stack is correctness and durability, so an untested candidate does
        # not reach READY. Fix = redeposit with a test_*.py.
        report.append("FAIL: no test_*.py present. Untested code does not")
        report.append("go READY. Redeposit with at least one test.")
        report.append("")
        report.append("VERDICT: FAILED_TEST (no_tests)")
        atomic_write_text(os.path.join(p_reports(), cand["id"], "test.txt"),
                          "\n".join(report) + "\n")
        transition(con, cand["id"], "FAILED_TEST", "test",
                   "FAILED_TEST no_tests")
        write_status_card(con)
        print("FAILED_TEST (no_tests)")
        return 1
    rc, out, err, to = _sandbox_run(
        [sys.executable, "-m", "unittest", "discover",
         "-s", ".", "-p", "test_*.py", "-v"],
        cwd=cdir, timeout=TEST_TIMEOUT_S)
    combined = (out or "") + "\n" + (err or "")
    ran_zero = "Ran 0 tests" in combined
    ok = (rc == 0) and (not to) and (not ran_zero)
    report.append("cmd: unittest discover -p test_*.py")
    report.append("rc=%s timeout=%s" % (rc, to))
    report.append("")
    report.append(combined.strip()[:8000])
    report.append("")
    if ran_zero:
        report.append("FAIL: discovery ran zero tests")
    report.append("VERDICT: %s" % ("TESTED" if ok else "FAILED_TEST"))
    atomic_write_text(os.path.join(p_reports(), cand["id"], "test.txt"),
                      "\n".join(report) + "\n")
    verdict = "TESTED" if ok else "FAILED_TEST"
    transition(con, cand["id"], verdict, "test", verdict)
    write_status_card(con)
    print(verdict)
    return 0 if ok else 1


def cmd_ready(args):
    con = db()
    cand = get_candidate(con, args.id)
    if not _require_state(cand, {"TESTED"}):
        return 1
    if not verify_manifest(con, cand):
        print("TAMPERED: manifest drift, candidate frozen")
        return 1
    transition(con, cand["id"], "READY", "ready", "queued for daily survey")
    write_status_card(con)
    print("READY -- will appear in the daily survey block")
    return 0


def cmd_lock(args):
    user = getpass.getuser()
    if user not in lock_users():
        print("refused: lock gate is human-gated (Will is Phase 3); "
              "run as one of: %s" % ", ".join(sorted(lock_users())))
        return 1
    if not args.approved_by:
        print("refused: --approved-by is required; the approver is the "
              "interim validator of record")
        return 1
    con = db()
    cand = get_candidate(con, args.id)
    if not _require_state(cand, {"READY"}):
        return 1
    if not verify_manifest(con, cand):
        print("TAMPERED: manifest drift, candidate frozen")
        return 1
    cdir = os.path.join(p_candidates(), cand["id"])
    files = json.loads(cand["files_json"])
    name = args.name or cand["name"]
    dest_name = "%s-%s" % (name, cand["id"])
    dest = os.path.join(toolkit_root(), dest_name)
    if os.path.exists(dest):
        print("refused: %s already exists in toolkit" % dest_name)
        return 1
    bare = os.path.join(toolkit_root(), name)
    if os.path.islink(bare) or os.path.exists(bare):
        tgt = os.readlink(bare) if os.path.islink(bare) else "(not a link)"
        print("refused: bare name '%s' is claimed -> %s; retire the "
              "blocking lock first: drop <id> --approved-by ..."
              % (name, tgt))
        return 1
    stage = os.path.join(p_staging(), "lock-%s" % cand["id"])
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)
    copy_tree_flat(cdir, files, stage)
    _, staged_mh = manifest_of(stage)
    if staged_mh != cand["manifest_hash"]:
        shutil.rmtree(stage)
        print("refused: staged copy hash mismatch")
        return 1
    manifest = {
        "candidate_id": cand["id"],
        "name": name,
        "depositor": cand["depositor"],
        "manifest_hash": cand["manifest_hash"],
        "files": files,
        "approved_by": args.approved_by,
        "locked_by": user,
        "locked_utc": now_utc(),
        "tier4_review": "pending (later phase)",
    }
    atomic_write_text(os.path.join(stage, "MANIFEST.json"),
                      json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # read-only before the rename so the toolkit never holds a writable copy
    for dirpath, dirnames, filenames in os.walk(stage, topdown=False):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), 0o444)
        for dn in dirnames:
            os.chmod(os.path.join(dirpath, dn), 0o555)
    os.rename(stage, dest)  # atomic arrival in the toolkit
    os.chmod(dest, 0o555)
    # bare-name link: the toolbox resolves <name>; lock owns this link
    tmp_link = os.path.join(toolkit_root(), ".lock-link-%s" % cand["id"])
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(dest_name, tmp_link)
    os.replace(tmp_link, bare)
    transition(con, cand["id"], "LOCKED", "locked",
               "toolkit=%s link=%s approved_by=%s"
               % (dest_name, name, args.approved_by))
    write_status_card(con)
    print("LOCKED -> %s" % dest)
    print("linked: %s -> %s" % (name, dest_name))
    return 0


def cmd_drop(args):
    con = db()
    cand = get_candidate(con, args.id)
    if cand is None:
        print("no such candidate")
        return 1
    if cand["state"] == "LOCKED":
        user = getpass.getuser()
        if user not in lock_users():
            print("refused: retiring a locked tool is human-gated; "
                  "run as one of: %s" % ", ".join(sorted(lock_users())))
            return 1
        if not args.approved_by:
            print("refused: --approved-by is required to retire a "
                  "locked tool; the approver is the validator of record")
            return 1
        # locate the shelf dir by content-addressed suffix, not by
        # name -- lock accepts --name, so cand["name"] is not
        # authoritative for what landed on the shelf.
        suffix = "-%s" % cand["id"]
        hits = [d for d in os.listdir(toolkit_root())
                if d.endswith(suffix)
                and not os.path.islink(os.path.join(toolkit_root(), d))
                and os.path.isdir(os.path.join(toolkit_root(), d))]
        if len(hits) != 1:
            print("refused: expected one toolkit dir *%s, found %d"
                  % (suffix, len(hits)))
            return 1
        dest_name = hits[0]
        dest = os.path.join(toolkit_root(), dest_name)
        bare = os.path.join(toolkit_root(), dest_name[:-len(suffix)])
        # fail closed: the name stops resolving before the copy goes
        if os.path.islink(bare) and os.readlink(bare) == dest_name:
            os.remove(bare)
        # locked trees are 0444/0555 by design; open dirs, then remove
        for dirpath, _dn, _fn in os.walk(dest):
            os.chmod(dirpath, 0o700)
        shutil.rmtree(dest)
        cdir = os.path.join(p_candidates(), cand["id"])
        if os.path.isdir(cdir):
            shutil.rmtree(cdir)
        transition(con, cand["id"], "DROPPED", "retired",
                   "toolkit=%s approved_by=%s"
                   % (dest_name, args.approved_by))
        write_status_card(con)
        print("RETIRED %s" % dest_name)
        return 0
    cdir = os.path.join(p_candidates(), cand["id"])
    if os.path.isdir(cdir):
        shutil.rmtree(cdir)
    transition(con, cand["id"], "DROPPED", "dropped", "operator discard")
    write_status_card(con)
    print("DROPPED")
    return 0


def cmd_status(_args):
    con = db()
    rows = con.execute(
        "SELECT id, state, name, depositor, updated_utc FROM candidates "
        "ORDER BY updated_utc DESC"
    ).fetchall()
    if not rows:
        print("candidate pool empty")
        return 0
    print("%-13s %-13s %-20s %-10s %s"
          % ("ID", "STATE", "NAME", "FROM", "UPDATED"))
    for r in rows:
        print("%-13s %-13s %-20s %-10s %s" % r)
    return 0


def cmd_digest(_args):
    con = db()
    sys.stdout.write(render_digest(con))
    return 0


# --------------------------------------------------------------------------
# selftest -- end to end in throwaway roots, never touches real paths
# --------------------------------------------------------------------------

def _st_invoke(env, *argv):
    return subprocess.run(
        [sys.executable, os.path.abspath(__file__)] + list(argv),
        env=env, capture_output=True, text=True)


def cmd_selftest(_args):
    passed = 0
    failed = []

    def check(label, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(label)
        print("%-42s %s" % (label, "ok" if cond else "FAIL"))

    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["ANVIL_ROOT"] = os.path.join(td, "anvil")
        env["TOOLKIT_ROOT"] = os.path.join(td, "toolkit")
        env["ANVIL_LOCK_USERS"] = getpass.getuser()
        os.environ["ANVIL_ROOT"] = env["ANVIL_ROOT"]
        os.environ["TOOLKIT_ROOT"] = env["TOOLKIT_ROOT"]

        r = _st_invoke(env, "init")
        check("init exits clean", r.returncode == 0)
        check("db created", os.path.exists(p_db()))
        check("dropzones per agent",
              all(os.path.isdir(os.path.join(p_dropzones(), a))
                  for a in AGENTS))

        # good candidate: module + passing test
        gz = os.path.join(p_dropzones(), "sun", "goodtool")
        os.makedirs(gz)
        with open(os.path.join(gz, "goodtool.py"), "w") as f:
            f.write("def double(x):\n    return 2 * x\n")
        with open(os.path.join(gz, "test_goodtool.py"), "w") as f:
            f.write("import unittest\nimport goodtool\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_double(self):\n"
                    "        self.assertEqual(goodtool.double(3), 6)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n")
        # defeat the settle guard deterministically
        old = time.time() - 10
        for rp, ap in walk_files(gz).items():
            os.utime(ap, (old, old))
        os.utime(gz, (old, old))

        r = _st_invoke(env, "intake")
        check("intake registers deposit", "registered" in r.stdout)
        con = db()
        row = con.execute(
            "SELECT id, manifest_hash FROM candidates WHERE name='goodtool'"
        ).fetchone()
        check("candidate row exists", row is not None)
        gid = row[0] if row else ""
        check("content-addressed id", row is not None
              and gid == row[1][:12])
        check("dropzone drained", os.listdir(
            os.path.join(p_dropzones(), "sun")) == [])

        # duplicate deposit collapses
        os.makedirs(gz)
        with open(os.path.join(gz, "goodtool.py"), "w") as f:
            f.write("def double(x):\n    return 2 * x\n")
        with open(os.path.join(gz, "test_goodtool.py"), "w") as f:
            f.write("import unittest\nimport goodtool\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_double(self):\n"
                    "        self.assertEqual(goodtool.double(3), 6)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n")
        for rp, ap in walk_files(gz).items():
            os.utime(ap, (old, old))
        os.utime(gz, (old, old))
        r = _st_invoke(env, "intake")
        check("duplicate skipped", "duplicate" in r.stdout)

        # pipeline on the good candidate
        r = _st_invoke(env, "build", gid)
        check("build passes good code", "BUILT" in r.stdout
              and r.returncode == 0)
        r = _st_invoke(env, "test", gid)
        check("test passes good tests", "TESTED" in r.stdout
              and r.returncode == 0)
        r = _st_invoke(env, "lock", gid, "--approved-by", "selftest")
        check("lock refused before ready", r.returncode != 0)
        r = _st_invoke(env, "ready", gid)
        check("ready transition", r.returncode == 0)
        r = _st_invoke(env, "digest")
        check("digest shows ready id", gid in r.stdout)
        r = _st_invoke(env, "lock", gid)
        check("lock requires approver", r.returncode != 0)
        r = _st_invoke(env, "lock", gid, "--approved-by", "selftest")
        check("lock succeeds via gate", r.returncode == 0)
        tk = [d for d in os.listdir(toolkit_root()) if gid in d]
        check("toolkit entry exists", len(tk) == 1)
        if tk:
            tdir = os.path.join(toolkit_root(), tk[0])
            mpath = os.path.join(tdir, "MANIFEST.json")
            check("manifest pinned", os.path.exists(mpath))
            with open(mpath) as f:
                man = json.load(f)
            ondisk = sha256_file(os.path.join(tdir, "goodtool.py"))
            check("locked hash verifies",
                  man["files"]["goodtool.py"] == ondisk)
            import stat as _stat
            mode = _stat.S_IMODE(
                os.stat(os.path.join(tdir, "goodtool.py")).st_mode)
            check("locked files read-only", mode == 0o444)
        else:
            check("manifest pinned", False)
            check("locked hash verifies", False)
            check("locked files read-only", False)

        # F14: bare link, refuse-until-drop, gated retire, re-lock
        bare = os.path.join(toolkit_root(), "goodtool")
        check("bare link written", os.path.islink(bare)
              and bool(tk) and os.readlink(bare) == tk[0])

        g2 = os.path.join(p_dropzones(), "clark", "goodtool")
        os.makedirs(g2)
        with open(os.path.join(g2, "goodtool.py"), "w") as f:
            f.write("def double(x):\n    return x + x\n")
        with open(os.path.join(g2, "test_goodtool.py"), "w") as f:
            f.write("import unittest\nimport goodtool\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_double(self):\n"
                    "        self.assertEqual(goodtool.double(3), 6)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n")
        for rp, ap in walk_files(g2).items():
            os.utime(ap, (old, old))
        os.utime(g2, (old, old))
        _st_invoke(env, "intake")
        con = db()
        g2id = con.execute(
            "SELECT id FROM candidates WHERE name='goodtool' AND id<>?",
            (gid,)).fetchone()[0]
        _st_invoke(env, "build", g2id)
        _st_invoke(env, "test", g2id)
        _st_invoke(env, "ready", g2id)
        r = _st_invoke(env, "lock", g2id, "--approved-by", "selftest")
        check("bare-name collision refused",
              r.returncode != 0 and "claimed" in r.stdout)
        r = _st_invoke(env, "drop", gid)
        check("retire requires approver", r.returncode != 0
              and bool(tk)
              and os.path.isdir(os.path.join(toolkit_root(), tk[0])))
        r = _st_invoke(env, "drop", gid, "--approved-by", "selftest")
        check("gated retire clears shelf, link, pool",
              r.returncode == 0 and not os.path.lexists(bare)
              and bool(tk)
              and not os.path.exists(os.path.join(toolkit_root(), tk[0]))
              and not os.path.exists(os.path.join(p_candidates(), gid)))
        r = _st_invoke(env, "lock", g2id, "--approved-by", "selftest")
        check("re-lock lands on new id", r.returncode == 0
              and os.path.islink(bare) and g2id in os.readlink(bare))

        # failure paths: syntax error and missing tests
        bz = os.path.join(p_dropzones(), "moon", "badsyntax")
        os.makedirs(bz)
        with open(os.path.join(bz, "badsyntax.py"), "w") as f:
            f.write("def broken(:\n")
        for rp, ap in walk_files(bz).items():
            os.utime(ap, (old, old))
        os.utime(bz, (old, old))
        nz = os.path.join(p_dropzones(), "moon", "notests")
        os.makedirs(nz)
        with open(os.path.join(nz, "notests.py"), "w") as f:
            f.write("def fine():\n    return 1\n")
        for rp, ap in walk_files(nz).items():
            os.utime(ap, (old, old))
        os.utime(nz, (old, old))
        _st_invoke(env, "intake")
        con = db()
        bid = con.execute(
            "SELECT id FROM candidates WHERE name='badsyntax'").fetchone()[0]
        nid = con.execute(
            "SELECT id FROM candidates WHERE name='notests'").fetchone()[0]
        r = _st_invoke(env, "build", bid)
        check("build fails bad syntax", "FAILED_BUILD" in r.stdout)
        r = _st_invoke(env, "build", nid)
        r = _st_invoke(env, "test", nid)
        check("no tests -> FAILED_TEST", "no_tests" in r.stdout)

        # tamper: mutate a registered candidate, pipeline must freeze it
        tz = os.path.join(p_dropzones(), "han", "tampered")
        os.makedirs(tz)
        with open(os.path.join(tz, "tampered.py"), "w") as f:
            f.write("X = 1\n")
        with open(os.path.join(tz, "test_tampered.py"), "w") as f:
            f.write("import unittest\nimport tampered\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_x(self):\n"
                    "        self.assertEqual(tampered.X, 1)\n")
        for rp, ap in walk_files(tz).items():
            os.utime(ap, (old, old))
        os.utime(tz, (old, old))
        _st_invoke(env, "intake")
        con = db()
        tid = con.execute(
            "SELECT id FROM candidates WHERE name='tampered'").fetchone()[0]
        with open(os.path.join(p_candidates(), tid, "tampered.py"), "w") as f:
            f.write("X = 999\n")
        r = _st_invoke(env, "build", tid)
        check("tamper detected and frozen", "TAMPERED" in r.stdout)

        check("staging empty after all ops",
              os.listdir(p_staging()) == [])
        con = db()
        n_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        check("event ledger populated", n_events >= 12)

        # transaction invariant: no candidate in a transitioned state
        # without an audit row recording an entry into that state.
        orphans = con.execute(
            "SELECT c.id, c.state FROM candidates c WHERE c.state != "
            "'REGISTERED' AND NOT EXISTS (SELECT 1 FROM events e WHERE "
            "e.candidate_id = c.id)"
        ).fetchall()
        check("every transition has an audit row", orphans == [])

        os.environ.pop("ANVIL_ROOT", None)
        os.environ.pop("TOOLKIT_ROOT", None)

    total = passed + len(failed)
    print()
    print("SELFTEST %d/%d %s" % (passed, total,
                                 "PASS" if not failed else "FAIL"))
    if failed:
        for f in failed:
            print("  failed: %s" % f)
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="anvil",
        description="Anvil v1 (bridge mode): shared build service for the "
                    "Treebeard stack. Operator-invoked; no triggers, no "
                    "Meeseeks, no cognition calls in this build.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create directory layout and database")
    sub.add_parser("intake", help="register deposits from all dropzones")
    p = sub.add_parser("build", help="parse/compile/import a candidate")
    p.add_argument("id")
    p = sub.add_parser("test", help="run candidate test suite in the jail")
    p.add_argument("id")
    p = sub.add_parser("ready", help="mark a TESTED candidate READY")
    p.add_argument("id")
    p = sub.add_parser("lock", help="human-gated promotion into the toolkit")
    p.add_argument("id")
    p.add_argument("--approved-by", required=False, default="",
                   help="approver of record (required)")
    p.add_argument("--name", default="",
                   help="override toolkit name (default: candidate name)")
    p = sub.add_parser("drop", help="discard a candidate; retiring a "
                       "LOCKED tool requires --approved-by")
    p.add_argument("id")
    p.add_argument("--approved-by", required=False, default="",
                   help="approver of record (required to retire LOCKED)")
    sub.add_parser("status", help="candidate pool table")
    sub.add_parser("digest", help="print the ready-queue block (Sun hook)")
    sub.add_parser("selftest", help="end-to-end selftest in temp roots")
    args = ap.parse_args(argv)
    return {
        "init": cmd_init, "intake": cmd_intake, "build": cmd_build,
        "test": cmd_test, "ready": cmd_ready, "lock": cmd_lock,
        "drop": cmd_drop, "status": cmd_status, "digest": cmd_digest,
        "selftest": cmd_selftest,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
