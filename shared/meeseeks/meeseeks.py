#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
meeseeks.py -- The Meeseeks Box, v1 (bridge mode).

One file, stdlib only, ASCII throughout. Sibling of anvil.py in the
Treebeard stack.

WHAT THIS IS
    Disposable LLM workers ("Meeseeks") that draft code for one discrete
    task and then end. Work accumulates in a shared, human- and
    agent-readable notepad per project. Nothing a Meeseeks produces is
    durable until it survives the review ladder and Treebeard's gate; the
    durable write is a deposit into Anvil's intake, where the existing
    pipeline (build/test/ready/human lock) still stands between it and
    the toolkit.

OPERATOR SPEC (encoded from the 2026-07-02 requirements)
    R1  A directives file governs Meeseeks behavior; Treebeard owns and
        edits it at will. It is read fresh at every spawn.
    R2  The default directive: finish the task as fast as possible, but a
        Meeseeks may declare CANT (with reason) or request SPLIT into
        smaller chunks.
    R3  When a Meeseeks finishes ("VERDICT: DONE"), the box either spawns
        the next Meeseeks (children from a SPLIT, the review ladder, a
        fix from feedback) or raises the section to Treebeard, who marks
        it done into Anvil (approve -> deposit) or creates another
        Meeseeks (reject / retask).
    R4  Slow and steady: drafted at tier 1; reviewed at tier 1, then tier
        2, then tier 3; only then requested for Treebeard review. The
        frontier tier is never reachable by a Meeseeks.
    R5  The loop repeats until the master task is done.
    R6  Every project has a notepad; every Meeseeks prompt carries the
        notepad manifest and the section's files; the notepad on disk is
        readable by all agents on the box.
    R7  Everything drafted must ship an automated test and be commented.
        Enforced mechanically (gates + a jailed test run) and again by
        every reviewer on the ladder.
    R8  After a project completes, a retrospective is produced: what
        worked, what did not, and SUGGESTED directive changes. The
        suggestions are advisory text only -- this tool never edits the
        directive itself; Treebeard applies changes by editing the file.
    R9  A machine-readable status card is rewritten atomically on every
        transition, for the Sun daily report to ingest.

BRIDGE MODE
    Every command is operator-invoked from the treebeard shell. No
    daemons, no triggers, no autonomous lock-in (Will is Phase 3).
    `step` performs at most one model call; `run` loops steps under a
    hard cap. All model calls go through router.py pinned to a tier and
    fail closed on the per-project call budget. The only place
    Meeseeks-produced code executes is the jailed test run (_jail_run --
    the single seam where the rootless-Podman prefix bolts on when the
    containment wall is built; callers will not change).

STATES
    project: ACTIVE -> NEEDS_RETRO -> CLOSED
    section: QUEUED -> TESTING -> REVIEW1 -> REVIEW2 -> REVIEW3
             -> AWAITING_TREEBEARD -> DONE
             back to QUEUED on any FAIL (fix cycle; the ladder restarts
             from the bottom)
             SPLIT (parent; auto-DONE when all children are DONE)
             BLOCKED (cant | cycles | misfires | oversize | split-depth)

PROTOCOL (code-side contract; the directive is policy layered on top)
    Drafters end with exactly one of:
        VERDICT: DONE   (files supplied as fenced blocks whose first line
                         is "# filename: name"; missing verdict + files
                         present is treated as DONE)
        VERDICT: CANT <reason>
        VERDICT: SPLIT  (subtasks as fenced blocks marked "# subtask")
    Reviewers end with exactly one of:
        VERDICT: PASS
        VERDICT: FAIL <issues>

DEPLOY NOTES
    Ownership (R3 of the Anvil walk applies here too) is set at deploy
    time: treebeard owns the tree; notepads are group-readable so agents
    can see them (R6). Tier names are environment-mapped
    (MEESEEKS_TIERS, default "local,free,cheap") and MUST be verified
    against the deployed router.py before the first live spawn.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Configuration (environment-read at call time so the wrapper pins it)
# --------------------------------------------------------------------------


def _env(name, default):
    v = os.environ.get(name, "")
    return v if v else default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def root():
    return _env("MEESEEKS_ROOT", "/opt/treebeard/shared/meeseeks/data")


def lib_dir():
    return _env("MEESEEKS_LIB", "/opt/treebeard/shared/lib")


def anvil_dropzone():
    return _env("MEESEEKS_ANVIL_DROPZONE",
                "/opt/treebeard/shared/anvil/data/dropzones/treebeard")


def gate_user():
    return _env("MEESEEKS_GATE_USER", "treebeard")


def agent_label():
    return _env("MEESEEKS_AGENT_LABEL", "meeseeks")


def tiers():
    """Tier 1/2/3 -> router force_tier strings. Exactly three, in order.
    R4: there is no fourth entry; the frontier is unreachable from here."""
    raw = _env("MEESEEKS_TIERS", "local,free,cheap")
    out = [t.strip() for t in raw.split(",") if t.strip()]
    if len(out) != 3:
        _die("MEESEEKS_TIERS must name exactly three tiers (t1,t2,t3); got %r" % raw)
    return out


def stages():
    """FACTORY LADDER (PO ruling 2026-08-24): implementation stages, distinct
    from tiers() which drives the 3-tier REVIEW ladder. Stage 2 is GITHUB --
    the cheap tier plus retrieved references, not a fourth model."""
    raw = _env("MEESEEKS_STAGES", "free,cheap")
    out = [t.strip() for t in raw.split(",") if t.strip()]
    if len(out) != 2:
        _die("MEESEEKS_STAGES must name exactly two tiers; got %r" % raw)
    return out


def stage_budget():
    """Attempts per stage: FREE x2 -> CHEAP x2 -> GITHUB x1 -> Treebeard."""
    raw = _env("MEESEEKS_STAGE_BUDGET", "2,2,1")
    try:
        out = [int(x) for x in raw.split(",")]
    except ValueError:
        _die("MEESEEKS_STAGE_BUDGET must be ints; got %r" % raw)
    if len(out) != 3 or any(n < 1 for n in out):
        _die("MEESEEKS_STAGE_BUDGET needs 3 positive ints; got %r" % raw)
    return out


STAGE_NAMES = ("free", "cheap", "github")


def last_stage():
    """Index of the final configured stage. F10: the ladder is a CONFIGURED
    list, not literally three rungs -- nothing below may assume otherwise."""
    return len(STAGE_NAMES) - 1


def _factory_exhausted(con, pid, sect, why, **cols):
    """The canonical terminal state: every configured stage has been tried and
    the factory has nowhere left to go. Jail failure at the last stage and a
    worker CANT at the last stage are the same fact and now say so with one
    name (PO ruling 2026-08-25). Event kinds are unchanged -- event taxonomy
    and state taxonomy are separate, and 'escalate'/'cant' remain true
    descriptions of what happened."""
    upd_section(con, pid, sect["sid"], cols.pop("_kind", "escalate"), why[:400],
                state="BLOCKED", ladder=0,
                blocked_reason="FACTORY_EXHAUSTED: %s" % why[:180], **cols)
    return ("section %s BLOCKED/FACTORY_EXHAUSTED: %s"
            % (sect["sid"], why[:140]))


def stage_tier(stage):
    """Stage -> model tier. Stage 2 (github) reuses the cheap tier."""
    s = stages()
    return s[0] if stage <= 0 else s[1]


def normalize_report(text):
    """Plateau input: strip run-to-run noise so identical failures hash
    identically. Jail dirs, timestamps, durations and addresses vary every
    run and would otherwise defeat detection."""
    import re as _re
    t = text or ""
    t = _re.sub(r"jail-[A-Za-z0-9_]+", "jail-X", t)
    t = _re.sub(r"0x[0-9a-fA-F]+", "0xX", t)
    t = _re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?", "TS", t)
    t = _re.sub(r"\d+\.\d+s", "Ns", t)
    t = _re.sub(r"receipt: \S+", "receipt: X", t)
    return t.strip()


def fail_hash(text):
    import hashlib as _h
    return _h.sha256(normalize_report(text).encode("utf-8", "replace")).hexdigest()[:16]


def retro_tier():
    return _env("MEESEEKS_RETRO_TIER", tiers()[2])


def call_timeout():
    return max(1, _env_int("MEESEEKS_CALL_TIMEOUT", 300))


def cycle_cap():
    return max(1, _env_int("MEESEEKS_CYCLE_CAP", 5))


def misfire_cap():
    return max(1, _env_int("MEESEEKS_MISFIRE_CAP", 2))


def call_cap_default():
    return max(1, _env_int("MEESEEKS_CALL_CAP", 80))


def prompt_cap():
    return max(200, _env_int("MEESEEKS_PROMPT_CAP", 24000))


def split_cap():
    return max(1, _env_int("MEESEEKS_SPLIT_CAP", 12))


def depth_cap():
    return max(1, _env_int("MEESEEKS_SPLIT_DEPTH", 3))


def run_max_default():
    return max(1, _env_int("MEESEEKS_RUN_MAX", 8))


# F1: a reasoning model can spend the whole completion budget on hidden
# reasoning and return no text (K3: 16384 == 16384, twice). The provider's
# only lever is on/off (budgets and effort levels are ignored -- probed
# 2026-08-25), so on that exact failure call_model makes ONE materially
# different retry with thinking disabled, then fails with the cause named.
# Cheap_provider's OpenAI-compatible dialect; a provider that rejects the field
# surfaces an ordinary backend error, which is a normal (non-identical)
# misfire.
_THINKING_OFF = {"thinking": {"type": "disabled"}}
_EXHAUSTED = "reasoning-exhausted"


def max_tokens():
    """Completion cap for Meeseeks work calls (S3-1b fix: the router's
    4096 default starves reasoning models on code-generation tasks --
    chess draft burned all 4096 on reasoning, zero output, twice)."""
    return max(1024, _env_int("MEESEEKS_MAX_TOKENS", 16384))


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

META_FILES = frozenset(["task.md", "feedback.md"])


def db_path():
    return os.path.join(root(), "ledger.db")


def status_path():
    return os.path.join(root(), "status", "meeseeks.txt")


def directives_dir():
    return os.path.join(root(), "directives")


def directive_path():
    return os.path.join(directives_dir(), "default.md")


def staging_dir():
    return os.path.join(root(), "staging")


def projects_dir():
    return os.path.join(root(), "projects")


def proj_dir(pid):
    return os.path.join(projects_dir(), pid)


def notepad_dir(pid):
    return os.path.join(proj_dir(pid), "notepad")


def section_dir(pid, sid):
    return os.path.join(notepad_dir(pid), sid)


def transcripts_dir(pid):
    return os.path.join(proj_dir(pid), "transcripts")


def retro_path(pid):
    return os.path.join(proj_dir(pid), "retro.md")


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _die(msg):
    sys.stderr.write("meeseeks: %s\n" % msg)
    sys.exit(2)


def atomic_write(path, text, encoding="ascii"):
    """Write-temp-then-rename, fsynced. Multi-writer future is guaranteed,
    so every shared-store write is atomic from day one (Anvil discipline)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    done = False
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors="replace") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        done = True
    finally:
        if not done and os.path.exists(tmp):
            os.unlink(tmp)
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass


def group_dirs(path):
    """R6: notepads must be visible to every agent on the box. Dirs come up
    setgid group-readable; the authoritative chown pass is deploy-time."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o2775)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Ledger (SQLite). Anvil discipline carried over whole: a state change and
# its audit row commit in ONE transaction -- either both land or neither.
# --------------------------------------------------------------------------

def _migrate(con):
    # Idempotent forward migration (factory refactor 2026-08-24).
    # CREATE TABLE IF NOT EXISTS skips existing tables, so columns
    # added after a store was created must be applied here too.
    want = {
        "sections": [("stage", "INTEGER NOT NULL DEFAULT 0"),
                     ("stage_attempts", "INTEGER NOT NULL DEFAULT 0"),
                     ("fail_hash", "TEXT NOT NULL DEFAULT ''"),
                     # F10: a CANT is evidence about the STAGE, not the task.
                     ("cant_stage", "INTEGER NOT NULL DEFAULT -1"),
                     ("cant_reason", "TEXT NOT NULL DEFAULT ''"),
                     ("cant_attempt", "INTEGER NOT NULL DEFAULT 0"),
                     ("cant_claim_status", "TEXT NOT NULL DEFAULT ''")],
        "projects": [("commissioned_by", "TEXT NOT NULL DEFAULT ''")],
    }
    for table, cols in want.items():
        try:
            have = {r[1] for r in
                    con.execute("PRAGMA table_info(%s)" % table)}
        except Exception:
            continue
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s"
                            % (table, name, decl))


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS projects(
        pid TEXT PRIMARY KEY, name TEXT NOT NULL, master TEXT NOT NULL,
        state TEXT NOT NULL, calls_used INTEGER NOT NULL DEFAULT 0,
        call_cap INTEGER NOT NULL, created TEXT NOT NULL,
        updated TEXT NOT NULL,
        commissioned_by TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS sections(
        pid TEXT NOT NULL, sid TEXT NOT NULL, parent TEXT,
        title TEXT NOT NULL, state TEXT NOT NULL,
        ladder INTEGER NOT NULL DEFAULT 0, cycles INTEGER NOT NULL DEFAULT 0,
        misfires INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL DEFAULT 0,
        blocked_reason TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
        stage INTEGER NOT NULL DEFAULT 0,
        stage_attempts INTEGER NOT NULL DEFAULT 0,
        fail_hash TEXT NOT NULL DEFAULT '',
        cant_stage INTEGER NOT NULL DEFAULT -1,
        cant_reason TEXT NOT NULL DEFAULT '',
        cant_attempt INTEGER NOT NULL DEFAULT 0,
        cant_claim_status TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (pid, sid))""",
    """CREATE TABLE IF NOT EXISTS meeseeks(
        pid TEXT NOT NULL, mid TEXT NOT NULL, sid TEXT,
        role TEXT NOT NULL, tier TEXT NOT NULL, model TEXT,
        verdict TEXT NOT NULL, prompt_sha TEXT NOT NULL,
        transcript TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
        PRIMARY KEY (pid, mid))""",
    """CREATE TABLE IF NOT EXISTS events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        pid TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL,
        detail TEXT NOT NULL)""",
]


def connect():
    os.makedirs(root(), exist_ok=True)
    con = sqlite3.connect(db_path(), isolation_level=None)
    con.row_factory = sqlite3.Row
    _migrate(con)
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def transition(con, pid, ref, kind, detail, sql=None, params=()):
    """The one path for state changes. UPDATE/INSERT plus the audit event
    in a single BEGIN/COMMIT with ROLLBACK on failure. There is no second,
    non-transactional path for a later edit to reach for."""
    cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if sql:
            cur.execute(sql, params)
        cur.execute(
            "INSERT INTO events(ts, pid, ref, kind, detail) VALUES(?,?,?,?,?)",
            (utcnow(), pid, ref, kind, (detail or "")[:2000]))
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def get_project(con, pid):
    return con.execute("SELECT * FROM projects WHERE pid=?", (pid,)).fetchone()


def get_section(con, pid, sid):
    return con.execute("SELECT * FROM sections WHERE pid=? AND sid=?",
                       (pid, sid)).fetchone()


def upd_section(con, pid, sid, kind, detail, **cols):
    """Column names originate in this file only; values are parameterized."""
    cols["updated"] = utcnow()
    sets = ", ".join("%s=?" % k for k in cols)
    params = list(cols.values()) + [pid, sid]
    transition(con, pid, sid, kind, detail,
               "UPDATE sections SET %s WHERE pid=? AND sid=?" % sets, params)


# --------------------------------------------------------------------------
# Directive (R1/R2: policy, Treebeard-owned, read fresh at every spawn) and
# the format contract (code-owned: editing the directive cannot break the
# parser; the parser cannot be loosened from the directive).
# --------------------------------------------------------------------------

DEFAULT_DIRECTIVE = """MEESEEKS DIRECTIVE (default)
Owned and edited by Treebeard. Read fresh at every spawn, so edits take
effect on the very next Meeseeks. Policy lives here; the response format
contract lives in the box and is not editable from this file.

Finish your task in as short a time as possible. Do not gold-plate, do
not wander, do not add scope. Ship the smallest correct thing.

You are allowed -- and expected -- to refuse honestly:
  - If you cannot do the task, say so and why (VERDICT: CANT <reason>).
  - If the task is too large to finish quickly and well, request that it
    be broken into smaller chunks (VERDICT: SPLIT with subtask prompts).

Every code file you produce must be properly commented (intent, not
noise), and every behavior must ship with an automated test (a test_*.py
using unittest, standard library only). Plain ASCII only.
"""

FORMAT_RULES = """RESPONSE FORMAT (parsed mechanically -- follow it exactly):

Supply each file as ONE fenced code block. The first line inside the fence
must be a filename marker, then the complete file content:

```
# filename: evens.py
<full file content here>
```

Rules that are enforced and will fail your work if broken:
  - Emit each file EXACTLY ONCE. Do not preview the code first and then
    repeat it. Do not write a placeholder like "<code as above>" or
    "# see above" -- the fenced block must contain the real, complete file.
  - Do NOT write any prose, summary, or explanation before or between the
    fenced blocks. Blocks first; the verdict last; nothing else.
  - The filename marker line is required and must be the first line inside
    the fence: either "# filename: NAME" or just "# NAME".

To request a split instead of files (only with VERDICT: SPLIT), give each
subtask as its own fenced block marked as a subtask:

```
# subtask
<one complete, self-contained task prompt>
```

End your reply with exactly one verdict line and NOTHING after it:
VERDICT: DONE
VERDICT: CANT <short reason>
VERDICT: SPLIT

If you supply file blocks and give no verdict, DONE is assumed; but always
state it. CANT and SPLIT must be stated explicitly or they do not count.
"""

REVIEW_RULES = """RESPONSE FORMAT (parsed mechanically -- follow it exactly):
Judge the files against the task. FAIL unless all of these hold:
1. The code does what the task asks -- nothing missing, nothing invented.
2. An automated test file (test_*.py, unittest) exists and meaningfully
   exercises the behavior; a trivial always-pass does not count.
3. Code files are properly commented (intent, not noise).
4. Plain ASCII; standard library only unless the task says otherwise.
List concrete issues; do not rewrite the code yourself.
End your reply with exactly one verdict line and nothing after it:
VERDICT: PASS
VERDICT: FAIL <issues>
"""

DRAFT_SYSTEM = ("You are a Meeseeks: a disposable worker that exists for "
                "exactly one task and then ends. Follow the directive and "
                "the format contract exactly. Existence is pain; finish the "
                "task.")

REVIEW_SYSTEM = ("You are a Meeseeks reviewer: one strict, honest review, "
                 "then you end. You judge; you do not rewrite.")

RETRO_SYSTEM = ("You are Treebeard's retrospective faculty. One project is "
                "done. Say plainly what worked and what did not, grounded "
                "only in the record given. Your suggestions are advisory; "
                "you change nothing yourself.")


def load_directive():
    try:
        with open(directive_path(), "r", encoding="ascii",
                  errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return DEFAULT_DIRECTIVE


# --------------------------------------------------------------------------
# Parsers (the mechanical contract)
# --------------------------------------------------------------------------

# A fenced block: ```[optional lang]\n <body> \n```. Body is captured whole;
# the filename/subtask marker is peeled off the first body line afterward, so
# a model that adds a language hint (```python) still parses.
_FENCE_RE = re.compile(
    r"(?ms)^[ \t]*```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?^[ \t]*```[ \t]*$")
# First-line filename marker inside a fence, two accepted forms:
#   explicit:  "# filename: name"  -- capture is permissive so a traversal
#              attempt is CAPTURED then rejected downstream, not silently
#              dropped.
#   bare:      "# name.ext"        -- a bare comment is only treated as a
#              marker if it is a single dotted token with no spaces, so an
#              ordinary first-line code comment ("# adds two integers") is
#              NOT mistaken for a filename.
_FNAME_EXPLICIT_RE = re.compile(
    r"^[ \t]*#[ \t]*filename:[ \t]*([./A-Za-z0-9][A-Za-z0-9_./-]*)[ \t]*$")
_FNAME_BARE_RE = re.compile(
    r"^[ \t]*#[ \t]*([./A-Za-z0-9][A-Za-z0-9_./-]*\.[A-Za-z0-9]+)[ \t]*$")
# First-line subtask marker inside a fence: "# subtask".
_SUBMARK_RE = re.compile(r"^[ \t]*#[ \t]*subtask\b[ \t]*$", re.IGNORECASE)
_VERDICT_RE = re.compile(
    r"(?m)^[ \t]*VERDICT:[ \t]*(DONE|CANT|SPLIT|PASS|FAIL)\b[ \t]*(.*?)[ \t]*$")
_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*$")
# A bare placeholder body the models emit instead of real content.
_PLACEHOLDER_RE = re.compile(
    r"(?i)(?:as above|see above|code lines as above|omitted|unchanged|"
    r"\.\.\.\s*$|<[^>\n]*>)")


def parse_verdict(text, allowed):
    """Last VERDICT line wins. Returns (word, rest, error)."""
    hits = _VERDICT_RE.findall(text or "")
    if not hits:
        return None, None, "no VERDICT line in response"
    word, rest = hits[-1]
    if word not in allowed:
        return None, None, ("verdict %r not allowed here (allowed: %s)"
                            % (word, "/".join(sorted(allowed))))
    return word, rest.strip(), None


def parse_files(text):
    """Fenced blocks whose first line is a filename marker -> [(name, body)].
    Names are sanitized (relative, ASCII, no traversal); bodies must be ASCII
    (stack discipline) and must not be a bare placeholder (the models' habit
    of gesturing at code instead of emitting it). Blocks marked '# subtask'
    are ignored here. Raises ValueError -- the caller turns that into a
    misfire, never a write."""
    out = []
    # F-fix: merged-fence split. Models sometimes emit several files
    # in ONE fence with interior "# filename:" markers; taking only
    # the first line as the marker silently corrupts file one with
    # the concatenated remainder (hello2, 2026-07-17). Cut each fence
    # at every interior marker line so each segment parses alone.
    blocks = []
    for block in _FENCE_RE.findall(text or ""):
        seg = block.split("\n")
        cuts = [0] + [i for i, ln in enumerate(seg) if i > 0 and
                      (_FNAME_EXPLICIT_RE.match(ln)
                       or _SUBMARK_RE.match(ln))]
        for a, b in zip(cuts, cuts[1:] + [len(seg)]):
            blocks.append("\n".join(seg[a:b]))
    for block in blocks:
        lines = block.split("\n", 1)
        first = lines[0]
        if _SUBMARK_RE.match(first):
            continue  # a subtask fence, not a file
        m = _FNAME_EXPLICIT_RE.match(first) or _FNAME_BARE_RE.match(first)
        if not m:
            # A fenced block with no filename marker: could be a stray preview.
            # Ignore it rather than guess a name -- if it was meant to be a
            # file, the missing marker is the model's error and DONE will land
            # with fewer files than intended, caught downstream.
            continue
        name = m.group(1).strip()
        if not _NAME_RE.match(name) or ".." in name.split("/"):
            raise ValueError("illegal file name %r" % name)
        body = lines[1] if len(lines) > 1 else ""
        try:
            body.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("non-ascii content in %r" % name)
        if not body.strip():
            raise ValueError("empty body for %r" % name)
        # Reject a body that is only a placeholder gesture (whole-body check:
        # a real file that happens to contain "<x>" in a string is fine, but a
        # body that is *nothing but* a placeholder line is the failure we saw).
        stripped = body.strip()
        if _PLACEHOLDER_RE.fullmatch(stripped) or stripped in (
                "<code>", "<code as above>", "..."):
            raise ValueError("placeholder body for %r (%r)" % (name, stripped[:40]))
        if not body.endswith("\n"):
            body += "\n"
        out.append((name, body))
    return out


def parse_subtasks(text):
    """Fenced blocks whose first line is '# subtask' -> [body]."""
    out = []
    for block in _FENCE_RE.findall(text or ""):
        lines = block.split("\n", 1)
        if _SUBMARK_RE.match(lines[0]):
            body = (lines[1] if len(lines) > 1 else "").strip()
            if body:
                out.append(body)
    return out


# --------------------------------------------------------------------------
# Notepad (R6): per-project shared scratch. task.md and feedback.md are
# meta; everything else in a section dir is the candidate file set.
# --------------------------------------------------------------------------


def read_task(pid, sid):
    p = os.path.join(section_dir(pid, sid), "task.md")
    try:
        with open(p, "r", encoding="ascii", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def read_feedback(pid, sid):
    p = os.path.join(section_dir(pid, sid), "feedback.md")
    try:
        with open(p, "r", encoding="ascii", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def append_feedback(pid, sid, header, text):
    old = read_feedback(pid, sid)
    entry = "--- %s %s ---\n%s\n" % (utcnow(), header,
                                     (text or "").strip()[:4000])
    combined = (old + "\n" + entry) if old else entry
    if len(combined) > 24000:
        combined = combined[-24000:]
    atomic_write(os.path.join(section_dir(pid, sid), "feedback.md"), combined)


def section_files(pid, sid):
    """The section's code files (meta and dotfiles excluded), sorted."""
    base = section_dir(pid, sid)
    out = {}
    if not os.path.isdir(base):
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), base)
            if rel in META_FILES or fn.startswith("."):
                continue
            with open(os.path.join(dirpath, fn), "r", encoding="ascii",
                      errors="replace") as fh:
                out[rel] = fh.read()
    return out


def clear_section_files(pid, sid):
    """A fix replaces the file set wholesale so a dropped file cannot
    linger and poison the jail run. Meta is preserved."""
    base = section_dir(pid, sid)
    if not os.path.isdir(base):
        return
    for entry in sorted(os.listdir(base)):
        if entry in META_FILES or entry.startswith("."):
            continue
        p = os.path.join(base, entry)
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def write_section_files(pid, sid, files):
    clear_section_files(pid, sid)
    base = section_dir(pid, sid)
    for name, body in files:
        dest = os.path.join(base, name)
        group_dirs(os.path.dirname(dest))
        atomic_write(dest, body)


def notepad_manifest(pid):
    base = notepad_dir(pid)
    lines = []
    if os.path.isdir(base):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                lines.append("%s (%d bytes)" % (rel, size))
    if len(lines) > 200:
        lines = lines[:200] + ["... (%d more)" % (len(lines) - 200)]
    return "\n".join(lines) if lines else "(empty)"


# --------------------------------------------------------------------------
# Prompt builders
# --------------------------------------------------------------------------


def _files_block(files):
    parts = []
    for name in sorted(files):
        body = files[name]
        if not body.endswith("\n"):
            body += "\n"
        parts.append("```\n# filename: %s\n%s```" % (name, body))
    return "\n".join(parts)


def references_dir(pid):
    return os.path.join(proj_dir(pid), "references")


def read_references(pid, sid):
    """Persisted references for this section, or []. Never raises."""
    p = os.path.join(section_dir(pid, sid), "references.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("references", [])
    except Exception:
        return []


def acquire_references(pid, sid, task_text, feedback):
    """STAGE 2 (GITHUB): bounded acquisition before escalating to Treebeard.
    Fail-soft by contract -- ANY failure yields no references and the stage
    proceeds as if retrieval found nothing. Retrieval must never be the
    reason a section dies."""
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        import github_search as gh
        import re as _re
        fails = _re.findall(r"(test_[A-Za-z0-9_]+)", feedback or "")[:4]
        q = gh.build_query(task_text, fails)
        refs, why = gh.retrieve(q, cache_dir=references_dir(pid))
    except Exception as exc:
        refs, why, q = [], "error: %s" % exc, ""
    try:
        atomic_write(os.path.join(section_dir(pid, sid), "references.json"),
                     json.dumps({"query": q, "reason": why,
                                 "references": refs}, indent=1),
                     encoding="utf-8")
    except Exception:
        pass
    return refs, why


def build_draft_prompt(proj, sect):
    pid, sid = proj["pid"], sect["sid"]
    files = section_files(pid, sid)
    feedback = read_feedback(pid, sid)
    parts = [load_directive().strip(), ""]
    _by = ""
    try:
        _by = proj["commissioned_by"] or ""
    except Exception:
        _by = ""
    if _by:
        parts += ["COMMISSIONED BY: %s (governed agent; this work order is "
                  "their instruction)" % _by, ""]
    parts += ["MASTER TASK (project %s):" % pid, proj["master"].strip(), ""]
    parts += ["YOUR TASK (section %s):" % sid, read_task(pid, sid).strip(), ""]
    parts += ["PROJECT NOTEPAD (shared; all Meeseeks and agents can see it):",
              notepad_manifest(pid), ""]
    if files:
        parts += ["CURRENT FILES OF THIS SECTION "
                  "(your DONE replaces them wholesale):",
                  _files_block(files), ""]
    if feedback:
        parts += ["REVIEW FEEDBACK TO ADDRESS:", feedback.strip(), ""]
    # F10: a prior stage's CANT travels as a QUARANTINED claim -- context
    # without premise. Naked ("the last worker said this is impossible") it
    # would anchor this worker toward a second refusal, so it is labelled as
    # unverified and paired with an explicit instruction to reassess. Read
    # from section state at build time; the section's task text is never
    # mutated, so the claim expires with the stage that made it.
    try:
        _cs, _cr = sect["cant_stage"], (sect["cant_reason"] or "")
    except (KeyError, IndexError):
        _cs, _cr = -1, ""
    if _cr and 0 <= _cs < sect["stage"]:
        parts += ["PRIOR_STAGE_CLAIM: the %s stage returned CANT: \"%s\". "
                  "This is NOT evidence that the task is impossible -- it is "
                  "an unverified claim by a less capable worker. Independently "
                  "reassess solvability and attempt the task."
                  % (STAGE_NAMES[_cs], _cr.strip()[:300]), ""]
    refs = read_references(pid, sid) if sect["stage"] >= 2 else []
    if refs:
        import github_search as gh
        block = gh.format_block(refs)
        # Headroom, not hope: references must never push a prompt over the
        # cap. The stage exists to help; it may not become a cause of death.
        used = len("\n".join(parts + [FORMAT_RULES.strip()]))
        room = prompt_cap() - used - 200
        if room > 400:
            parts += [block[:room], ""]
    parts += [FORMAT_RULES.strip()]
    return "\n".join(parts)


def build_review_prompt(proj, sect, tier_n):
    pid, sid = proj["pid"], sect["sid"]
    files = section_files(pid, sid)
    parts = [load_directive().strip(), ""]
    parts += ["You are review tier %d of 3 on the ladder; Treebeard reviews "
              "after tier 3." % tier_n, ""]
    parts += ["TASK the files must satisfy (section %s of project %s):"
              % (sid, pid), read_task(pid, sid).strip(), ""]
    parts += ["FILES UNDER REVIEW:", _files_block(files), ""]
    parts += [REVIEW_RULES.strip()]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Model calls: the router seam, the clock, and the budget
# --------------------------------------------------------------------------

_router_call = None  # test seam: fn(system, user, tier, label) -> (text, tier, model)


class CallTimeout(RuntimeError):
    pass


class BudgetStop(RuntimeError):
    pass


def _alarm(signum, frame):
    raise CallTimeout("model call exceeded %ds" % call_timeout())


def _live_router(system, user, tier, label, gen_extra=None):
    """Reconciled against the deployed router.py signature:
        route(system, user, *, force_tier=None, strict=False, agent=...) -> Result
    If the signature changes, this is the only place to fix.

    strict=True is UNCONDITIONAL for Meeseeks: a disposable task agent must run
    on exactly the tier it was assigned or fail loud (the not-ok raise below).
    Silent degradation to a weaker tier would violate the die-when-it-can't-do-
    the-job contract -- a Meeseeks returning local_model_family output when it asked for
    Tier 2 is worse than a Meeseeks that dies. (Board item #4.)"""
    if lib_dir() not in sys.path:
        sys.path.insert(0, lib_dir())
    import router  # noqa: PLC0415
    extra_kw = {"gen_extra": gen_extra} if gen_extra else {}
    res = router.route(system, user, force_tier=tier, strict=True, agent=label,
                       governed=False, factory_caller="meeseeks",
                       max_tokens=max_tokens(), **extra_kw)
    if not getattr(res, "ok", False):
        raise RuntimeError("router not-ok: %s" % getattr(res, "error", "?"))
    text = getattr(res, "text", "") or ""
    if not text.strip():
        raise RuntimeError("router returned no text")
    return text, getattr(res, "tier", tier), getattr(res, "model", "?")


def call_model(system, user, tier):
    """One governed call under a wall clock (die-when-done includes
    die-when-stuck). F1: if the call fails because the completion budget
    went entirely to hidden reasoning, retry ONCE with thinking disabled --
    a different request, not a replay -- then let the named cause propagate
    (it is classed deterministic, so the caller blocks, never re-pays)."""
    fn = _router_call or _live_router

    def _run(**kw):
        if hasattr(signal, "SIGALRM"):
            old = signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(call_timeout())
            try:
                return fn(system, user, tier, agent_label(), **kw)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
        return fn(system, user, tier, agent_label(), **kw)

    try:
        return _run()
    except RuntimeError as exc:
        _m = str(exc).lower()
        # A+ ROUTER (2026-09-04): the router now makes the thinking-off
        # retry in-rung itself and marks a spent rung "thinking-off-retried".
        # Retrying here again would be a third paid call for a cause already
        # proven deterministic -- stand down and let it propagate.
        if _EXHAUSTED not in _m or "thinking-off-retried" in _m:
            raise
        sys.stderr.write("[meeseeks] F1: %s on tier %s -- one retry with "
                         "thinking disabled\n" % (_EXHAUSTED, tier))
        return _run(gen_extra=dict(_THINKING_OFF))


def _spend(con, pid, note):
    """Fail-closed: refuse BEFORE the call; count the attempt (a crashed
    call may still have been charged by the provider)."""
    proj = get_project(con, pid)
    if proj["calls_used"] >= proj["call_cap"]:
        raise BudgetStop(
            "budget exhausted: %d/%d calls used (raise with: meeseeks fund %s "
            "--calls N --by <you>)" % (proj["calls_used"], proj["call_cap"], pid))
    transition(con, pid, pid, "call", note,
               "UPDATE projects SET calls_used=calls_used+1, updated=? "
               "WHERE pid=?", (utcnow(), pid))


def _next_mid(con, pid):
    n = con.execute("SELECT COUNT(*) FROM meeseeks WHERE pid=?",
                    (pid,)).fetchone()[0]
    return "m%04d" % (n + 1)


def _write_transcript(path, system, user, response):
    body = ("== SYSTEM ==\n%s\n\n== USER ==\n%s\n\n== RESPONSE ==\n%s\n"
            % (system, user, response))
    atomic_write(path, body, encoding="utf-8")


def spawn(con, pid, sid, role, tier, system, user):
    """One Meeseeks = one governed model call, fully audited: budget row,
    spawn row, transcript on disk, end/error row. Returns (mid, text)."""
    _spend(con, pid, "%s %s tier=%s" % (role, sid or "-", tier))
    mid = _next_mid(con, pid)
    tdir = transcripts_dir(pid)
    group_dirs(tdir)
    tpath = os.path.join(tdir, "%s.txt" % mid)
    sha = hashlib.sha256(user.encode("ascii", "replace")).hexdigest()[:16]
    transition(con, pid, mid, "spawn",
               "%s %s tier=%s sid=%s" % (mid, role, tier, sid or "-"),
               "INSERT INTO meeseeks(pid, mid, sid, role, tier, model, "
               "verdict, prompt_sha, transcript, started) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (pid, mid, sid, role, tier, None, "PENDING", sha, tpath,
                utcnow()))
    try:
        text, tused, model = call_model(system, user, tier)
    except Exception as exc:
        transition(con, pid, mid, "meeseek_error",
                   "%s: %s" % (type(exc).__name__, exc),
                   "UPDATE meeseeks SET ended=?, verdict='ERROR' "
                   "WHERE pid=? AND mid=?", (utcnow(), pid, mid))
        _write_transcript(tpath, system, user,
                          "<< CALL FAILED: %s: %s >>"
                          % (type(exc).__name__, exc))
        raise
    _write_transcript(tpath, system, user, text)
    transition(con, pid, mid, "meeseek_end", "%s model=%s" % (mid, model),
               "UPDATE meeseeks SET ended=?, model=? WHERE pid=? AND mid=?",
               (utcnow(), model, pid, mid))
    return mid, text


def set_verdict(con, pid, mid, verdict):
    transition(con, pid, mid, "verdict", "%s -> %s" % (mid, verdict),
               "UPDATE meeseeks SET verdict=? WHERE pid=? AND mid=?",
               (verdict, pid, mid))


# --------------------------------------------------------------------------
# The jail: the ONLY place Meeseeks-produced code executes in this box.
# CONTAINMENT T2 / I1 (Tex 2026-10-02; rev 8, operator P2): it executes as
# uid tbjail, through `sudo -n -u tbjail /usr/bin/python3 -IB
# ctl/jail_prep.py request` -> the frozen ctl/jail_launch.py. The sudo argv
# is FIXED, token for token the one sudoers rule (rev 8.1: explicit
# interpreter, single "-IB" token, no wildcard); every variable input -- timeout, mode,
# args -- goes on stdin as one JSON header line ahead of the tar. There is
# NO fallback to a treebeard-uid run: an unsupported call shape, a missing
# sudo rule or a jail abort is a failed run, never a local one.
# --------------------------------------------------------------------------

JAIL_SUDO = "/usr/bin/sudo"
JAIL_USER = "tbjail"
JAIL_PY = "/usr/bin/python3"
JAIL_PREP = "/opt/treebeard/shared/ctl/jail_prep.py"
JAIL_ARGV = [JAIL_SUDO, "-n", "-u", JAIL_USER, JAIL_PY, "-IB", JAIL_PREP, "request"]
JAIL_PACK_MAX = 32 * 1024 * 1024
# Test seam: cmd_selftest swaps in _jail_run_local for the duration of the
# selftest only. Production imports leave it None (asserted by the selftest).
_JAIL_IMPL = None

# --------------------------------------------------------------------------
# Containment (Option C, C3 / D9): is code execution PROVISIONED on this box?
# A NON-EXECUTING check, as the service account: lookups and reads only --
# no sudo, no helper, no probe run, no subprocess. Every unprovisioned path
# ends in the ONE message below, with nothing appended; the failing check is
# reported by root-side `provision-containment.sh --check` and preflight,
# which can see what this account cannot (/etc/sudoers.d is root 0750).
#  (a) accounts tbjail and tbctl exist (jail_launch _resolve_uid_gid);
#  (b) ctl/jail_prep.py, ctl/jail_launch.py, ctl/board_writer.py are
#      root-owned regular files, not group/other-writable, with sha256 equal
#      to their ctl/T2_MANIFEST line (itself root-owned, not writable);
#  (c) runs/tbjail is a directory owned by tbjail, mode 0700
#      (jail_prep _dir_problem);
#  (e) the sudo binary JAIL_SUDO is a root-owned setuid regular file, not
#      group/other-writable: os.stat (follows the alternatives link), never
#      executed (DC-8, ruling 2026-10-08 23:15; C3.1 check 4);
#  (d) a root-written record of the sudoers fragment: ctl/PROVISIONED
#      (root:root, not writable) with `recorded /etc/sudoers.d/treebeard-
#      containment <sha256>`, OR the T2 deploy record's T2_MANIFEST line
#      `recorded /etc/sudoers.d/treebeard-t2 <sha256>` (the reference box).
# Known limit (R-C3-1): a fragment deleted or edited after provisioning is
# not visible here -- the check still says PROVISIONED, root-side --check /
# preflight say INCONSISTENT, and the jail run fails closed (sudo refuses).
# --------------------------------------------------------------------------
CONTAINMENT_MSG = "execution unavailable: not provisioned"
CTL_DIR = "/opt/treebeard/shared/ctl"
JAIL_SCRATCH = "/opt/treebeard/shared/toolbox/data/runs/tbjail"
_SUDOERS_RECORDS = (("PROVISIONED", "/etc/sudoers.d/treebeard-containment"),
                    ("T2_MANIFEST", "/etc/sudoers.d/treebeard-t2"))
_CONTAINMENT = None   # per-process cache; the selftest sets it, production computes it once


def _containment_check(pw, lst, rd, stf=os.stat):
    """(verdict, failing check). pw/lst/rd/stf are pwd.getpwnam, os.lstat, a
    bytes reader and os.stat: lookups only. Any error is NOT PROVISIONED."""
    import stat as st_

    def root_file(p):
        try:
            s = lst(p)
        except OSError:
            return "absent"
        if not st_.S_ISREG(s.st_mode):
            return "not a regular file"
        if s.st_uid != 0:
            return "not root-owned"
        if s.st_mode & 0o022:
            return "group/other-writable"
        return None

    try:
        for u in ("tbjail", "tbctl"):
            try:
                pw(u)
            except KeyError:
                return "NOT PROVISIONED", "(a) account %s absent" % u
        jail_uid = pw("tbjail").pw_uid
        man = CTL_DIR + "/T2_MANIFEST"
        why = root_file(man)
        if why:
            return "NOT PROVISIONED", "(b) %s %s" % (man, why)
        pins = {}
        for line in rd(man).decode("utf-8", "replace").splitlines():
            f = line.split()
            if len(f) >= 3 and f[0] in ("sha256", "recorded"):
                pins[(f[0], f[1])] = f[2]
        for name in ("jail_prep.py", "jail_launch.py", "board_writer.py"):
            p = CTL_DIR + "/" + name
            why = root_file(p)
            if why:
                return "NOT PROVISIONED", "(b) %s %s" % (p, why)
            if pins.get(("sha256", p)) != hashlib.sha256(rd(p)).hexdigest():
                return "NOT PROVISIONED", "(b) %s does not match T2_MANIFEST" % p
        try:
            s = lst(JAIL_SCRATCH)
        except OSError:
            return "NOT PROVISIONED", "(c) %s absent" % JAIL_SCRATCH
        if not st_.S_ISDIR(s.st_mode) or s.st_uid != jail_uid \
                or st_.S_IMODE(s.st_mode) != 0o700:
            return "NOT PROVISIONED", "(c) %s not a tbjail 0700 directory" % JAIL_SCRATCH
        try:
            s = stf(JAIL_SUDO)
        except OSError:
            return "NOT PROVISIONED", "(e) %s absent" % JAIL_SUDO
        if not st_.S_ISREG(s.st_mode) or s.st_uid != 0 \
                or not s.st_mode & st_.S_ISUID or s.st_mode & 0o022:
            return "NOT PROVISIONED", "(e) %s is not a root-owned setuid regular file" % JAIL_SUDO
        for rec, frag in _SUDOERS_RECORDS:
            p = CTL_DIR + "/" + rec
            if root_file(p):
                continue
            for line in rd(p).decode("utf-8", "replace").splitlines():
                f = line.split()
                if len(f) == 3 and f[0] == "recorded" and f[1] == frag \
                        and re.fullmatch(r"[0-9a-f]{64}", f[2]):
                    return "PROVISIONED", None
        return "NOT PROVISIONED", "(d) no root-written sudoers record"
    except Exception as exc:     # an unreadable record is not a provisioned box
        return "NOT PROVISIONED", "error: %s" % type(exc).__name__


def _read_bytes(p):
    with open(p, "rb") as f:
        return f.read(1 << 20)


def containment_state():
    """'PROVISIONED' or 'NOT PROVISIONED' (cached per process)."""
    global _CONTAINMENT
    if _CONTAINMENT is None:
        import pwd
        _CONTAINMENT = _containment_check(pwd.getpwnam, os.lstat, _read_bytes, os.stat)[0]
    return _CONTAINMENT


def _rlimit_setter():
    import resource  # noqa: PLC0415

    def setter():
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_AS,
                           (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (16 * 1024 * 1024, 16 * 1024 * 1024))
    return setter


def _jail_run_local(argv, cwd, timeout=90):
    """SELFTEST ONLY: the pre-T2 runner, executing as the CALLING uid. It is
    installed by cmd_selftest for its own duration and never otherwise."""
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": cwd, "LC_ALL": "C"}
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout,
                           preexec_fn=_rlimit_setter())
    except subprocess.TimeoutExpired as te:
        out = te.stdout or ""
        err = te.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err + "\nKILLED: wall timeout"
    return p.returncode, p.stdout or "", p.stderr or ""


def _jail_rel(path, cwd):
    """A path inside cwd, as a relative path; None if it escapes."""
    full = os.path.normpath(path if os.path.isabs(path)
                            else os.path.join(cwd, path))
    root = os.path.normpath(cwd)
    if not full.startswith(root + os.sep):
        return None
    return os.path.relpath(full, root)


def _jail_mode(argv, cwd):
    """The fixed call shapes -> jail_prep mode. None = refused. The jail
    pins its own interpreter (/usr/bin/python3); argv[0] is not used."""
    a = list(argv[1:])
    if len(a) == 3 and a[:2] == ["-m", "py_compile"]:
        rel = _jail_rel(a[2], cwd)
        return None if rel is None else ["py_compile", rel]
    if a == ["-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"]:
        return ["unittest-discover"]
    if len(a) == 4 and a[:3] == ["-m", "unittest", "-v"]:
        return ["unittest", a[3]]
    if a and not a[0].startswith("-"):
        rel = _jail_rel(a[0], cwd)
        return None if rel is None else ["run", rel] + a[1:]
    return None


def _jail_pack(cwd):
    """cwd -> an uncompressed tar of its directories and regular files.
    Symlinks and special files are skipped, never followed."""
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        # Reviewed walk (test_containment_guard): the root is the caller's
        # jail or workspace directory, never shared/ or an ancestor of it.
        for dp, dns, fns in os.walk(cwd):
            dns[:] = sorted(d for d in dns
                            if not os.path.islink(os.path.join(dp, d)))
            for d in dns:
                ti = tarfile.TarInfo(os.path.relpath(os.path.join(dp, d), cwd))
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)
            for f in sorted(fns):
                full = os.path.join(dp, f)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                with open(full, "rb") as fh:
                    data = fh.read(JAIL_PACK_MAX + 1)
                total += len(data)
                if total > JAIL_PACK_MAX:
                    raise ValueError("jail input exceeds %d bytes"
                                     % JAIL_PACK_MAX)
                ti = tarfile.TarInfo(os.path.relpath(full, cwd))
                ti.size, ti.mode = len(data), 0o644
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _jail_run(argv, cwd, timeout=90):
    """Run one fixed-shape command over a COPY of cwd, as tbjail. Returns
    (rc, stdout, stderr) like subprocess. Files the run writes stay in its
    tbjail scratch, which jail_prep removes."""
    if containment_state() != "PROVISIONED":
        return 126, "", CONTAINMENT_MSG
    if _JAIL_IMPL is not None:
        return _JAIL_IMPL(argv, cwd, timeout)
    mode = _jail_mode(argv, cwd)
    if mode is None:
        return 126, "", ("JAIL_REFUSED: unsupported command shape %r (no "
                         "treebeard-uid fallback exists)" % (list(argv),))
    try:
        data = _jail_pack(cwd)
    except (OSError, ValueError) as exc:
        return 126, "", "JAIL_REFUSED: %s" % exc
    header = json.dumps({"v": 1, "timeout": max(1, int(timeout)),
                         "mode": mode[0], "args": mode[1:]},
                        sort_keys=True).encode("ascii") + b"\n"
    try:
        p = subprocess.run(list(JAIL_ARGV), input=header + data,
                           capture_output=True,
                           timeout=int(timeout) + 30,
                           env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except subprocess.TimeoutExpired as te:
        out = (te.stdout or b"").decode("utf-8", "replace")
        err = (te.stderr or b"").decode("utf-8", "replace")
        return 124, out, err + "\nKILLED: wall timeout (jail did not return)"
    except (FileNotFoundError, PermissionError):
        # the belt for a race between the precheck and the spawn (K1):
        # no exception ever leaves _jail_run
        return 126, "", CONTAINMENT_MSG
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def _section_tests_core(pid, sid, _runs):
    """R7 enforced mechanically before any reviewer spends a call:
    files exist, a test_*.py exists, code files carry comments, everything
    compiles, and the tests pass in the jail. Returns (ok, report)."""
    files = section_files(pid, sid)
    if not files:
        return False, "no files in section"
    pys = [n for n in files if n.endswith(".py")]
    tests = [n for n in pys if os.path.basename(n).startswith("test_")]
    if not tests:
        return False, "R7 gate: no test_*.py file present"
    for n in pys:
        if os.path.basename(n).startswith("test_"):
            continue
        body = files[n]
        if (not re.search(r"(?m)^\s*#", body) and '"""' not in body
                and "'''" not in body):
            return False, "R7 gate: %s has no comments" % n
    group_dirs(staging_dir())
    jail = tempfile.mkdtemp(prefix="jail-", dir=staging_dir())
    try:
        for name, body in files.items():
            dest = os.path.join(jail, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="ascii", errors="replace") as fh:
                fh.write(body)
        # F-fix: unittest discovery only descends into packages, so a
        # test under tests/ passes the R7 gate (basename check) but is
        # invisible to discover -> "Ran 0 tests" (hello2, 2026-07-17).
        # Touch __init__.py in every jail subdirectory.
        for _dp, _dn, _fn in os.walk(jail):
            if _dp != jail:
                _ip = os.path.join(_dp, "__init__.py")
                if not os.path.exists(_ip):
                    open(_ip, "w").close()
        for n in pys:
            rc, out, err = _jail_run([sys.executable, "-m", "py_compile", n],
                                     jail)
            _runs.append(("py_compile %s" % n, rc, out, err))
            if rc != 0:
                return False, ("py_compile failed on %s:\n%s"
                               % (n, (err or out)[-2000:]))
        rc, out, err = _jail_run(
            [sys.executable, "-m", "unittest", "discover", "-s", ".",
             "-p", "test_*.py", "-v"], jail)
        _runs.append(("unittest discover", rc, out, err))
        if rc != 0:
            return False, "tests failed:\n%s" % ((err + "\n" + out)[-3000:])
        return True, "tests passed:\n%s" % ((err + "\n" + out)[-1500:])
    finally:
        shutil.rmtree(jail, ignore_errors=True)


def _receipts_dir(pid):
    return os.path.join(proj_dir(pid), "receipts")


def _write_test_receipt(pid, sid, ok, report, runs):
    """S3-1a durable jail receipt: the FULL command outputs (ledger
    detail and feedback keep their truncations; this file is the
    complete record). Fail-soft: never blocks the pipeline."""
    try:
        d = _receipts_dir(pid)
        group_dirs(d)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = os.path.join(d, "%s-%s-%03d.txt"
                            % (sid, stamp,
                               int(time.time() * 1000) % 1000))
        parts = ["JAIL TEST RECEIPT -- project %s section %s -- %s"
                 % (pid, sid, utcnow()),
                 "RESULT: %s" % ("PASS" if ok else "FAIL"),
                 "REPORT:", report, ""]
        for label, rc, out, err in runs:
            parts += ["== %s rc=%d ==" % (label, rc),
                      "-- stdout --", out if out else "(empty)",
                      "-- stderr --", err if err else "(empty)", ""]
        atomic_write(path, "\n".join(parts), encoding="utf-8")
        return path
    except Exception as exc:
        sys.stderr.write("meeseeks: receipt write failed (non-fatal): %s\n"
                         % exc)
        return None


def run_section_tests(pid, sid):
    """R7 gates + jailed run (_section_tests_core) plus the S3-1a
    durable receipt; the receipt path is prepended to the report so
    ledger detail and fail-cycle feedback carry the pointer."""
    runs = []
    ok, report = _section_tests_core(pid, sid, runs)
    path = _write_test_receipt(pid, sid, ok, report, runs)
    if path:
        report = "receipt: %s\n%s" % (path, report)
    return ok, report


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

REVIEW_STATES = {"REVIEW1": 1, "REVIEW2": 2, "REVIEW3": 3}


# Causes a retry can never fix: the second attempt fails identically and
# burns a paid call. Observed twice on 2026-08-24 (missing TREEBEARD_FREE_API_KEY,
# then cheap budget-check) -- each converted an operator config problem into
# a BLOCKED section two calls later, with the real cause buried under a
# misfire count. Matched case-insensitively against the router error text.
_DETERMINISTIC = (
    "api_key not set",
    "api key not set",
    "budget-check failed",
    "budget exhausted",
    "unavailable:",
    "not a recognized",
    "gate ",
    "context too small",
    "no price for model",
    "reasoning-exhausted",   # F1: completion budget spent on hidden reasoning
)


def _is_deterministic(reason):
    r = (reason or "").lower()
    return any(t in r for t in _DETERMINISTIC)


def _misfire(con, pid, sect, reason):
    """A Meeseeks that produced garbage (or a call that died). Retries a
    bounded number of times, then blocks loud for Treebeard.

    A DETERMINISTIC cause (missing key, budget refusal, gate veto, context
    floor) blocks on the FIRST occurrence: retrying cannot change the
    outcome, and the honest report is the cause, not a misfire tally."""
    if _is_deterministic(reason):
        upd_section(con, pid, sect["sid"], "blocked",
                    "deterministic: %s" % reason[:400],
                    state="BLOCKED", misfires=sect["misfires"] + 1,
                    blocked_reason="deterministic: %s" % reason[:200])
        return ("section %s BLOCKED (deterministic, not retried): %s"
                % (sect["sid"], reason[:160]))
    m = sect["misfires"] + 1
    if m >= misfire_cap():
        upd_section(con, pid, sect["sid"], "blocked",
                    "misfires: %s" % reason[:400],
                    state="BLOCKED", misfires=m,
                    blocked_reason="misfires: %s" % reason[:200])
        return ("section %s BLOCKED after %d misfires (%s)"
                % (sect["sid"], m, reason[:120]))
    upd_section(con, pid, sect["sid"], "misfire", reason[:400], misfires=m)
    return ("section %s misfire %d/%d (%s); step again to retry"
            % (sect["sid"], m, misfire_cap(), reason[:120]))


def _fail_cycle(con, pid, sect, kind, header, report, count=True):
    """Jail fail, review FAIL, or Treebeard reject: feedback recorded, the
    section requeues for a fix at tier 1, and the ladder restarts from the
    bottom. Auto-fails count toward the cycle cap; Treebeard's own rejects
    do not (the cap guards runaway model loops, not the governor)."""
    append_feedback(pid, sect["sid"], header, report)
    stage_cols = {}
    if count and kind == "tests_fail":
        h = fail_hash(report)
        st, att = sect["stage"], sect["stage_attempts"] + 1
        budget = stage_budget()
        plateau = (h == (sect["fail_hash"] or ""))
        done = plateau or att >= budget[min(st, len(budget) - 1)]
        if done and st < last_stage():
            stage_cols = {"stage": st + 1, "stage_attempts": 0, "fail_hash": h}
        elif done:
            # Final configured stage exhausted: the ladder ends at Treebeard.
            return _factory_exhausted(
                con, pid, sect,
                "stages exhausted at %s" % STAGE_NAMES[st],
                stage=st, stage_attempts=att, fail_hash=h,
                cycles=sect["cycles"] + 1)
        else:
            stage_cols = {"stage_attempts": att, "fail_hash": h}
    c = sect["cycles"] + (1 if count else 0)
    if count and c >= cycle_cap():
        upd_section(con, pid, sect["sid"], "blocked",
                    "cycle cap %d hit at %s" % (cycle_cap(), kind),
                    state="BLOCKED", cycles=c, ladder=0,
                    blocked_reason="cycles: cap %d hit at %s"
                                   % (cycle_cap(), kind), **stage_cols)
        return ("section %s BLOCKED: cycle cap %d hit"
                % (sect["sid"], cycle_cap()))
    upd_section(con, pid, sect["sid"], kind, report[:400],
                state="QUEUED", cycles=c, ladder=0, misfires=0,
                blocked_reason=None, **stage_cols)
    return ("section %s -> QUEUED for fix (cycle %d/%d, %s)"
            % (sect["sid"], c, cycle_cap(), kind))


def _next_sid(con, pid):
    n = con.execute("SELECT COUNT(*) FROM sections WHERE pid=?",
                    (pid,)).fetchone()[0]
    return "s%02d" % (n + 1)


def create_section(con, pid, parent, depth, task_text, kind, detail):
    sid = _next_sid(con, pid)
    first = task_text.strip().splitlines()[0] if task_text.strip() else ""
    title = first[:70] if first else "(untitled)"
    now = utcnow()
    transition(con, pid, sid, kind, detail,
               "INSERT INTO sections(pid, sid, parent, title, state, ladder, "
               "cycles, misfires, depth, blocked_reason, created, updated) "
               "VALUES(?,?,?,?,'QUEUED',0,0,0,?,NULL,?,?)",
               (pid, sid, parent, title, depth, now, now))
    group_dirs(section_dir(pid, sid))
    atomic_write(os.path.join(section_dir(pid, sid), "task.md"),
                 task_text.strip() + "\n")
    return sid


def handle_draft(con, proj, sect):
    """R2/R3: spawn a drafter (or fixer) at tier 1; route its verdict."""
    pid, sid = proj["pid"], sect["sid"]
    prompt = build_draft_prompt(proj, sect)
    if len(prompt) > prompt_cap():
        upd_section(con, pid, sid, "blocked",
                    "prompt %d chars > cap %d" % (len(prompt), prompt_cap()),
                    state="BLOCKED",
                    blocked_reason="oversize: prompt %d > %d"
                                   % (len(prompt), prompt_cap()))
        return True, ("section %s BLOCKED oversize (prompt %d > cap %d); "
                      "retask or split it" % (sid, len(prompt), prompt_cap()))
    if containment_state() != "PROVISIONED":
        upd_section(con, pid, sid, "blocked", CONTAINMENT_MSG,
                    state="BLOCKED", blocked_reason=CONTAINMENT_MSG)
        return True, "section %s BLOCKED: %s" % (sid, CONTAINMENT_MSG)
    role = "fix" if (sect["cycles"] > 0 or read_feedback(pid, sid)) else "draft"
    if sect["stage"] >= 2 and not read_references(pid, sid):
        _refs, _why = acquire_references(
            pid, sid, read_task(pid, sid), read_feedback(pid, sid))
        transition(con, pid, sid, "acquire",
                   "github: %d reference(s) (%s)" % (len(_refs), _why), None, ())
        prompt = build_draft_prompt(proj, sect)
    tier = stage_tier(sect["stage"])
    try:
        mid, text = spawn(con, pid, sid, role, tier, DRAFT_SYSTEM, prompt)
    except BudgetStop:
        raise
    except Exception as exc:
        return True, _misfire(con, pid, sect, "call failed: %s" % exc)
    word, rest, err = parse_verdict(text, {"DONE", "CANT", "SPLIT"})
    # Missing verdict is not fatal for a drafter: files-present means DONE
    # (models reliably emit code, less reliably the terminal line). But CANT
    # and SPLIT must be stated explicitly -- they are not inferable from
    # output, so a missing verdict never silently becomes a refusal or split.
    if err:
        word, rest = "DONE", ""  # provisional; requires files to stand
    else:
        set_verdict(con, pid, mid, word)
    if word == "CANT":
        # F10 (PO ruling 2026-08-25). A CANT is evidence about THIS STAGE, not
        # about the task. Observed live: the free tier declared a solvable
        # recursion exercise impossible, and the section blocked with a funded
        # cheap stage never asked -- the same category error the misfire ruling
        # fixed for reviewers. The attempt that produced the CANT is charged,
        # that stage ends immediately (a worker that has declared impossibility
        # will not discover possibility from the same prompt), and the same
        # artifact advances to the next configured stage with cycles intact.
        # The reason travels as an explicitly UNVERIFIED claim (see
        # build_draft_prompt), never as a premise.
        why = rest[:400] or "(no reason)"
        st, att = sect["stage"], sect["stage_attempts"] + 1
        cant_cols = {"cant_stage": st, "cant_reason": why[:400],
                     "cant_attempt": att, "cant_claim_status": "unverified"}
        if st < last_stage():
            upd_section(con, pid, sid, "cant_escalate",
                        "CANT at %s (attempt %d): %s -- advancing to %s"
                        % (STAGE_NAMES[st], att, why[:200], STAGE_NAMES[st + 1]),
                        state="QUEUED", stage=st + 1, stage_attempts=0,
                        ladder=0, misfires=0, blocked_reason=None, **cant_cols)
            return True, ("section %s CANT at stage %s (%s); stage ended, "
                          "advanced to %s -- claim unverified"
                          % (sid, STAGE_NAMES[st], why[:90], STAGE_NAMES[st + 1]))
        return True, _factory_exhausted(
            con, pid, sect,
            "CANT at final stage %s: %s" % (STAGE_NAMES[st], why[:200]),
            _kind="cant", stage=st, stage_attempts=att, **cant_cols)
    if word == "SPLIT":
        if sect["depth"] + 1 > depth_cap():
            upd_section(con, pid, sid, "blocked",
                        "split refused at depth %d (cap %d)"
                        % (sect["depth"], depth_cap()),
                        state="BLOCKED",
                        blocked_reason="split-depth: cap %d" % depth_cap())
            return True, ("section %s BLOCKED: split depth cap %d"
                          % (sid, depth_cap()))
        subs = parse_subtasks(text)
        if not subs or len(subs) > split_cap():
            set_verdict(con, pid, mid, "MALFORMED")
            return True, _misfire(con, pid, sect,
                                  "SPLIT with %d subtasks (need 1..%d)"
                                  % (len(subs), split_cap()))
        kids = []
        for sub in subs:
            kids.append(create_section(con, pid, sid, sect["depth"] + 1, sub,
                                       "split_child",
                                       "from %s by %s" % (sid, mid)))
        upd_section(con, pid, sid, "split",
                    "%s -> %s" % (mid, ",".join(kids)),
                    state="SPLIT", misfires=0)
        return True, "section %s SPLIT into %s" % (sid, ", ".join(kids))
    # DONE (explicit, or implicit from files-present with no verdict line)
    try:
        files = parse_files(text)
    except ValueError as exc:
        set_verdict(con, pid, mid, "MALFORMED")
        return True, _misfire(con, pid, sect, str(exc))
    if not files:
        set_verdict(con, pid, mid, "MALFORMED")
        return True, _misfire(
            con, pid, sect,
            "no file blocks" if not err
            else "no verdict and no file blocks")
    if err:
        set_verdict(con, pid, mid, "DONE(implicit)")
    write_section_files(pid, sid, files)
    upd_section(con, pid, sid, "drafted",
                "%s (%s) wrote %d file(s)" % (mid, role, len(files)),
                state="TESTING", ladder=0, misfires=0)
    return True, ("section %s drafted by %s (%d files) -> TESTING"
                  % (sid, mid, len(files)))


def handle_testing(con, proj, sect):
    """The jailed run between draft and the ladder (no model call)."""
    pid, sid = proj["pid"], sect["sid"]
    ok, report = run_section_tests(pid, sid)
    if ok:
        upd_section(con, pid, sid, "tests_pass", report[:400],
                    state="REVIEW1", ladder=1, misfires=0)
        return True, "section %s tests passed -> tier 1 review" % sid
    return True, _fail_cycle(con, pid, sect, "tests_fail",
                             "jailed test run FAIL", report)


def _review_misfire(con, pid, sect, n, reason):
    """A REVIEWER failed -- not the artifact. (PO ruling 2026-08-24.)

    A malformed verdict, a timeout, or a provider error is evidence about the
    REVIEW MECHANISM, not about the code under review. Treating it as a FAIL
    was a category error: it sent a test-passing artifact back for repair and
    restarted the ladder, letting the weakest reviewer veto work it could not
    evaluate. Observed live -- phi3:mini returned an unparseable verdict on a
    2KB artifact that an external grader scored 6.0/6.

    So: escalate to the NEXT reviewer, leaving the artifact, its cycles and
    its stage untouched. If every tier misfires, the finding is that review
    is broken, not that the code is: BLOCKED naming review infrastructure,
    artifact preserved as test-passing."""
    sid = sect["sid"]
    if n < 3:
        upd_section(con, pid, sid, "review_misfire",
                    "tier %d misfired (%s); escalating to tier %d"
                    % (n, reason[:200], n + 1),
                    state="REVIEW%d" % (n + 1), ladder=n + 1, misfires=0)
        return ("section %s tier-%d reviewer MISFIRED (%s); escalated to "
                "tier %d -- artifact untouched" % (sid, n, reason[:100], n + 1))
    upd_section(con, pid, sid, "review_infra_blocked",
                "all reviewer tiers misfired; last: %s" % reason[:200],
                state="BLOCKED", ladder=3, misfires=0,
                blocked_reason="review-infrastructure: all reviewer tiers "
                               "misfired (last: %s)" % reason[:160])
    return ("section %s BLOCKED: review infrastructure failed at every tier "
            "(artifact preserved, tests passing)" % sid)


def handle_review(con, proj, sect):
    """R4: the ladder. PASS climbs; after tier 3 the section awaits Treebeard.
    FAIL requeues a fix and the ladder restarts from tier 1. MISFIRE escalates
    to the next reviewer without touching the artifact (2026-08-24)."""
    pid, sid = proj["pid"], sect["sid"]
    n = REVIEW_STATES[sect["state"]]
    prompt = build_review_prompt(proj, sect, n)
    if len(prompt) > prompt_cap():
        # Artifact-caused and deterministic: a property of the CODE makes it
        # impossible to review under the contract. Distinct from a misfire
        # (reviewer failed) and from a FAIL (reviewer judged it bad) -- no
        # other reviewer could do better, so escalation is pointless.
        upd_section(con, pid, sid, "unreviewable",
                    "review prompt %d > cap %d" % (len(prompt), prompt_cap()),
                    state="UNREVIEWABLE",
                    blocked_reason="contract violation: review prompt %d > "
                                   "cap %d" % (len(prompt), prompt_cap()))
        return True, ("section %s UNREVIEWABLE: prompt %d > cap %d "
                      "(artifact violates the review contract)"
                      % (sid, len(prompt), prompt_cap()))
    tier = tiers()[n - 1]
    try:
        mid, text = spawn(con, pid, sid, "review-t%d" % n, tier,
                          REVIEW_SYSTEM, prompt)
    except BudgetStop:
        raise
    except Exception as exc:
        return True, _review_misfire(con, pid, sect, n,
                                     "call failed: %s" % exc)
    word, rest, err = parse_verdict(text, {"PASS", "FAIL"})
    if err:
        set_verdict(con, pid, mid, "MALFORMED")
        return True, _review_misfire(con, pid, sect, n, err)
    set_verdict(con, pid, mid, word)
    if word == "FAIL":
        return True, _fail_cycle(con, pid, sect, "review_fail",
                                 "tier-%d review %s FAIL" % (n, mid), text)
    if n < 3:
        upd_section(con, pid, sid, "review_pass",
                    "tier %d PASS by %s" % (n, mid),
                    state="REVIEW%d" % (n + 1), ladder=n + 1, misfires=0)
        return True, ("section %s passed tier %d -> tier %d review"
                      % (sid, n, n + 1))
    upd_section(con, pid, sid, "ladder_complete",
                "tier 3 PASS by %s; ready for treebeard" % mid,
                state="AWAITING_TREEBEARD", ladder=3, misfires=0)
    return True, "section %s passed the ladder -> AWAITING_TREEBEARD" % sid


def close_finished_parents(con, pid):
    """SPLIT parents whose children are all DONE become DONE (bubbles up)."""
    msgs = []
    while True:
        row = con.execute(
            "SELECT sid FROM sections s WHERE pid=? AND state='SPLIT' AND "
            "NOT EXISTS (SELECT 1 FROM sections c WHERE c.pid=s.pid AND "
            "c.parent=s.sid AND c.state<>'DONE') ORDER BY sid LIMIT 1",
            (pid,)).fetchone()
        if not row:
            break
        upd_section(con, pid, row["sid"], "children_done",
                    "all children DONE", state="DONE")
        msgs.append("section %s DONE (all children done)" % row["sid"])
    return msgs


def _pick(con, pid, states):
    q = ",".join("?" for _ in states)
    return con.execute(
        "SELECT * FROM sections WHERE pid=? AND state IN (%s) "
        "ORDER BY sid LIMIT 1" % q, (pid,) + tuple(states)).fetchone()


def step_once(con, pid):
    """At most ONE model call (or one jailed test run) per step.
    Priority: finish what is started -- TESTING, then the ladder, then
    new drafts. Returns (progress, message)."""
    proj = get_project(con, pid)
    if proj is None:
        _die("no such project: %s" % pid)
    if proj["state"] in ("CLOSED", "CLOSED_BLOCKED"):
        return False, "project %s is %s" % (pid, proj["state"])
    notes = close_finished_parents(con, pid)
    root_row = get_section(con, pid, "s01")
    if root_row and root_row["state"] == "DONE" and proj["state"] == "ACTIVE":
        transition(con, pid, pid, "master_done",
                   "root section DONE; retro pending",
                   "UPDATE projects SET state='NEEDS_RETRO', updated=? "
                   "WHERE pid=?", (utcnow(), pid))
        write_card(con)
        return True, "; ".join(
            notes + ["master task DONE -> run: meeseeks retro %s" % pid])
    if proj["state"] == "NEEDS_RETRO":
        write_card(con)
        return False, ("master task done; awaiting retro "
                       "(meeseeks retro %s)" % pid)
    try:
        sect = _pick(con, pid, ("TESTING",))
        if sect:
            prog, msg = handle_testing(con, proj, sect)
            write_card(con)
            return prog, "; ".join(notes + [msg])
        sect = _pick(con, pid, ("REVIEW1", "REVIEW2", "REVIEW3"))
        if sect:
            prog, msg = handle_review(con, proj, sect)
            write_card(con)
            return prog, "; ".join(notes + [msg])
        sect = _pick(con, pid, ("QUEUED",))
        if sect:
            prog, msg = handle_draft(con, proj, sect)
            write_card(con)
            return prog, "; ".join(notes + [msg])
    except BudgetStop as exc:
        transition(con, pid, pid, "budget_refused", str(exc))
        write_card(con)
        return False, str(exc)
    write_card(con)
    waiting = [r["sid"] for r in con.execute(
        "SELECT sid FROM sections WHERE pid=? AND state='AWAITING_TREEBEARD' "
        "ORDER BY sid", (pid,))]
    blocked = [(r["sid"], r["blocked_reason"]) for r in con.execute(
        "SELECT sid, blocked_reason FROM sections WHERE pid=? AND "
        "state='BLOCKED' ORDER BY sid", (pid,))]
    bits = list(notes)
    if waiting:
        bits.append("awaiting treebeard review: %s" % ", ".join(waiting))
    if blocked:
        bits.append("blocked: %s" % "; ".join("%s (%s)" % b for b in blocked))
    if not bits:
        bits.append("nothing actionable")
    return bool(notes), "; ".join(bits)


# --------------------------------------------------------------------------
# Status card (R9): rewritten atomically on every transition; the Sun
# integration reads this file (one-line patch, later, same as Anvil's).
# --------------------------------------------------------------------------


def write_card(con):
    lines = ["MEESEEKS BOX -- %s" % utcnow()]
    projs = con.execute("SELECT * FROM projects ORDER BY pid").fetchall()
    total_wait = con.execute(
        "SELECT COUNT(*) FROM sections WHERE state='AWAITING_TREEBEARD'"
    ).fetchone()[0]
    total_block = con.execute(
        "SELECT COUNT(*) FROM sections WHERE state='BLOCKED'").fetchone()[0]
    lines.append("awaiting treebeard: %d | blocked: %d | projects: %d"
                 % (total_wait, total_block, len(projs)))
    for p in projs:
        done = con.execute(
            "SELECT COUNT(*) FROM sections WHERE pid=? AND state='DONE'",
            (p["pid"],)).fetchone()[0]
        tot = con.execute("SELECT COUNT(*) FROM sections WHERE pid=?",
                          (p["pid"],)).fetchone()[0]
        lines.append("%s [%s] sections %d/%d done, calls %d/%d"
                     % (p["pid"], p["state"], done, tot,
                        p["calls_used"], p["call_cap"]))
        for r in con.execute(
                "SELECT sid, state, blocked_reason FROM sections WHERE pid=? "
                "AND state<>'DONE' ORDER BY sid", (p["pid"],)):
            extra = " (%s)" % r["blocked_reason"] if r["blocked_reason"] else ""
            lines.append("  %s %s%s" % (r["sid"], r["state"], extra))
    if not projs:
        lines.append("(no projects)")
    atomic_write(status_path(), "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _require_gate():
    u = getpass.getuser()
    if u != gate_user():
        _die("gate command: must run as user %r (you are %r)"
             % (gate_user(), u))


def cmd_init(args):
    for d in (root(), projects_dir(), staging_dir(), directives_dir(),
              os.path.dirname(status_path())):
        group_dirs(d)
    if not os.path.exists(directive_path()):
        atomic_write(directive_path(), DEFAULT_DIRECTIVE)
    con = connect()
    transition(con, "-", "-", "init",
               "meeseeks box initialized at %s" % root())
    write_card(con)
    print("meeseeks box initialized at %s" % root())
    print("directive: %s (treebeard-owned; edits take effect next spawn)"
          % directive_path())
    print("status card: %s" % status_path())
    print("NOTE: ownership (R3-style) is set at deploy time -- treebeard "
          "owns the tree; notepads")
    print("group-readable (2775/664) so agents can see them (R6); "
          "chown/chmod in the walk.")
    print("NOTE: verify MEESEEKS_TIERS against router.py before the first "
          "live spawn.")


def cmd_new(args):
    name = args.name
    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,40}$", name):
        _die("project name must match [a-z0-9][a-z0-9_-]{1,40}")
    task = " ".join(args.task).strip() if args.task else ""
    if getattr(args, "task_file", None):
        with open(args.task_file, "r", encoding="ascii",
                  errors="replace") as fh:
            task = fh.read().strip()
    if not task:
        _die("empty master task")
    con = connect()
    if get_project(con, name):
        _die("project %r already exists" % name)
    now = utcnow()
    by = (getattr(args, "by", "") or "").strip()
    if not by:
        _die("--by must name the commissioning agent")
    transition(con, name, name, "project_new",
               "%s (commissioned by %s)" % (task[:360], by),
               "INSERT INTO projects(pid, name, master, state, calls_used, "
               "call_cap, created, updated, commissioned_by) "
               "VALUES(?,?,?,?,0,?,?,?,?)",
               (name, name, task, "ACTIVE", call_cap_default(), now, now, by))
    group_dirs(proj_dir(name))
    group_dirs(notepad_dir(name))
    group_dirs(transcripts_dir(name))
    create_section(con, name, None, 0, task, "root_section", "master task")
    write_card(con)
    print("project %s created; root section s01 QUEUED; cap %d calls"
          % (name, call_cap_default()))
    print("next: meeseeks step %s" % name)


def cmd_step(args):
    con = connect()
    _prog, msg = step_once(con, args.project)
    print(msg)


def cmd_run(args):
    con = connect()
    cap = args.max_steps or run_max_default()
    for i in range(cap):
        prog, msg = step_once(con, args.project)
        print("[%d] %s" % (i + 1, msg))
        if not prog:
            return
    print("(step cap %d reached; run again to continue)" % cap)


def cmd_status(args):
    con = connect()
    write_card(con)
    with open(status_path(), "r", encoding="ascii", errors="replace") as fh:
        sys.stdout.write(fh.read())
    if getattr(args, "project", None):
        pid = args.project
        proj = get_project(con, pid)
        if not proj:
            _die("no such project: %s" % pid)
        print("--- %s detail ---" % pid)
        for r in con.execute("SELECT * FROM sections WHERE pid=? ORDER BY sid",
                             (pid,)):
            print("%s [%s] ladder=%d cycles=%d misfires=%d depth=%d "
                  "parent=%s :: %s"
                  % (r["sid"], r["state"], r["ladder"], r["cycles"],
                     r["misfires"], r["depth"], r["parent"] or "-",
                     r["title"]))


def cmd_digest(args):
    con = connect()
    write_card(con)
    with open(status_path(), "r", encoding="ascii", errors="replace") as fh:
        sys.stdout.write(fh.read())


def cmd_review(args):
    """R3: the Treebeard gate. Approve deposits the section into Anvil's
    treebeard dropzone (the Anvil human lock remains the toolkit gate);
    reject sends it back with feedback (a new Meeseeks on the next step)."""
    _require_gate()
    con = connect()
    pid, sid = args.project, args.section
    sect = get_section(con, pid, sid)
    if not sect:
        _die("no such section: %s/%s" % (pid, sid))
    if args.approve:
        if sect["state"] != "AWAITING_TREEBEARD":
            _die("section %s is %s, not AWAITING_TREEBEARD"
                 % (sid, sect["state"]))
        files = section_files(pid, sid)
        if not files:
            _die("section %s has no files to deposit" % sid)
        dest = os.path.join(anvil_dropzone(), "%s_%s" % (pid, sid))
        if os.path.exists(dest):
            _die("deposit target already exists: %s" % dest)
        os.makedirs(dest)
        for name, body in files.items():
            fdest = os.path.join(dest, name)
            os.makedirs(os.path.dirname(fdest), exist_ok=True)
            with open(fdest, "w", encoding="ascii", errors="replace") as fh:
                fh.write(body)
        upd_section(con, pid, sid, "approved",
                    "by %s: %s; deposited %d file(s) -> %s"
                    % (args.by, args.note or "-", len(files), dest),
                    state="DONE", blocked_reason=None)
        write_card(con)
        print("section %s DONE; %d file(s) deposited to %s"
              % (sid, len(files), dest))
        print("next: register it -- run anvil intake as treebeard")
    elif args.reject:
        msg = _fail_cycle(con, pid, sect, "rejected",
                          "treebeard reject by %s" % args.by, args.reject,
                          count=False)
        write_card(con)
        print(msg)
    else:
        _die("pass --approve or --reject REASON")


def cmd_retask(args):
    """R3: Treebeard creates a Meeseeks -- replace the task, requeue."""
    _require_gate()
    con = connect()
    pid, sid = args.project, args.section
    sect = get_section(con, pid, sid)
    if not sect:
        _die("no such section: %s/%s" % (pid, sid))
    if sect["state"] in ("DONE", "SPLIT"):
        _die("section %s is %s; retask a leaf that is not done"
             % (sid, sect["state"]))
    task = " ".join(args.task).strip()
    if not task:
        _die("empty task")
    atomic_write(os.path.join(section_dir(pid, sid), "task.md"), task + "\n")
    append_feedback(pid, sid, "retask by %s" % args.by,
                    "task replaced by treebeard")
    upd_section(con, pid, sid, "retasked", "by %s" % args.by,
                state="QUEUED", ladder=0, misfires=0, blocked_reason=None)
    write_card(con)
    print("section %s retasked -> QUEUED" % sid)


def cmd_fund(args):
    _require_gate()
    con = connect()
    proj = get_project(con, args.project)
    if not proj:
        _die("no such project: %s" % args.project)
    transition(con, args.project, args.project, "funded",
               "call cap %d -> %d by %s"
               % (proj["call_cap"], args.calls, args.by),
               "UPDATE projects SET call_cap=?, updated=? WHERE pid=?",
               (args.calls, utcnow(), args.project))
    write_card(con)
    print("project %s call cap: %d" % (args.project, args.calls))


def cmd_retro(args):
    """R8: what worked, what did not, suggested directive changes --
    ADVISORY ONLY. This tool never edits the directive that governs all
    future Meeseeks; that write belongs to Treebeard (R1), by hand."""
    _require_gate()
    con = connect()
    pid = args.project
    proj = get_project(con, pid)
    if not proj:
        _die("no such project: %s" % pid)
    if proj["state"] != "NEEDS_RETRO":
        _die("project %s is %s; retro runs when the master task is DONE "
             "(NEEDS_RETRO)" % (pid, proj["state"]))
    sects = con.execute("SELECT * FROM sections WHERE pid=? ORDER BY sid",
                        (pid,)).fetchall()
    mrows = con.execute(
        "SELECT role, tier, verdict, COUNT(*) c FROM meeseeks WHERE pid=? "
        "GROUP BY role, tier, verdict ORDER BY role, tier",
        (pid,)).fetchall()
    evs = con.execute(
        "SELECT ts, ref, kind, detail FROM events WHERE pid=? "
        "ORDER BY seq DESC LIMIT 80", (pid,)).fetchall()
    parts = ["PROJECT: %s" % pid, "MASTER TASK:", proj["master"].strip(), "",
             "SECTIONS (sid state cycles misfires depth title):"]
    for s in sects:
        parts.append("%s %s cycles=%d misfires=%d depth=%d :: %s"
                     % (s["sid"], s["state"], s["cycles"], s["misfires"],
                        s["depth"], s["title"]))
    parts += ["", "MEESEEKS (role tier verdict count):"]
    for m in mrows:
        parts.append("%s %s %s x%d" % (m["role"], m["tier"], m["verdict"],
                                       m["c"]))
    parts += ["", "CALLS USED: %d/%d" % (proj["calls_used"], proj["call_cap"]),
              "", "EVENT TAIL (newest first):"]
    for e in evs:
        parts.append("%s %s %s :: %s"
                     % (e["ts"], e["ref"], e["kind"], e["detail"][:120]))
    parts += ["", "CURRENT DIRECTIVE:", load_directive().strip(), "",
              "Write three sections, plain text:",
              "WHAT WORKED",
              "WHAT DID NOT",
              "SUGGESTED DIRECTIVE CHANGES (advisory only -- Treebeard edits "
              "the directive file",
              "himself; nothing you write here is applied automatically)."]
    prompt = "\n".join(parts)
    if len(prompt) > prompt_cap():
        prompt = prompt[:prompt_cap()] + "\n(record truncated at prompt cap)"
    try:
        mid, text = spawn(con, pid, None, "retro", retro_tier(),
                          RETRO_SYSTEM, prompt)
    except BudgetStop as exc:
        transition(con, pid, pid, "budget_refused", str(exc))
        _die(str(exc))
    except Exception as exc:
        transition(con, pid, pid, "retro_error",
                   "%s: %s" % (type(exc).__name__, exc))
        _die("retro call failed: %s" % exc)
    set_verdict(con, pid, mid, "RETRO")
    atomic_write(retro_path(pid),
                 "RETROSPECTIVE -- project %s -- %s -- by %s\n\n%s\n"
                 % (pid, utcnow(), mid, text), encoding="utf-8")
    transition(con, pid, pid, "retro",
               "retro written by %s -> %s" % (mid, retro_path(pid)),
               "UPDATE projects SET state='CLOSED', updated=? WHERE pid=?",
               (utcnow(), pid))
    write_card(con)
    print("retro written: %s" % retro_path(pid))
    print("project %s CLOSED. Directive suggestions are advisory; apply by "
          "editing %s" % (pid, directive_path()))


def cmd_close(args):
    """Terminal close (finding #7): a project whose ROOT section is
    genuinely BLOCKED can be closed WITHOUT falsifying history. The
    section stays BLOCKED -- never synthesized DONE, never a passing
    result. Success-path projects are refused; they close via retro."""
    _require_gate()
    reason = (args.reason or "").strip()
    if not reason:
        _die("close requires a non-empty --reason")
    con = connect()
    proj = get_project(con, args.project)
    if not proj:
        _die("no such project: %s" % args.project)
    if proj["state"] != "ACTIVE":
        _die("project %s is %s; close applies only to an ACTIVE project "
             "with a BLOCKED or UNREVIEWABLE root (success path closes "
             "via retro)" % (args.project, proj["state"]))
    root = get_section(con, args.project, "s01")
    # UNREVIEWABLE added 2026-08-24. It is terminal for the same reason
    # BLOCKED is -- no handler advances it and no further work can help --
    # so it needs the same honest exit. Without this the state machine
    # would repeat finding #7: a terminal root with no terminal path,
    # leaving the project ACTIVE forever.
    _TERMINAL_ROOTS = ("BLOCKED", "UNREVIEWABLE")
    if not root or root["state"] not in _TERMINAL_ROOTS:
        _die("root section is %s, not one of %s; refusing terminal close"
             % (root["state"] if root else "missing",
                "/".join(_TERMINAL_ROOTS)))
    transition(con, args.project, "s01", "closed_blocked",
               "by %s: %s" % (args.by, reason[:400]),
               "UPDATE projects SET state='CLOSED_BLOCKED', updated=? "
               "WHERE pid=?", (utcnow(), args.project))
    write_card(con)
    print("project %s CLOSED_BLOCKED; root section preserved BLOCKED"
          % args.project)
    print("reason: %s" % reason[:200])


def cmd_directive(args):
    if getattr(args, "what", "show") == "path":
        print(directive_path())
        return
    with open(directive_path(), "r", encoding="ascii", errors="replace") as fh:
        sys.stdout.write(fh.read())


# --------------------------------------------------------------------------
# Selftest: the full machine in a temp root with a scripted fake router.
# No live model calls, no live paths, no money.
# --------------------------------------------------------------------------


class _FakeRouter(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # (system, user, tier, label)

    def __call__(self, system, user, tier, label):
        self.calls.append((system, user, tier, label))
        if not self.responses:
            raise RuntimeError("fake router: script exhausted")
        item = self.responses.pop(0)
        if callable(item):
            return item(system, user, tier)
        return item, tier, "fake-model"


def _mk_files(files):
    return "".join("```\n# filename: %s\n%s```\n"
                   % (n, b if b.endswith("\n") else b + "\n")
                   for n, b in files)


def _r_done(files):
    return _mk_files(files) + "VERDICT: DONE\n"


def _r_done_noverdict(files):
    """A drafter that supplied files but omitted the verdict line -- the
    real-world Free_provider/local_model_family failure the implicit-DONE path handles."""
    return _mk_files(files)


def _r_pass():
    return "Reviewed: correct, tested, commented.\nVERDICT: PASS\n"


def _r_fail(issues):
    return "Reviewed.\nProblems:\n%s\nVERDICT: FAIL %s\n" % (issues, issues)


def _r_cant(reason):
    return "VERDICT: CANT %s\n" % reason


def _r_split(subs):
    body = "".join("```\n# subtask\n%s\n```\n" % s for s in subs)
    return "Too big for one pass.\n" + body + "VERDICT: SPLIT\n"


_ADDER = "# adds two integers; selftest happy-path module\ndef add(a, b):\n    return a + b\n"
_TEST_ADDER = ("import unittest\n# exercises adder.add\nimport adder\n\n"
               "class T(unittest.TestCase):\n    def test_add(self):\n"
               "        self.assertEqual(adder.add(2, 3), 5)\n\n"
               "if __name__ == '__main__':\n    unittest.main()\n")
_TEST_ADDER_BAD = _TEST_ADDER.replace("5)", "6)")
_ADDER_NOCOMMENT = "def add(a, b):\n    return a + b\n"
_MUL = "# multiplies two integers\ndef mul(a, b):\n    return a * b\n"
_TEST_MUL = ("import unittest\n# exercises mul.mul\nimport mul\n\n"
             "class T(unittest.TestCase):\n    def test(self):\n"
             "        self.assertEqual(mul.mul(2, 3), 6)\n")
_SUBBER = "# subtracts b from a\ndef sub(a, b):\n    return a - b\n"
_TEST_SUBBER = ("import unittest\n# exercises subber.sub\nimport subber\n\n"
                "class T(unittest.TestCase):\n    def test(self):\n"
                "        self.assertEqual(subber.sub(5, 3), 2)\n")


def cmd_selftest(args):
    global _router_call
    results = []

    def ok(name, cond, note=""):
        results.append((name, bool(cond), note))
        print("%-46s %s" % (name, "ok" if cond else "FAIL"))
        if not cond and note:
            print("    detail: %s" % note)

    global _JAIL_IMPL
    # T2 wiring, checked BEFORE the seam is installed: the production jail
    # is tbjail via sudo, there is no local fallback, and odd shapes refuse.
    ok("jail: production runner is tbjail (no seam)", _JAIL_IMPL is None)
    global _CONTAINMENT
    ok("C3: production containment state is computed, not seamed", _CONTAINMENT is None)
    ok("jail: py_compile shape",
       _jail_mode([sys.executable, "-m", "py_compile", "a.py"], "/w")
       == ["py_compile", "a.py"])
    ok("jail: discover shape",
       _jail_mode([sys.executable, "-m", "unittest", "discover", "-s", ".",
                   "-p", "test_*.py", "-v"], "/w") == ["unittest-discover"])
    ok("jail: unittest module shape",
       _jail_mode([sys.executable, "-m", "unittest", "-v", "test_x"], "/w")
       == ["unittest", "test_x"])
    ok("jail: run shape (loop exec)",
       _jail_mode(["/usr/bin/python3", "/w/s/m.py", "x"], "/w")
       == ["run", "s/m.py", "x"])
    ok("jail: escaping path refused",
       _jail_mode(["/usr/bin/python3", "/etc/passwd"], "/w") is None)
    ok("jail: other shapes refused, never run locally",
       _jail_run([sys.executable, "-c", "print(1)"], "/w")[0] == 126)
    saved_env = dict(os.environ)
    saved_router = _router_call
    saved_jail = _JAIL_IMPL
    _JAIL_IMPL = _jail_run_local
    saved_c3 = _CONTAINMENT
    _CONTAINMENT = "PROVISIONED"
    tmp = tempfile.mkdtemp(prefix="meeseeks-selftest-")
    try:
        os.environ["MEESEEKS_ROOT"] = os.path.join(tmp, "data")
        os.environ["MEESEEKS_GATE_USER"] = getpass.getuser()
        os.environ["MEESEEKS_ANVIL_DROPZONE"] = os.path.join(tmp, "dropzone")
        os.environ["MEESEEKS_TIERS"] = "t1,t2,t3"
        os.environ["MEESEEKS_STAGES"] = "s1,s2"
        os.environ["MEESEEKS_CALL_CAP"] = "50"
        os.environ.pop("MEESEEKS_PROMPT_CAP", None)
        os.environ.pop("MEESEEKS_CALL_TIMEOUT", None)
        os.makedirs(os.environ["MEESEEKS_ANVIL_DROPZONE"], exist_ok=True)

        # -- init ----------------------------------------------------------
        cmd_init(argparse.Namespace())
        ok("init creates db", os.path.exists(db_path()))
        ok("init writes directive",
           "broken into smaller chunks" in open(directive_path()).read())
        ok("init writes card", os.path.exists(status_path()))

        # -- parser units ---------------------------------------------------
        w, _r, e = parse_verdict("blah\nVERDICT: DONE\n", {"DONE"})
        ok("verdict parses", w == "DONE" and e is None)
        w, _r, e = parse_verdict("VERDICT: PASS\n", {"DONE", "CANT", "SPLIT"})
        ok("illegal verdict rejected", e is not None)
        w, _r, e = parse_verdict("VERDICT: FAIL x\nmore\nVERDICT: PASS\n",
                                 {"PASS", "FAIL"})
        ok("last verdict wins", w == "PASS")
        fs = parse_files("```\n# filename: a.py\n# c\nx = 1\n```\n")
        ok("fenced file parses",
           fs and fs[0][0] == "a.py" and fs[0][1] == "# c\nx = 1\n")
        fs = parse_files("```python\n# b.py\nx = 2\n```\n")
        ok("language hint + bare-# marker parses",
           fs and fs[0][0] == "b.py" and fs[0][1] == "x = 2\n")
        try:
            parse_files("```\n# filename: ../evil.py\nx = 1\n```\n")
            escaped = False
        except ValueError:
            escaped = True
        ok("path escape rejected", escaped)
        try:
            parse_files("```\n# filename: u.py\n" + chr(233) + "\n```\n")
            nonascii = False
        except ValueError:
            nonascii = True
        ok("non-ascii content rejected", nonascii)
        try:
            parse_files("```\n# filename: p.py\n<code lines as above>\n```\n")
            placeheld = False
        except ValueError:
            placeheld = True
        ok("placeholder body rejected", placeheld)
        # A fenced preview with no filename marker is ignored, not a file.
        fs = parse_files("```python\nx = 1\n```\n"
                         "```\n# filename: real.py\ny = 2\n```\n")
        ok("unmarked preview ignored, real file kept",
           len(fs) == 1 and fs[0][0] == "real.py")
        subs = parse_subtasks("```\n# subtask\nA\n```\n```\n# subtask\nB\n```\n")
        ok("fenced subtasks parse", subs == ["A", "B"])

        # -- C3 / D9: the non-executing provisioning-state check -------------
        import stat as _st

        class _S(object):
            def __init__(self, mode, uid):
                self.st_mode, self.st_uid = mode, uid

        def _fixture(**over):
            files = {}
            pins = []
            for n in ("jail_prep.py", "jail_launch.py", "board_writer.py"):
                p = CTL_DIR + "/" + n
                files[p] = (_S(_st.S_IFREG | 0o644, 0), ("code %s" % n).encode())
                pins.append("sha256 %s %s" % (p, hashlib.sha256(files[p][1]).hexdigest()))
            pins.append("recorded /etc/sudoers.d/treebeard-containment " + "a" * 64)
            files[CTL_DIR + "/T2_MANIFEST"] = (_S(_st.S_IFREG | 0o644, 0), "\n".join(pins).encode())
            files[CTL_DIR + "/PROVISIONED"] = (_S(_st.S_IFREG | 0o644, 0),
                ("receipt r.json %s\nrecorded /etc/sudoers.d/treebeard-containment %s\n" % ("b" * 64, "c" * 64)).encode())
            files[JAIL_SCRATCH] = (_S(_st.S_IFDIR | 0o700, 975), b"")
            files[JAIL_SUDO] = (_S(_st.S_IFREG | 0o4755, 0), b"")
            users = {"tbjail": 975, "tbctl": 976}
            for k, v in over.items():
                if k == "users":
                    users = v
                elif v == "OWNER1000":
                    files[k] = (_S(files[k][0].st_mode, 1000), files[k][1])
                elif v is None:
                    files.pop(k, None)
                else:
                    files[k] = v

            class _Pw(object):
                def __init__(self, uid):
                    self.pw_uid = uid

            def pw(u):
                return _Pw(users[u])

            def lst(p):
                if p not in files:
                    raise FileNotFoundError(p)
                return files[p][0]

            def rd(p):
                if p not in files:
                    raise FileNotFoundError(p)
                return files[p][1]
            return pw, lst, rd, lst     # the fixture's stat (no links in the fixture)

        _spawned = []
        _real_run, _real_popen = subprocess.run, subprocess.Popen
        subprocess.run = lambda *a, **k: _spawned.append(a) or _real_run(*a, **k)
        subprocess.Popen = lambda *a, **k: _spawned.append(a) or _real_popen(*a, **k)
        try:
            JP = CTL_DIR + "/jail_prep.py"
            cases = [
                ("all present", {}, "PROVISIONED"),
                ("(a) tbjail absent", {"users": {"tbctl": 976}}, "NOT PROVISIONED"),
                ("(a) tbctl absent", {"users": {"tbjail": 975}}, "NOT PROVISIONED"),
                ("(b) T2_MANIFEST absent", {CTL_DIR + "/T2_MANIFEST": None}, "NOT PROVISIONED"),
                ("(b) T2_MANIFEST not root-owned", {CTL_DIR + "/T2_MANIFEST": "OWNER1000"}, "NOT PROVISIONED"),
                ("(b) helper absent", {JP: None}, "NOT PROVISIONED"),
                ("(b) helper hash differs", {JP: (_S(_st.S_IFREG | 0o644, 0), b"other")}, "NOT PROVISIONED"),
                ("(b) launcher group-writable", {CTL_DIR + "/jail_launch.py": (_S(_st.S_IFREG | 0o664, 0), b"code jail_launch.py")}, "NOT PROVISIONED"),
                ("(b) writer not root-owned", {CTL_DIR + "/board_writer.py": (_S(_st.S_IFREG | 0o644, 1000), b"code board_writer.py")}, "NOT PROVISIONED"),
                ("(b) helper a symlink", {JP: (_S(_st.S_IFLNK | 0o777, 0), b"code jail_prep.py")}, "NOT PROVISIONED"),
                ("(c) scratch absent", {JAIL_SCRATCH: None}, "NOT PROVISIONED"),
                ("(c) scratch owner", {JAIL_SCRATCH: (_S(_st.S_IFDIR | 0o700, 1000), b"")}, "NOT PROVISIONED"),
                ("(c) scratch mode 750", {JAIL_SCRATCH: (_S(_st.S_IFDIR | 0o750, 975), b"")}, "NOT PROVISIONED"),
                ("(e) sudo binary absent", {JAIL_SUDO: None}, "NOT PROVISIONED"),
                ("(e) sudo binary not setuid", {JAIL_SUDO: (_S(_st.S_IFREG | 0o755, 0), b"")}, "NOT PROVISIONED"),
                ("(e) sudo binary not root-owned", {JAIL_SUDO: (_S(_st.S_IFREG | 0o4755, 1000), b"")}, "NOT PROVISIONED"),
                ("(e) sudo binary group-writable", {JAIL_SUDO: (_S(_st.S_IFREG | 0o4775, 0), b"")}, "NOT PROVISIONED"),
                ("(e) sudo binary a directory", {JAIL_SUDO: (_S(_st.S_IFDIR | 0o4755, 0), b"")}, "NOT PROVISIONED"),
                ("(d) no record", {CTL_DIR + "/PROVISIONED": None}, "NOT PROVISIONED"),
                ("(d) record not root-owned", {CTL_DIR + "/PROVISIONED": (_S(_st.S_IFREG | 0o644, 1000), b"recorded /etc/sudoers.d/treebeard-containment " + b"c" * 64)}, "NOT PROVISIONED"),
                ("(d) record names another file", {CTL_DIR + "/PROVISIONED": (_S(_st.S_IFREG | 0o644, 0), b"recorded /etc/sudoers.d/other " + b"c" * 64)}, "NOT PROVISIONED"),
            ]
            for name, over, want in cases:
                got = _containment_check(*_fixture(**over))
                ok("C3 check: %s -> %s" % (name, want), got[0] == want, got)
            # (d) the reference box: no PROVISIONED, the T2 deploy record line
            pw, lst, rd, _stf = _fixture(**{CTL_DIR + "/PROVISIONED": None})
            _man = rd(CTL_DIR + "/T2_MANIFEST").replace(b"treebeard-containment", b"treebeard-t2")
            _files_t2 = {CTL_DIR + "/T2_MANIFEST": (_S(_st.S_IFREG | 0o644, 0), _man)}
            for p in ("jail_prep.py", "jail_launch.py", "board_writer.py"):
                _files_t2[CTL_DIR + "/" + p] = (lst(CTL_DIR + "/" + p), rd(CTL_DIR + "/" + p))
            _files_t2[JAIL_SCRATCH] = (lst(JAIL_SCRATCH), b"")
            _files_t2[JAIL_SUDO] = (lst(JAIL_SUDO), b"")
            _l2 = lambda p: _files_t2[p][0] if p in _files_t2 else (_ for _ in ()).throw(FileNotFoundError(p))
            got = _containment_check(pw, _l2,
                                     lambda p: _files_t2[p][1] if p in _files_t2 else (_ for _ in ()).throw(FileNotFoundError(p)), _l2)
            ok("C3 check: (d) T2 deploy record (treebeard-t2) -> PROVISIONED", got[0] == "PROVISIONED", got)
            ok("C3 check: executes nothing (no subprocess spawned)", _spawned == [], _spawned)
            ok("C3 message is exactly 'execution unavailable: not provisioned'",
               CONTAINMENT_MSG == "execution unavailable" + ": not provisioned", CONTAINMENT_MSG)

            # the hooks, NOT PROVISIONED: exact message, no spawn, no model call
            _CONTAINMENT = "NOT PROVISIONED"
            saved_seam = _JAIL_IMPL
            _JAIL_IMPL = None
            _wd = tempfile.mkdtemp(prefix="c3-")
            open(os.path.join(_wd, "a.py"), "w").write("x = 1\n")
            del _spawned[:]
            r = _jail_run([sys.executable, "-m", "py_compile", "a.py"], _wd)
            ok("C3 _jail_run NOT PROVISIONED -> (126, '', exact message)",
               r == (126, "", CONTAINMENT_MSG), r)
            ok("C3 _jail_run NOT PROVISIONED spawns nothing", _spawned == [], _spawned)
            # the belt: PROVISIONED, but the sudo binary is missing (K1)
            _CONTAINMENT = "PROVISIONED"
            global JAIL_ARGV
            saved_argv = JAIL_ARGV
            JAIL_ARGV = [os.path.join(_wd, "no-such-sudo")] + saved_argv[1:]
            try:
                r = _jail_run([sys.executable, "-m", "py_compile", "a.py"], _wd)
                exc = None
            except Exception as e:      # K1 would land here
                r, exc = None, e
            JAIL_ARGV = saved_argv
            _JAIL_IMPL = saved_seam
            ok("C3 belt: missing sudo binary -> exact message, no exception",
               exc is None and r == (126, "", CONTAINMENT_MSG), (r, exc))
            shutil.rmtree(_wd, ignore_errors=True)
            # handle_draft: BLOCKED with the exact reason and 0 model calls
            _CONTAINMENT = "NOT PROVISIONED"
            fake_c3 = _FakeRouter([_r_done([("x.py", "x = 1\n")])])
            _router_call = fake_c3
            con_c3 = connect()
            cmd_new(argparse.Namespace(name="c3np", by="treebeard",
                                       task=["Build", "x."], task_file=None))
            _p, msg = step_once(con_c3, "c3np")
            s = get_section(con_c3, "c3np", "s01")
            ok("C3 draft NOT PROVISIONED -> BLOCKED",
               s["state"] == "BLOCKED", (s["state"], msg))
            ok("C3 blocked_reason is exactly the message",
               s["blocked_reason"] == CONTAINMENT_MSG, s["blocked_reason"])
            ok("C3 draft NOT PROVISIONED spends 0 model calls",
               fake_c3.calls == [], len(fake_c3.calls))
            con_c3.close()
        finally:
            subprocess.run, subprocess.Popen = _real_run, _real_popen
            _CONTAINMENT = "PROVISIONED"

        # -- happy path: draft -> jail -> ladder -> approve -> retro --------
        fake = _FakeRouter([
            _r_done([("adder.py", _ADDER), ("test_adder.py", _TEST_ADDER)]),
            _r_pass(), _r_pass(), _r_pass(),
        ])
        _router_call = fake
        con = connect()
        cmd_new(argparse.Namespace(name="happy", by="treebeard",
                                   task=["Build", "an", "adder", "module."],
                                   task_file=None))
        s = get_section(con, "happy", "s01")
        ok("new project root QUEUED", s and s["state"] == "QUEUED")
        ok("task.md written", "adder" in read_task("happy", "s01"))
        _p, msg = step_once(con, "happy")
        ok("draft -> TESTING",
           get_section(con, "happy", "s01")["state"] == "TESTING", msg)
        ok("files landed in notepad",
           os.path.exists(os.path.join(section_dir("happy", "s01"),
                                       "adder.py")))
        _p, msg = step_once(con, "happy")
        ok("jail pass -> REVIEW1",
           get_section(con, "happy", "s01")["state"] == "REVIEW1", msg)
        rdir = os.path.join(proj_dir("happy"), "receipts")
        rfs = sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []
        rtxt = (open(os.path.join(rdir, rfs[-1]), encoding="utf-8")
                .read() if rfs else "")
        ok("durable receipt written with full output",
           rfs and "== unittest discover rc=0 ==" in rtxt
           and "RESULT: PASS" in rtxt)
        drow = con.execute("SELECT detail FROM events WHERE "
                           "pid='happy' AND kind='tests_pass' "
                           "ORDER BY seq DESC LIMIT 1").fetchone()
        ok("ledger carries receipt pointer",
           drow and drow["detail"].startswith("receipt: "))
        step_once(con, "happy")
        step_once(con, "happy")
        step_once(con, "happy")
        ok("ladder complete -> AWAITING",
           get_section(con, "happy", "s01")["state"] == "AWAITING_TREEBEARD")
        # Factory refactor 2026-08-24: draft/repair draws from the STAGE
        # ladder (MEESEEKS_STAGES), review from the TIER ladder
        # (MEESEEKS_TIERS). Distinct fake names prove neither leaks into
        # the other -- draft on s1, the 3-tier review still t1..t3.
        ok("draft on stage ladder, review on tier ladder",
           [c[2] for c in fake.calls] == ["s1", "t1", "t2", "t3"],
           str([c[2] for c in fake.calls]))
        # FACTORY LADDER (2026-08-24): the property the refactor exists
        # for -- a REPAIR call must climb the stage ladder. The happy path
        # above never fails, so it cannot show this. Drive a section whose
        # jail run fails and capture the tiers its draft+fix calls used.
        _pre = len(fake.calls)
        con2 = connect()
        transition(con2, "esc", "esc", "project_new", "t",
                   "INSERT INTO projects(pid,name,master,state,calls_used,"
                   "call_cap,created,updated) VALUES(?,?,?,?,0,?,?,?)",
                   ("esc", "esc", "m", "ACTIVE", 80, utcnow(), utcnow()))
        create_section(con2, "esc", None, 0, "task", "root_section", "d")
        s_esc = get_section(con2, "esc", "s01")
        _fail_cycle(con2, "esc", s_esc, "tests_fail", "FAIL", "same failure")
        s_esc = get_section(con2, "esc", "s01")
        ok("repair after 1st fail still stage 0",
           s_esc["stage"] == 0, str(s_esc["stage"]))
        ok("stage 0 resolves to first stage tier",
           stage_tier(s_esc["stage"]) == stages()[0], stage_tier(s_esc["stage"]))
        _fail_cycle(con2, "esc", s_esc, "tests_fail", "FAIL", "same failure")
        s_esc = get_section(con2, "esc", "s01")
        ok("plateau escalates repair to stage 1",
           s_esc["stage"] == 1, str(s_esc["stage"]))
        ok("stage 1 resolves to second stage tier",
           stage_tier(s_esc["stage"]) == stages()[1], stage_tier(s_esc["stage"]))
        ok("escalated repair tier differs from draft tier",
           stage_tier(0) != stage_tier(1))

        # COMMISSIONER RECORDING (2026-08-24, R3 recording half): every
        # project records the governed agent that commissioned it, and the
        # work order names them. Enforcement (only agents may commission)
        # is deferred until an agent-invocable action surface exists.
        _pr = get_project(con, "happy")
        ok("commissioner persisted", _pr["commissioned_by"] == "treebeard",
           _pr["commissioned_by"])
        ok("work order names commissioner",
           "COMMISSIONED BY: treebeard" in
           build_draft_prompt(_pr, get_section(con, "happy", "s01")))
        _ev = con.execute("SELECT detail FROM events WHERE pid='happy' AND "
                          "kind='project_new'").fetchone()
        ok("commission audited", _ev and "commissioned by treebeard" in _ev["detail"],
           _ev["detail"] if _ev else "no event")

        # DETERMINISTIC MISFIRE (2026-08-24): a cause retry cannot fix must
        # block on the FIRST occurrence with the real reason, not burn a
        # second paid call and report a misfire tally. Both strings below
        # were observed live during the K1 benchmark.
        ok("missing key classed deterministic",
           _is_deterministic("router not-ok: free unavailable: "
                             "TREEBEARD_FREE_API_KEY not set"))
        ok("budget refusal classed deterministic",
           _is_deterministic("router not-ok: cheap budget-check failed "
                             "(fail-closed)"))
        ok("garbage output NOT deterministic",
           not _is_deterministic("garbage with no verdict at all"))
        ok("timeout NOT deterministic",
           not _is_deterministic("call failed: model call exceeded 300s"))
        # F1: reasoning-exhausted -> exactly one thinking-off retry, then
        #     deterministic. Fakes accept **kw so the retry's gen_extra is seen.
        _f1 = []
        _msg = ("router not-ok: cheap: reasoning-exhausted: max_tokens=16384 "
                "reasoning_tokens=16384")
        def fake_f1(system, user, tier, label, **kw):
            _f1.append(kw.get("gen_extra"))
            if len(_f1) == 1:
                raise RuntimeError(_msg)
            return ("```python\n# filename: a.py\nx = 1\n```", tier, "m")
        _prev_rc = globals()["_router_call"]
        globals()["_router_call"] = fake_f1
        try:
            _t, _, _ = call_model("s", "u", "cheap")
            ok("F1: exhausted once -> exactly one retry, result returned",
               len(_f1) == 2 and "x = 1" in _t)
            ok("F1: first attempt has no extra, retry carries thinking-off",
               _f1[0] is None and _f1[1] == _THINKING_OFF)
            _f1.clear()
            def fake_f1b(system, user, tier, label, **kw):
                _f1.append(1); raise RuntimeError(_msg)
            globals()["_router_call"] = fake_f1b
            try:
                call_model("s", "u", "cheap"); _raised = None
            except RuntimeError as e:
                _raised = str(e)
            ok("F1: exhausted twice -> raises after exactly two attempts",
               _raised is not None and _EXHAUSTED in _raised and len(_f1) == 2)
            ok("F1: reasoning-exhausted classed deterministic (no paid replay)",
               _is_deterministic(_raised))
            _f1.clear()
            def fake_f1c(system, user, tier, label, **kw):
                _f1.append(1); raise RuntimeError("router not-ok: cheap: other")
            globals()["_router_call"] = fake_f1c
            try:
                call_model("s", "u", "cheap")
            except RuntimeError:
                pass
            ok("F1: a different failure gets NO thinking-off retry", len(_f1) == 1)
            _f1.clear()
            def fake_f1d(system, user, tier, label, **kw):
                _f1.append(1)
                raise RuntimeError(_msg + "|thinking-off-retried")
            globals()["_router_call"] = fake_f1d
            try:
                call_model("s", "u", "cheap"); _raised = None
            except RuntimeError as e:
                _raised = str(e)
            ok("A+ D3: router already retried thinking-off -> NO second retry here, "
               "still deterministic",
               len(_f1) == 1 and _raised is not None and _is_deterministic(_raised))
        finally:
            globals()["_router_call"] = _prev_rc
        con_d = connect()
        transition(con_d, "det", "det", "project_new", "t",
                   "INSERT INTO projects(pid,name,master,state,calls_used,"
                   "call_cap,created,updated,commissioned_by) "
                   "VALUES(?,?,?,?,0,?,?,?,?)",
                   ("det", "det", "m", "ACTIVE", 80, utcnow(), utcnow(),
                    "treebeard"))
        create_section(con_d, "det", None, 0, "t", "root_section", "d")
        _s = get_section(con_d, "det", "s01")
        _msg = _misfire(con_d, "det", _s, "router not-ok: cheap "
                                          "budget-check failed (fail-closed)")
        _s = get_section(con_d, "det", "s01")
        ok("deterministic blocks on first occurrence",
           _s["state"] == "BLOCKED" and _s["misfires"] == 1,
           "%s/%d" % (_s["state"], _s["misfires"]))
        ok("blocked reason names the cause, not a tally",
           "budget-check failed" in (_s["blocked_reason"] or ""),
           _s["blocked_reason"])

        # MISFIRE vs FAIL (PO ruling 2026-08-24). A reviewer that malfunctions
        # is evidence about the REVIEWER; a reviewer that judges is evidence
        # about the ARTIFACT. Conflating them let the weakest tier veto
        # test-passing code. R1-R6 bind the distinction, R4 is the negative
        # control: a real FAIL must STILL go to repair.
        con_r = connect()
        def _mk(pid_):
            transition(con_r, pid_, pid_, "project_new", "t",
                       "INSERT INTO projects(pid,name,master,state,calls_used,"
                       "call_cap,created,updated,commissioned_by) "
                       "VALUES(?,?,?,?,0,?,?,?,?)",
                       (pid_, pid_, "m", "ACTIVE", 80, utcnow(), utcnow(),
                        "treebeard"))
            create_section(con_r, pid_, None, 0, "t", "root_section", "d")
            upd_section(con_r, pid_, "s01", "seed", "to review",
                        state="REVIEW1", ladder=1, cycles=2)
            return get_section(con_r, pid_, "s01")

        _s = _mk("mis1")
        _review_misfire(con_r, "mis1", _s, 1, "no VERDICT line")
        _s = get_section(con_r, "mis1", "s01")
        ok("R1 tier-1 misfire escalates to tier 2",
           _s["state"] == "REVIEW2" and _s["ladder"] == 2, _s["state"])
        ok("R2 misfire does NOT repair the artifact",
           _s["cycles"] == 2, "cycles=%d" % _s["cycles"])

        _s = _mk("mis3")
        upd_section(con_r, "mis3", "s01", "seed", "t", state="REVIEW3", ladder=3)
        _s = get_section(con_r, "mis3", "s01")
        _review_misfire(con_r, "mis3", _s, 3, "timed out")
        _s = get_section(con_r, "mis3", "s01")
        ok("R3 all tiers misfired -> BLOCKED",
           _s["state"] == "BLOCKED", _s["state"])
        ok("R3b blocked_reason names REVIEW INFRASTRUCTURE, not the artifact",
           "review-infrastructure" in (_s["blocked_reason"] or ""),
           _s["blocked_reason"])
        ok("R3c artifact preserved (cycles untouched)",
           _s["cycles"] == 2, "cycles=%d" % _s["cycles"])

        # NEGATIVE CONTROL: a genuine FAIL must still requeue for repair.
        _s = _mk("realfail")
        _fail_cycle(con_r, "realfail", _s, "review_fail", "tier-1 FAIL", "bad")
        _s = get_section(con_r, "realfail", "s01")
        ok("R4 genuine FAIL still repairs (not laundered into a misfire)",
           _s["state"] == "QUEUED" and _s["cycles"] == 3,
           "%s/%d" % (_s["state"], _s["cycles"]))

        # UNREVIEWABLE: artifact-caused, deterministic, terminal.
        os.environ["MEESEEKS_PROMPT_CAP"] = "300"
        try:
            _s = _mk("unrev")
            _p = get_project(con_r, "unrev")
            handle_review(con_r, _p, _s)
            _s = get_section(con_r, "unrev", "s01")
            ok("R5 oversize review prompt -> UNREVIEWABLE",
               _s["state"] == "UNREVIEWABLE", _s["state"])
            ok("R6 UNREVIEWABLE names a contract violation",
               "contract violation" in (_s["blocked_reason"] or ""),
               _s["blocked_reason"])
        finally:
            os.environ["MEESEEKS_PROMPT_CAP"] = "24000"

        # UNREVIEWABLE must have a terminal path (finding #7 lesson): a
        # state no handler advances and no command can close would strand
        # the project ACTIVE forever.
        con_u = connect()
        transition(con_u, "unrv", "unrv", "project_new", "t",
                   "INSERT INTO projects(pid,name,master,state,calls_used,"
                   "call_cap,created,updated,commissioned_by) "
                   "VALUES(?,?,?,?,0,?,?,?,?)",
                   ("unrv", "unrv", "m", "ACTIVE", 80, utcnow(), utcnow(),
                    "treebeard"))
        create_section(con_u, "unrv", None, 0, "t", "root_section", "d")
        upd_section(con_u, "unrv", "s01", "unreviewable", "contract",
                    state="UNREVIEWABLE",
                    blocked_reason="contract violation: test")
        cmd_close(argparse.Namespace(project="unrv", by="treebeard",
                                     reason="unreviewable artifact"))
        _pj = get_project(connect(), "unrv")
        _sc = get_section(connect(), "unrv", "s01")
        ok("R7 UNREVIEWABLE root can be terminally closed",
           _pj["state"] == "CLOSED_BLOCKED", _pj["state"])
        ok("R8 close preserves UNREVIEWABLE (never synthesized DONE)",
           _sc["state"] == "UNREVIEWABLE", _sc["state"])

        card = open(status_path()).read()
        ok("card shows awaiting",
           "AWAITING_TREEBEARD" in card and "happy" in card)
        cmd_review(argparse.Namespace(project="happy", section="s01",
                                      approve=True, reject=None,
                                      by="selftest", note="ok"))
        dep = os.path.join(os.environ["MEESEEKS_ANVIL_DROPZONE"], "happy_s01")
        ok("approve deposits files",
           os.path.exists(os.path.join(dep, "adder.py"))
           and os.path.exists(os.path.join(dep, "test_adder.py")))
        ok("deposit excludes meta",
           not os.path.exists(os.path.join(dep, "task.md")))
        ok("section DONE after approve",
           get_section(con, "happy", "s01")["state"] == "DONE")
        _p, msg = step_once(con, "happy")
        ok("master done -> NEEDS_RETRO",
           get_project(con, "happy")["state"] == "NEEDS_RETRO", msg)
        fake2 = _FakeRouter(["WHAT WORKED\n- everything\nWHAT DID NOT\n"
                             "- nothing\nSUGGESTED DIRECTIVE CHANGES\n"
                             "- none\n"])
        _router_call = fake2
        cmd_retro(argparse.Namespace(project="happy"))
        ok("retro.md written", os.path.exists(retro_path("happy")))
        ok("project CLOSED", get_project(con, "happy")["state"] == "CLOSED")
        ok("retro used t3", fake2.calls and fake2.calls[0][2] == "t3")

        # -- split, CANT, retask, review FAIL, fix, parent auto-close --------
        fake3 = _FakeRouter([
            _r_split(["Write a multiplier module (mul.py) with tests.",
                      "Write a subtractor module (subber.py) with tests."]),
            _r_cant("underspecified"),
            _r_done([("mul.py", _MUL), ("test_mul.py", _TEST_MUL)]),
            _r_fail("mul lacks an intent comment on the function"),
            _r_done([("mul.py", _MUL), ("test_mul.py", _TEST_MUL)]),
            _r_pass(), _r_pass(), _r_pass(),
            _r_done([("subber.py", _SUBBER),
                     ("test_subber.py", _TEST_SUBBER)]),
            _r_pass(), _r_pass(), _r_pass(),
        ])
        _router_call = fake3
        cmd_new(argparse.Namespace(name="splitter", by="treebeard",
                                   task=["Build", "math", "helpers."],
                                   task_file=None))
        step_once(con, "splitter")
        ok("root SPLIT with children",
           get_section(con, "splitter", "s01")["state"] == "SPLIT"
           and get_section(con, "splitter", "s02") is not None
           and get_section(con, "splitter", "s03") is not None)
        step_once(con, "splitter")
        s02 = get_section(con, "splitter", "s02")
        # F10: CANT at a non-final stage ADVANCES instead of blocking.
        ok("F10 CANT at stage 0 advances to stage 1, not BLOCKED",
           s02["state"] == "QUEUED" and s02["stage"] == 1
           and s02["blocked_reason"] is None)
        ok("F10 CANT charges the attempt that produced it",
           s02["cant_attempt"] == 1 and s02["cant_stage"] == 0)
        ok("F10 CANT reason recorded as an unverified claim",
           "underspecified" in (s02["cant_reason"] or "")
           and s02["cant_claim_status"] == "unverified")
        ok("F10 next stage starts with a clean attempt count",
           s02["stage_attempts"] == 0)
        _p = build_draft_prompt(get_project(con, "splitter"), s02)
        ok("F10 prior CANT reaches the next stage as a quarantined claim",
           "PRIOR_STAGE_CLAIM" in _p and "underspecified" in _p
           and "NOT evidence" in _p)
        ok("F10 the section task text was NOT mutated",
           "PRIOR_STAGE_CLAIM" not in read_task("splitter", "s02"))
        cmd_retask(argparse.Namespace(project="splitter", section="s02",
                                      task=["Write", "mul.py", "with",
                                            "a", "test."],
                                      by="selftest"))
        ok("retask requeues",
           get_section(con, "splitter", "s02")["state"] == "QUEUED")
        step_once(con, "splitter")   # s02 fix-draft DONE
        step_once(con, "splitter")   # s02 jail -> REVIEW1
        step_once(con, "splitter")   # t1 FAIL
        s02 = get_section(con, "splitter", "s02")
        ok("review FAIL requeues with cycle",
           s02["state"] == "QUEUED" and s02["cycles"] == 1)
        ok("feedback recorded",
           "intent comment" in read_feedback("splitter", "s02"))
        step_once(con, "splitter")   # fix DONE
        step_once(con, "splitter")   # jail
        step_once(con, "splitter")
        step_once(con, "splitter")
        step_once(con, "splitter")
        ok("s02 ladder complete after fix",
           get_section(con, "splitter", "s02")["state"]
           == "AWAITING_TREEBEARD")
        step_once(con, "splitter")   # s03 draft
        step_once(con, "splitter")   # jail
        step_once(con, "splitter")
        step_once(con, "splitter")
        step_once(con, "splitter")
        ok("s03 ladder complete",
           get_section(con, "splitter", "s03")["state"]
           == "AWAITING_TREEBEARD")
        cmd_review(argparse.Namespace(project="splitter", section="s02",
                                      approve=True, reject=None,
                                      by="selftest", note=None))
        cmd_review(argparse.Namespace(project="splitter", section="s03",
                                      approve=True, reject=None,
                                      by="selftest", note=None))
        _p, msg = step_once(con, "splitter")
        ok("parent auto-DONE + retro pending",
           get_section(con, "splitter", "s01")["state"] == "DONE"
           and get_project(con, "splitter")["state"] == "NEEDS_RETRO", msg)

        # -- implicit DONE: files present, no verdict line -------------------
        fakeimp = _FakeRouter([
            _r_done_noverdict([("adder.py", _ADDER),
                               ("test_adder.py", _TEST_ADDER)]),
            _r_pass(), _r_pass(), _r_pass(),
        ])
        _router_call = fakeimp
        cmd_new(argparse.Namespace(name="implicit", by="treebeard",
                                   task=["Adder", "no", "verdict", "line."],
                                   task_file=None))
        _p, msg = step_once(con, "implicit")
        ok("no-verdict + files -> TESTING (implicit DONE)",
           get_section(con, "implicit", "s01")["state"] == "TESTING", msg)
        vrow = con.execute("SELECT verdict FROM meeseeks WHERE pid='implicit' "
                           "AND mid='m0001'").fetchone()
        ok("implicit DONE recorded distinctly",
           vrow and vrow["verdict"] == "DONE(implicit)")

        # -- misfires, reject, gate refusal, budget --------------------------
        fake4 = _FakeRouter(["garbage with no verdict at all",
                             "still nothing parseable"])
        _router_call = fake4
        cmd_new(argparse.Namespace(name="misf", by="treebeard", task=["Do", "a", "thing."],
                                   task_file=None))
        step_once(con, "misf")
        s = get_section(con, "misf", "s01")
        ok("misfire retries first",
           s["state"] == "QUEUED" and s["misfires"] == 1)
        step_once(con, "misf")
        ok("misfire cap blocks",
           get_section(con, "misf", "s01")["state"] == "BLOCKED")
        cmd_review(argparse.Namespace(project="misf", section="s01",
                                      approve=False, reject="be specific",
                                      by="selftest", note=None))
        s = get_section(con, "misf", "s01")
        ok("reject requeues clean",
           s["state"] == "QUEUED" and s["cycles"] == 0
           and s["misfires"] == 0
           and "be specific" in read_feedback("misf", "s01"))
        try:
            cmd_review(argparse.Namespace(project="misf", section="s01",
                                          approve=True, reject=None,
                                          by="selftest", note=None))
            refused = False
        except SystemExit:
            refused = True
        ok("approve refused off-state", refused)
        cmd_fund(argparse.Namespace(project="misf", calls=2, by="selftest"))
        _p, msg = step_once(con, "misf")
        ok("budget refuses fail-closed",
           (not _p) and "budget exhausted" in msg, msg)
        nrefused = con.execute(
            "SELECT COUNT(*) FROM events WHERE pid='misf' AND "
            "kind='budget_refused'").fetchone()[0]
        ok("budget refusal audited", nrefused >= 1)

        # -- jail gates (R7) --------------------------------------------------
        fake5 = _FakeRouter([
            _r_done([("plain.py", _ADDER_NOCOMMENT),
                     ("test_plain.py",
                      _TEST_ADDER.replace("adder", "plain"))]),
            _r_done([("plain.py", _ADDER)]),
            _r_done([("adder.py", _ADDER),
                     ("test_adder.py", _TEST_ADDER_BAD)]),
            _r_done([("adder.py", _ADDER), ("test_adder.py", _TEST_ADDER)]),
        ])
        _router_call = fake5
        cmd_new(argparse.Namespace(name="gates", by="treebeard",
                                   task=["Adder", "behind", "gates."],
                                   task_file=None))
        step_once(con, "gates")
        step_once(con, "gates")
        ok("comment gate fails",
           get_section(con, "gates", "s01")["state"] == "QUEUED"
           and "no comments" in read_feedback("gates", "s01"))
        step_once(con, "gates")
        step_once(con, "gates")
        ok("missing-test gate fails",
           "no test_" in read_feedback("gates", "s01"))
        step_once(con, "gates")
        step_once(con, "gates")
        ok("failing tests fail jail",
           "tests failed" in read_feedback("gates", "s01"))
        gr = os.path.join(proj_dir("gates"), "receipts")
        ok("fail receipt + feedback pointer",
           os.path.isdir(gr) and len(os.listdir(gr)) >= 3
           and "receipt: " in read_feedback("gates", "s01"))
        step_once(con, "gates")
        _p, msg = step_once(con, "gates")
        s = get_section(con, "gates", "s01")
        ok("good draft passes jail",
           s["state"] == "REVIEW1" and s["cycles"] == 3, msg)

        # -- oversize blocks before any spend ---------------------------------
        os.environ["MEESEEKS_PROMPT_CAP"] = "300"
        _router_call = _FakeRouter([])
        cmd_new(argparse.Namespace(name="fat", by="treebeard", task=["tiny", "task"],
                                   task_file=None))
        step_once(con, "fat")
        ok("oversize blocks before spend",
           get_section(con, "fat", "s01")["state"] == "BLOCKED"
           and get_project(con, "fat")["calls_used"] == 0)
        del os.environ["MEESEEKS_PROMPT_CAP"]

        # -- call timeout is a death, recorded --------------------------------
        def _sleepy(system, user, tier, label):
            time.sleep(3)
            return "late", tier, "slow"
        _router_call = _sleepy
        os.environ["MEESEEKS_CALL_TIMEOUT"] = "1"
        cmd_new(argparse.Namespace(name="slow", by="treebeard", task=["a", "task"],
                                   task_file=None))
        _p, msg = step_once(con, "slow")
        os.environ.pop("MEESEEKS_CALL_TIMEOUT", None)
        s = get_section(con, "slow", "s01")
        ok("call timeout -> misfire",
           s["misfires"] == 1 and "exceeded" in msg, msg)

        # -- directive edits reach the very next spawn (R1) --------------------
        with open(directive_path(), "a") as fh:
            fh.write("\nMAGICTOKEN-7Q\n")
        fake7 = _FakeRouter([_r_cant("noted")])
        _router_call = fake7
        cmd_new(argparse.Namespace(name="dirp", by="treebeard", task=["another", "task"],
                                   task_file=None))
        step_once(con, "dirp")
        ok("directive edit reaches prompt",
           fake7.calls and "MAGICTOKEN-7Q" in fake7.calls[0][1])

        # -- terminal close (finding #7) ------------------------------
        cmd_close(argparse.Namespace(project="fat", by="selftest",
                                     reason="capability test"))
        ok("blocked-root close -> CLOSED_BLOCKED",
           get_project(con, "fat")["state"] == "CLOSED_BLOCKED")
        ok("close preserves BLOCKED section",
           get_section(con, "fat", "s01")["state"] == "BLOCKED")
        nclose = con.execute(
            "SELECT COUNT(*) FROM events WHERE pid='fat' AND "
            "kind='closed_blocked' AND detail LIKE '%capability test%'"
        ).fetchone()[0]
        ok("close event carries actor+reason", nclose == 1)
        try:
            cmd_close(argparse.Namespace(project="happy", by="selftest",
                                         reason="x"))
            _r = False
        except SystemExit:
            _r = True
        ok("close refused on CLOSED project", _r)
        try:
            cmd_close(argparse.Namespace(project="gates", by="selftest",
                                         reason="x"))
            _r = False
        except SystemExit:
            _r = True
        ok("close refused when root not BLOCKED", _r)
        try:
            cmd_close(argparse.Namespace(project="slow", by="selftest",
                                         reason="   "))
            _r = False
        except SystemExit:
            _r = True
        ok("close refused on empty reason", _r)
        ok("card shows CLOSED_BLOCKED distinctly",
           "CLOSED_BLOCKED" in open(status_path()).read())

        # -- invariants ---------------------------------------------------------
        pend = con.execute("SELECT COUNT(*) FROM meeseeks WHERE "
                           "verdict='PENDING'").fetchone()[0]
        ok("no PENDING meeseeks left", pend == 0)
        # --- F10 REGRESSION (PO ruling 2026-08-25): the category error that
        # cost K3 a stage. A worker CANT must not end the job while a funded
        # stage sits unasked. Scripted: stage 0 refuses, stage 1 delivers.
        fakeF10 = _FakeRouter([
            _r_cant("this cannot be written under the stated restrictions"),
            _r_done([("mul.py", _MUL), ("test_mul.py", _TEST_MUL)]),
            _r_pass(), _r_pass(), _r_pass(),
        ])
        _router_call = fakeF10
        cmd_new(argparse.Namespace(name="f10", by="treebeard",
                                   task=["Write", "mul.py", "with", "a", "test."],
                                   task_file=None))
        for _ in range(12):
            sF = get_section(con, "f10", "s01")
            if sF["state"] in ("AWAITING_TREEBEARD", "DONE", "BLOCKED"):
                break
            step_once(con, "f10")
        sF = get_section(con, "f10", "s01")
        ok("F10 REGRESSION: stage-0 CANT then stage-1 success -> section completes",
           sF["state"] in ("AWAITING_TREEBEARD", "DONE") and sF["stage"] == 1)
        _tiers = [c[2] for c in fakeF10.calls]
        ok("F10 REGRESSION: accepted draft came from a DIFFERENT tier than the CANT",
           len(_tiers) >= 2 and _tiers[0] != _tiers[1])
        ok("F10 REGRESSION: overturned claim still on record as unverified",
           sF["cant_stage"] == 0 and sF["cant_claim_status"] == "unverified"
           and "restrictions" in (sF["cant_reason"] or ""))
        fakeF10b = _FakeRouter([_r_cant("genuinely impossible")])
        _router_call = fakeF10b
        cmd_new(argparse.Namespace(name="f10b", by="treebeard",
                                   task=["Write", "mul.py", "with", "a", "test."],
                                   task_file=None))
        _last = last_stage()
        upd_section(con, "f10b", "s01", "test_setup", "pin to final stage",
                    stage=_last)
        step_once(con, "f10b")
        sFb = get_section(con, "f10b", "s01")
        ok("F10 CANT at the final stage blocks as FACTORY_EXHAUSTED",
           sFb["state"] == "BLOCKED"
           and "FACTORY_EXHAUSTED" in (sFb["blocked_reason"] or "")
           and sFb["stage"] == _last)
        kinds = set(r["kind"] for r in
                    con.execute("SELECT DISTINCT kind FROM events"))
        need = {"init", "project_new", "call", "spawn", "drafted",
                "tests_pass", "tests_fail", "review_pass", "review_fail",
                "ladder_complete", "approved", "split", "cant",
                "cant_escalate", "retasked",
                "misfire", "blocked", "children_done", "retro", "funded",
                "budget_refused", "master_done", "rejected"}
        ok("audit trail covers the machine", need.issubset(kinds),
           "missing: %s" % sorted(need - kinds))
        ok("staging empty after all ops",
           os.listdir(staging_dir()) == [])
        leftovers = [f for f in os.listdir(os.path.dirname(status_path()))
                     if f.startswith(".tmp-")]
        ok("card writes atomic", leftovers == [])

        _CONTAINMENT = saved_c3
        fails = [r for r in results if not r[1]]
        print("SELFTEST %d/%d PASS" % (len(results) - len(fails),
                                       len(results)))
        if fails:
            for name, _c, note in fails:
                print("FAILED: %s  %s" % (name, note))
            sys.exit(1)
    finally:
        _router_call = saved_router
        _JAIL_IMPL = saved_jail
        os.environ.clear()
        os.environ.update(saved_env)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        prog="meeseeks", description="The Meeseeks Box (bridge mode).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init",
                   help="create the data tree, directive, ledger, card")
    p = sub.add_parser("new", help="create a project from a master task")
    p.add_argument("name")
    p.add_argument("task", nargs="*")
    p.add_argument("--task-file")
    p.add_argument("--by", required=True,
                   help="commissioning governed agent (recorded, not yet "
                        "enforced -- R3 enforcement deferred)")
    p = sub.add_parser("step",
                       help="advance one transition (at most one model call)")
    p.add_argument("project")
    p = sub.add_parser("run",
                       help="loop steps until blocked/done or the step cap")
    p.add_argument("project")
    p.add_argument("--max-steps", type=int, default=0)
    p = sub.add_parser("status", help="print the status card (+ detail)")
    p.add_argument("project", nargs="?")
    sub.add_parser("digest", help="print the status card")
    p = sub.add_parser("review",
                       help="treebeard gate: approve (deposit) or reject")
    p.add_argument("project")
    p.add_argument("section")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", metavar="REASON")
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p = sub.add_parser("retask",
                       help="treebeard gate: replace a section's task")
    p.add_argument("project")
    p.add_argument("section")
    p.add_argument("task", nargs="+")
    p.add_argument("--by", required=True)
    p = sub.add_parser("fund", help="treebeard gate: set a project call cap")
    p.add_argument("project")
    p.add_argument("--calls", type=int, required=True)
    p.add_argument("--by", required=True)
    p = sub.add_parser("retro",
                       help="treebeard gate: retrospective, then CLOSE")
    p.add_argument("project")
    p = sub.add_parser("directive", help="show the directive or its path")
    p.add_argument("what", choices=["show", "path"], nargs="?",
                   default="show")
    p = sub.add_parser("close",
                       help="terminal close: ACTIVE project with BLOCKED root")
    p.add_argument("project")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    sub.add_parser("selftest",
                   help="full pipeline in a temp root, scripted fake router")
    args = ap.parse_args()
    return {
        "init": cmd_init, "new": cmd_new, "step": cmd_step, "run": cmd_run,
        "status": cmd_status, "digest": cmd_digest, "review": cmd_review,
        "retask": cmd_retask, "fund": cmd_fund, "retro": cmd_retro,
        "close": cmd_close,
        "directive": cmd_directive, "selftest": cmd_selftest,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
