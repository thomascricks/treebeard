#!/usr/bin/env python3
# -*- coding: ascii -*-
"""boxread.py -- bounded READ-ONLY inspection of Treebeard's own box.

Four read classes, one contract: every call returns a dict whose "status"
is OK or UNVERIFIED. There is no third outcome. An unreadable fact is
named as unreadable, with a reason, so it can never be silently replaced
by a plausible answer -- and an empty result is never reported as an
absence of facts.

This module owns the MECHANICS the agent must not invent: unit names,
journal bounds, path allow-lists, board access. It runs no shell, builds
no command strings, and contains no call that writes. It is not a general
command surface and must never grow into one.

Board and execution truth come from board.py and opshell.py -- the same
reads the console uses. No second state logic lives here. The board is the
PLANNING record, not execution state; its freshness is judged by
statusboard's one board-staleness rule (BOARD-TRUTH S1), never here.
"""
import json
import os
import pwd
import re
import stat
import subprocess
import sys

_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
LIB = "%s/shared/lib" % _TB_BASE
if LIB not in sys.path:
    sys.path.insert(0, LIB)

TIMEOUT = int(os.environ.get("BOXREAD_TIMEOUT", "10"))
FILE_MAX = int(os.environ.get("BOXREAD_FILE_MAX", "65536"))
JOURNAL_N = int(os.environ.get("BOXREAD_JOURNAL_N", "20"))
JOURNAL_MAX = int(os.environ.get("BOXREAD_JOURNAL_MAX", "100"))

# PO ruling: narrow. Readable directories are listed EXACTLY and are NOT
# recursive -- a file is readable only when its resolved parent equals one
# of these. /etc/treebeard/credentials and /etc/treebeard/personality are
# subdirectories, so they are unreachable by construction rather than by
# pattern match, and no symlink or ../ can re-enter them: realpath is
# resolved first and the parent must match exactly.
# Release prep R3/S1 (D2 read-access closure): the dream and meeting stores
# are flat directories, added EXACTLY (their subdirectories, e.g.
# dreams/archive, stay unreachable by the same exact-parent rule).
DREAMS_DIR = "%s/shared/memory/dreams" % _TB_BASE
MEETINGS_DIR = "%s/shared/memory/meetings" % _TB_BASE
ALLOW_DIRS = ("%s/shared/lib" % _TB_BASE, "%s/treebeard" % _TB_BASE,
              "/etc/treebeard", DREAMS_DIR, MEETINGS_DIR)
# Unit files, restricted to this project's own units by name.
UNIT_DIR = "/etc/systemd/system"
UNIT_PREFIX = "treebeard"

# Second layer. The structural rule above already excludes these; both
# are kept because either alone would be sufficient and neither is free.
DENY_SUBSTR = ("credential", "personality", "operator_token",
               "session_secret", "/.ssh/", "providers")
DENY_SUFFIX = (".env", ".key", ".pem", ".db", ".sqlite", ".sqlite3",
               ".bak", ".pyc")
ALLOW_SUFFIX = (".py", ".md", ".txt", ".conf", ".json", ".toml", ".sh",
                ".service", ".timer", ".example")

_UNIT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "0123456789@._-")
_PROPS = ("Id", "LoadState", "Description", "Type", "User", "ActiveState",
          "SubState", "UnitFileState", "Result", "ExecMainStartTimestamp",
          "ActiveEnterTimestamp", "NextElapseUSecRealtime",
          "LastTriggerUSec", "Unit")

# Each operation is described with its argument on its own ARGS line.
# The earlier one-line form ("box.service  <unit>") showed the operation
# and its argument together -- the exact malformed shape a frontier turn
# then wrote (2026-09-01, probe 3), because the table contradicted the
# block format directly above it. One format is shown here, not two:
# the interface owns the mechanics, so an agent must never have to
# choose between two layouts the same block presents as correct.
OPS_HELP = (
    "box.cards    -- every card that is not done: the PLANNING record,\n"
    "                with the board's last write and age (STALE = old)\n"
    "                ARGS: (leave empty)\n"
    "box.card     -- one card's phase and linked factory state, with the\n"
    "                board's last write and age\n"
    "                ARGS: <card id>\n"
    "box.units    -- every treebeard-* service AND timer\n"
    "                ARGS: (leave empty)\n"
    "box.service  -- one treebeard-* unit's live state\n"
    "                ARGS: <unit>\n"
    "box.journal  -- last N entries for a treebeard-* unit\n"
    "                ARGS: <unit> [lines]\n"
    "box.file     -- one readable file's current content\n"
    "                ARGS: <absolute path>\n"
    "box.status   -- statusboard card: a job's reported outcome and age,\n"
    "                FRESH or UNVERIFIED (box.service is NOT job outcome)\n"
    "                ARGS: <card id> or all\n"
    "box.writer   -- the trusted board writer: who can change the board,\n"
    "                whether its code matches the install, its receipts,\n"
    "                and which cards came through the root-gated writer\n"
    "                ARGS: (leave empty)\n"
    "box.ledger   -- one Meeseeks project's newest ledger events (spawn,\n"
    "                verdict, stage, error), newest first\n"
    "                ARGS: <project> [rows]\n"
    "box.project  -- a Meeseeks project's receipts, transcripts or notepad:\n"
    "                a listing, or one file by name (long files are paged)\n"
    "                ARGS: <project> <receipts|transcripts|notepad> [file] [page]\n"
    "box.log      -- last N lines of a Treebeard log: cli (today, or\n"
    "                cli:YYYY-MM-DD), consolidation (moon), flags\n"
    "                (subconscious), sun, moon (agent output)\n"
    "                ARGS: <log> [lines]\n"
    "box.shortterm -- recent exchanges across threads: one agent, all\n"
    "                agents, or one thread id\n"
    "                ARGS: <agent|all|thread:ID> [rows] [days]\n"
    "box.memory   -- search an agent's long-term memory or its RAW\n"
    "                subconscious fragments, on any turn\n"
    "                ARGS: <longterm|subconscious> <agent> <query words>\n"
    "box.dream    -- an agent's dream journal: newest entries, or one\n"
    "                consolidated week (latest dream: box.file on\n"
    "                shared/memory/dreams/AGENT.latest.txt)\n"
    "                ARGS: <agent> [entries] [week like 2026-W40]\n"
    "box.meetings -- meeting transcripts, newest first; read one with\n"
    "                box.file on the path it lists\n"
    "                ARGS: [participant] [count]")


def _ok(kind, **f):
    d = {"status": "OK", "kind": kind}
    d.update(f)
    return d


def _unverified(kind, reason, **f):
    """The ONLY failure shape. Callers get a reason, never a guess."""
    d = {"status": "UNVERIFIED", "kind": kind, "reason": reason}
    d.update(f)
    return d


def _clean(v):
    """Reject anything that is not a plain, single-line, sane string.
    Malformed input fails here rather than reaching a command."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or len(v) > 512:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    return v


def _run(argv):
    """Fixed-argv subprocess, no shell, bounded timeout. Returns
    (True, output) or (False, reason). A NON-ZERO EXIT IS A FAILURE:
    stderr from a failed command must never be handed back as data."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "command timed out after %ds" % TIMEOUT
    except Exception as e:
        return False, "command could not be executed: %s" % (e,)
    if p.returncode != 0:
        return False, ("command failed rc=%d: %s"
                       % (p.returncode,
                          ((p.stderr or p.stdout or "").strip()[:300]
                           or "no output")))
    return True, (p.stdout or "").strip()


# --- read class 1: board / cards (planning record, freshness-judged) --
#
# BOARD-TRUTH S1 (2026-10-01). The board records planning intent: what was
# posted, by whom, its phase. It is not execution state, and on 2026-10-01 it
# had not been written for 29 days while every read of it looked current.
# Every board read now carries the board's last write and age, judged by
# statusboard's ONE staleness rule (the threshold Sun's digest uses). A
# STALE or unjudgeable board is UNVERIFIED: its cards are still shown, but
# only under "recorded", rendered as AS LAST RECORDED -- NOT CURRENT STATE.

BOARD_MEANING = ("planning record (what was posted, owner, phase); NOT "
                 "execution state -- job outcome is box.status, unit state "
                 "is box.units / box.service")


def _board():
    import board
    import opshell
    import uistate
    return board, opshell, uistate


def _board_freshness(board, now=None):
    """(fields, stale) or (None, reason). The verdict is statusboard's
    (FRESH / UNCHANGED / STALE, L-R7); this only formats it."""
    try:
        import statusboard
        fr = statusboard.board_freshness(board.board_path(), now)
    except Exception as e:
        return None, "board freshness could not be established: %s" % (e,)
    f = {"board_last_write": fr["last_write_utc"],
         "board_age": _age_text(fr["age_s"]),
         "board_window": "%gh" % fr["window_h"],
         "meaning": BOARD_MEANING}
    verdict = fr.get("verdict", "STALE" if fr["stale"] else "FRESH")
    f["board_verdict"] = verdict
    if fr.get("reconciled_utc"):
        f["board_reconciled"] = "%s (age %s)%s" % (
            fr["reconciled_utc"], _age_text(fr["reconcile_age_s"]),
            "" if fr.get("reconcile_matches")
            else "; board changed since that reconcile")
    else:
        f["board_reconciled"] = "never (no reconcile stamp)"
    if fr.get("inconsistent"):
        f["board_inconsistent"] = ("reconciler found board != execution for "
                                   "card(s) %s" % ", ".join(
                                       "#%d" % i for i in fr["inconsistent"]))
    return f, fr["stale"]


def _board_verdict(kind, board, recorded, now=None, **ok_fields):
    """OK only for a FRESH board, or an UNCHANGED one the board machinery
    reconciled within the window (L-R7). Otherwise UNVERIFIED with the
    reason, and the read kept under `recorded` so it is never presented as
    current."""
    fresh, stale = _board_freshness(board, now)
    if fresh is None:
        return _unverified(kind, "%s; card states are withheld as current"
                           % stale, meaning=BOARD_MEANING, recorded=recorded)
    if stale:
        return _unverified(
            kind, "STALE: board last written %s, age %s exceeds the %s board "
            "window, and the board machinery has not reconciled it within "
            "that window (last reconcile: %s); card states are withheld as "
            "current"
            % (fresh["board_last_write"], fresh["board_age"],
               fresh["board_window"], fresh["board_reconciled"]),
            recorded=recorded, **fresh)
    ok_fields.update(fresh)
    if fresh["board_verdict"] == "UNCHANGED":
        return _ok(kind, freshness="UNCHANGED since %s (no transition in "
                   "the %s window; the board machinery reconciled these "
                   "exact board bytes at %s)"
                   % (fresh["board_last_write"], fresh["board_window"],
                      fresh["board_reconciled"]), **ok_fields)
    return _ok(kind, freshness="FRESH (board written within its %s window)"
               % fresh["board_window"], **ok_fields)


def cards_active(now=None):
    """Every OPEN card with its resolved operator state (L: done and closed
    cards are finished and appear on box.card / Recently Finished), read through
    board.py as a library and opshell's immutable ledger handle -- never
    from rendered HTML, never from board.json directly."""
    try:
        board, opshell, uistate = _board()
        items = board.load_board()
        keyed = opshell.resolve_items(
            [c for c in items if c.get("status") == "open"])
    except Exception as e:
        return _unverified("cards", "board could not be read: %s" % (e,))
    out = []
    for c, key in keyed:
        out.append("#%s [%s] phase=%s owner=%s pid=%s :: %s"
                   % (c.get("id"), uistate.get(key)[0], c.get("phase"),
                      c.get("owner") or "unassigned",
                      c.get("meeseeks_pid") or "none",
                      (c.get("text") or "")[:120]))
    return _board_verdict("cards", board, out, now, count=len(out), rows=out,
                          source="board.py library + meeseeks ledger "
                                 "(immutable)")


def card_state(card_id, now=None):
    """One card's phase plus its linked execution state. A card that does
    not exist is UNVERIFIED, never an empty card."""
    raw = _clean(card_id if isinstance(card_id, str) else str(card_id))
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return _unverified("card", "card id is not a number: %r" % (card_id,))
    try:
        board, opshell, uistate = _board()
        c = board.find(board.load_board(), cid)
    except Exception as e:
        return _unverified("card", "board could not be read: %s" % (e,))
    if not c:
        return _unverified("card", "no card with id %d exists on the board"
                           % cid)
    d = opshell.card_detail(c)
    key = uistate.resolve(c, d)
    f = dict(id=cid, text=c.get("text"), state=uistate.get(key)[0],
             lifecycle=(board.canonical_state(c)
                        if hasattr(board, "canonical_state") else None),
             rev=c.get("rev") or 0, outcome=c.get("outcome"),
             phase=c.get("phase"), card_status=c.get("status"),
             pid=c.get("meeseeks_pid"), project_state=d.get("project_state"),
             section_state=d.get("state"), stage=d.get("stage"),
             blocked_reason=d.get("reason"), since=d.get("since"))
    return _board_verdict("card", board, f, now,
                          source="board.py library + meeseeks ledger "
                                 "(immutable)", **f)


# --- read class 2: systemd units AND timers ---------------------------

def _valid_unit(unit):
    u = _clean(unit)
    if not u or len(u) > 128 or not set(u) <= _UNIT_OK:
        return None
    # Narrowed with the paths: this interface inspects THIS project's
    # units. Any other unit name is refused rather than answered.
    if not u.startswith(UNIT_PREFIX):
        return None
    return u


def _show(unit):
    ok, out = _run(["systemctl", "show", unit] +
                   ["-p%s" % p for p in _PROPS])
    if not ok:
        return None, out
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    # systemctl show EXITS 0 for a unit that does not exist. Absence is
    # detected from LoadState, never inferred from a quiet result.
    if props.get("LoadState") == "not-found":
        return None, "systemd knows no unit by that name"
    return props, None


def _row(p, name):
    return {"unit": p.get("Id", name), "type": p.get("Type") or "timer",
            "active": p.get("ActiveState"), "sub": p.get("SubState"),
            "enabled": p.get("UnitFileState"),
            "last_start": p.get("ExecMainStartTimestamp") or None,
            "last_trigger": p.get("LastTriggerUSec") or None,
            "next_run": p.get("NextElapseUSecRealtime") or None,
            "triggers": p.get("Unit") or None, "result": p.get("Result")}


def units():
    """Every treebeard-* service AND timer with live state. The prefix is
    fixed here, not taken from input. Timer-driven oneshots are normally
    inactive between runs -- reported as the fact it is, not an error."""
    ok, out = _run(["systemctl", "list-units", "--all", "--no-pager",
                    "--no-legend", "%s*" % UNIT_PREFIX])
    if not ok:
        return _unverified("units", out)
    names = []
    for line in out.splitlines():
        parts = line.replace("\u25cf", " ").split()
        if parts and (parts[0].endswith(".service")
                      or parts[0].endswith(".timer")):
            names.append(parts[0])
    rows, bad = [], 0
    for n in names:
        p, err = _show(n)
        if p is None:
            bad += 1
            rows.append({"unit": n, "state": "UNVERIFIED", "reason": err})
            continue
        rows.append(_row(p, n))
    running = [r for r in rows if r.get("sub") == "running"]
    return _ok("units", count=len(rows), running=len(running),
               unreadable=bad, rows=rows,
               note=("oneshot units are inactive between timer runs; "
                     "processes started outside systemd are not units and "
                     "cannot appear here at all"),
               source="systemctl list-units + show")


def service_status(unit):
    """One unit's live state and its last start time, from systemd."""
    u = _valid_unit(unit)
    if not u:
        return _unverified("service", "not a readable %s-* unit name: %r"
                           % (UNIT_PREFIX, unit))
    p, err = _show(u)
    if p is None:
        return _unverified("service", err, unit=u)
    d = _row(p, u)
    return _ok("service", description=p.get("Description"),
               user=p.get("User") or "root",
               meaning="unit exit status, not job outcome",
               source="systemctl show", **d)


# --- read class 3: journal (bounded, completeness asserted) -----------

def _me():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ""


def _privileged():
    """True when this process can read OTHER users' journals. Checked in
    ADVANCE, because journalctl exits 0 with empty output when it cannot
    -- and empty would otherwise read as 'this unit logged nothing'."""
    import grp
    gids = set(os.getgroups())
    for g in ("adm", "systemd-journal"):
        try:
            if grp.getgrnam(g).gr_gid in gids:
                return True
        except KeyError:
            continue
    return False


def journal(unit, n=None):
    """Last n entries for one unit. Bounded by construction: n is capped
    and there is no unbounded form. The result states its own access and
    completeness -- an OK journal read NEVER implies it is the whole log."""
    u = _valid_unit(unit)
    if not u:
        return _unverified("journal", "not a readable %s-* unit name: %r"
                           % (UNIT_PREFIX, unit))
    if n is None:
        n = JOURNAL_N
    else:
        try:
            n = int(_clean(n if isinstance(n, str) else str(n)) or "")
        except (TypeError, ValueError):
            return _unverified("journal", "line count is not a number: %r"
                               % (n,), unit=u)
    n = max(1, min(n, JOURNAL_MAX))
    p, err = _show(u)
    if p is None:
        return _unverified("journal", err, unit=u)
    owner = p.get("User") or "root"
    if owner != _me() and not _privileged():
        return _unverified(
            "journal",
            "insufficient privilege: %s runs as %s, and this process is in "
            "neither adm nor systemd-journal. journalctl would return an "
            "empty result here, which is NOT evidence that the unit logged "
            "nothing." % (u, owner), unit=u, unit_user=owner)
    ok, out = _run(["journalctl", "-q", "-u", u, "-n", str(n), "--no-pager"])
    if not ok:
        return _unverified("journal", out, unit=u)
    lines = [l for l in out.splitlines() if l.strip()]
    complete = len(lines) >= n
    return _ok("journal", unit=u, requested=n, returned=len(lines),
               access="full", window_filled=complete,
               note=("this is the last %d entries only; older entries may "
                     "exist beyond the window or have been rotated" % n),
               rows=lines, source="journalctl -u %s -n %d" % (u, n))


# --- read class 4: files (exact-directory allow-list, bounded) --------

def read_file(path, max_bytes=None):
    """One file's current content. Refused by location, then name, then
    type, then size. Truncation is always stated with the byte counts."""
    p = _clean(path)
    if not p or not p.startswith("/"):
        return _unverified("file", "not an absolute path: %r" % (path,))
    try:
        real = os.path.realpath(p)
    except Exception as e:
        return _unverified("file", "path could not be resolved: %s" % (e,))
    parent, base = os.path.split(real)
    low = base.lower()
    allowed = parent in ALLOW_DIRS or (
        parent == UNIT_DIR and low.startswith(UNIT_PREFIX))
    if not allowed:
        return _unverified("file", "outside the readable directories",
                           path=real)
    if any(s in low for s in DENY_SUBSTR):
        return _unverified("file", "denied: restricted file", path=real)
    if any(low.endswith(s) for s in DENY_SUFFIX) or ".bak-" in low:
        return _unverified("file", "denied: file type is not readable here",
                           path=real)
    if not any(low.endswith(s) for s in ALLOW_SUFFIX):
        return _unverified("file", "not an allowed file type", path=real)
    if not os.path.isfile(real):
        return _unverified("file", "no such file", path=real)
    cap = FILE_MAX
    if max_bytes is not None:
        try:
            cap = max(1, min(int(max_bytes), FILE_MAX))
        except (TypeError, ValueError):
            return _unverified("file", "size limit is not a number: %r"
                               % (max_bytes,), path=real)
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            raw = fh.read(cap + 1)
    except PermissionError:
        return _unverified("file", "permission denied reading it", path=real)
    except Exception as e:
        return _unverified("file", "could not be read: %s" % (e,), path=real)
    if b"\x00" in raw:
        return _unverified("file", "binary content, not readable as text",
                           path=real)
    cut = len(raw) > cap
    text = raw[:cap].decode("utf-8", "replace")
    if cut:
        text += "\n[TRUNCATED: %d of %d bytes shown]" % (cap, size)
    return _ok("file", path=real, size=size, truncated=cut, text=text,
               source="direct read")


# --- read class 5: statusboard cards (generic, policy-gated) ----------
#
# R-B6-SENSOR (a), 2026-10-01. A job's OUTCOME lives on its statusboard
# card, not in its unit's exit status: the 2026-10-01 career run exited 0
# (box.service "result: success") while its card recorded a false-green.
# This op reads cards from ONE store only. The caller names a card id or
# "all"; no path is ever accepted. Freshness is judged HERE, against the
# card's own stale_after_hours -- the agent never judges it. A stale card
# returns UNVERIFIED and withholds its state, so a stale "ok" cannot be
# repeated as a current fact.
#
# Grant: deny by default, per agent, from a root-owned policy file in the
# dispatch syntax ('agent -> box.status'), parsed by dispatch.load_ops so
# no second policy interpreter exists. It is deliberately NOT
# dispatch_ops.conf: an op permitted there satisfies ask.py's dispatch
# third-outcome guard, so a box.status DISPATCH block would pass the
# live-state guard and then be refused at execution. A read is not a
# dispatch.

STATUS_OP = "box.status"
STATUS_DIR = "%s/shared/status" % _TB_BASE
STATUS_POLICY = os.environ.get("BOXREAD_OPS_CONF",
                               "/etc/treebeard/boxread_ops.conf")
STATUS_MAX = 65536
STATUS_SKEW_S = 300
SUMMARY_MAX = 240
_CARD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _status_permitted(agent, op=STATUS_OP):
    """(True, "") or (False, reason). FAIL-CLOSED on every policy fault."""
    a = _clean(agent)
    if not a:
        return False, "no agent identity: %s is granted per agent" % op
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import dispatch
        pairs = dispatch.load_ops(STATUS_POLICY)
    except Exception as e:
        return False, "read policy unavailable, nothing is granted (%s)" % (e,)
    if (a.lower(), op) not in pairs:
        return False, "agent %r is not granted %s" % (a, op)
    return True, ""


def _age_text(sec):
    h, m = divmod(int(sec) // 60, 60)
    d, h = divmod(h, 24)
    t = ("%dd %dh %dm" % (d, h, m)) if d else ("%dh %dm" % (h, m))
    return "%s (%ds)" % (t, int(sec))


def _card_load(cid):
    """(obj, None) or (None, reason). Opens only a regular file whose
    resolved parent IS the store; a symlink out of the store is refused."""
    store = os.path.realpath(STATUS_DIR)
    if not os.path.isdir(store):
        return None, "statusboard store missing"
    real = os.path.realpath(os.path.join(store, cid + ".json"))
    if os.path.dirname(real) != store:
        return None, "card resolves outside the statusboard store"
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "no such card"
    except PermissionError:
        return None, "permission denied reading the card"
    except OSError as e:
        return None, "card could not be opened: %s" % (e.strerror or e,)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None, "card is not a regular file"
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(STATUS_MAX + 1)
    except OSError as e:
        return None, "card could not be read: %s" % (e.strerror or e,)
    if len(raw) > STATUS_MAX:
        return None, "card larger than %d bytes" % STATUS_MAX
    if not raw.strip():
        return None, "card is empty"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "card is not valid JSON"
    if not isinstance(obj, dict):
        return None, "card is not a JSON object"
    return obj, None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _status_one(cid, now=None):
    """One card, normalized. OK only when the card is complete AND fresh."""
    import datetime
    src = os.path.join(STATUS_DIR, cid + ".json")
    obj, why = _card_load(cid)
    if obj is None:
        return _unverified("status", why, card=cid, source=src)
    st = obj.get("status")
    ep = obj.get("updated_epoch")
    win = obj.get("stale_after_hours")
    utc = obj.get("updated_utc")
    if st not in ("ok", "warn", "fail"):
        return _unverified("status", "card has no valid status (ok|warn|fail)",
                           card=cid, source=src)
    if not _num(ep) or ep <= 0:
        return _unverified("status", "card has no updated_epoch",
                           card=cid, source=src)
    if not _num(win) or win <= 0:
        return _unverified("status", "card declares no freshness window "
                           "(stale_after_hours)", card=cid, source=src)
    want = datetime.datetime.fromtimestamp(
        int(ep), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if utc != want:
        return _unverified("status", "updated_utc %r does not match "
                           "updated_epoch (%s)" % (utc, want),
                           card=cid, source=src)
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp()
              if now is None else now)
    age = now - int(ep)
    if age < -STATUS_SKEW_S:
        return _unverified("status", "card timestamp is in the future",
                           card=cid, updated_utc=utc, source=src)
    age = max(age, 0)
    if age > float(win) * 3600.0:
        return _unverified("status", "STALE: card %s written %s, age %s "
                           "exceeds its own %gh window; its state is "
                           "withheld" % (cid, utc, _age_text(age), win),
                           card=cid, updated_utc=utc, age=_age_text(age),
                           stale_after_hours=win, source=src)
    summ = obj.get("summary")
    summ = " ".join(summ.split()) if isinstance(summ, str) else ""
    if len(summ) > SUMMARY_MAX:
        summ = summ[:SUMMARY_MAX] + " [TRUNCATED]"
    run = obj.get("run_id")
    run = run if isinstance(run, str) and _RUN_RE.match(run) \
        else "not recorded on card"
    extra = {}
    lf = obj.get("last_failure")
    if isinstance(lf, dict):
        # L-R8: sticky HISTORY. Labelled so it is never read as the
        # current state or as an authority signal.
        if lf.get("unknown"):
            extra["last_failure"] = ("UNKNOWN (an earlier card was "
                                     "unreadable) -- history only")
        else:
            ls = lf.get("summary")
            ls = " ".join(ls.split())[:SUMMARY_MAX] if isinstance(ls, str) \
                else ""
            lr = lf.get("run_id")
            lr = lr if isinstance(lr, str) and _RUN_RE.match(lr) \
                else "no run_id"
            extra["last_failure"] = (
                "%s run_id=%s | %s -- history only, NOT current state, NOT "
                "an authority signal" % (lf.get("utc") or "time unknown",
                                         lr, ls))
    return _ok("status", card=cid, state=st, summary=summ, run_id=run,
               updated_utc=utc, age=_age_text(age), stale_after_hours=win,
               freshness="FRESH (within the card's own %gh window)" % win,
               source="statusboard %s" % src, **extra)


def _status_row(r):
    if r.get("status") == "OK":
        row = "%s: FRESH state=%s updated_utc=%s age=%s run_id=%s | %s" % (
            r["card"], r["state"], r["updated_utc"], r["age"], r["run_id"],
            r["summary"])
        if r.get("last_failure"):
            row += " || last_failure: %s" % r["last_failure"]
        return row
    return "%s: UNVERIFIED (%s)" % (r.get("card"), r.get("reason"))


def _status_all(now=None):
    store = os.path.realpath(STATUS_DIR)
    try:
        names = sorted(n for n in os.listdir(store) if n.endswith(".json"))
    except OSError as e:
        return _unverified("status", "statusboard store unreadable: %s"
                           % (e.strerror or e,))
    if not names:
        return _unverified("status", "statusboard store holds no cards")
    rows, fresh = [], 0
    for n in names:
        cid = n[:-5]
        if not _CARD_RE.match(cid):
            r = _unverified("status", "not a valid card id", card=cid)
        else:
            r = _status_one(cid, now)
        fresh += r.get("status") == "OK"
        rows.append(_status_row(r))
    return _ok("status", cards=len(rows), fresh=fresh,
               unverified=len(rows) - fresh, rows=rows,
               note="each row carries its own verdict; an UNVERIFIED row "
                    "asserts no state", source="statusboard %s" % STATUS_DIR)


def status_card(card, agent=None, now=None):
    """box.status: one card by id, or "all". Never a path."""
    ok, why = _status_permitted(agent)
    if not ok:
        return _unverified("status", why)
    c = _clean(card)
    if c is None:
        return _unverified("status", "expected: <card id> or all, got %r"
                           % (card,))
    if c.lower() == "all":
        return _status_all(now)
    if not _CARD_RE.match(c):
        return _unverified("status", "not a card id (letters, digits, _ "
                           "only; paths are refused): %r" % (c,))
    return _status_one(c, now)


# --- read class 6: the trusted board writer (CONTAINMENT T2) ----------
#
# Operating model section 7: T2 is observable. One read answers "who can
# change the board, and with what origin?" from four sources, each with its
# own freshness, and says UNVERIFIED/UNKNOWN whenever one cannot be read:
#   * T2_MANIFEST (root-owned, written by the install): the installed hashes;
#     live code that differs is DRIFT, and the read is UNVERIFIED;
#   * the board directory (live stat): T2 leaves board.json writable by uid
#     treebeard (the narrow I3; closed in W), so direct writes are POSSIBLE
#     and their origin is RECORDED;
#   * the writer's receipts (hash chain, readable through group tbaudit):
#     count, last decision, chain verdict;
#   * the board bytes: equal to the last receipt's `after` hash, or written
#     OUTSIDE the writer since then.
# A card is ROOT-GATED when its current (id, rev) is the result of an
# accepted root-gated writer receipt AND the board has not been written
# outside the writer since. Everything is RECORDED: T2 authenticates no
# operator (operator P1; operator identity is not yet separated from the
# service identity -- Router 2.0). sudoers is not readable here: it is reported as recorded at install,
# never as live.

WRITER_OP = "box.writer"
CTL_DIR = "%s/shared/ctl" % _TB_BASE
WRITER_RECEIPTS = "%s/receipts/board.receipts" % CTL_DIR
T2_MANIFEST = "%s/T2_MANIFEST" % CTL_DIR
WRITER_MEANING = ("ROOT-GATED = a change made through board_writer (which "
                  "only root can run), with a verified receipt; it is "
                  "RECORDED, NOT operator-authenticated (Router 2.0). "
                  "RECORDED = every other change too (direct board.py / "
                  "board.json writes). Receipt chains prove order and "
                  "integrity of what is there, not that nothing was trimmed "
                  "from the end")
# The approved T2 claim (Tex, rev-6 ruling 1) -- the MAXIMUM this sensor may
# report about commissioning; it implies no ledger-content integrity after
# commissioning (backlog LEDGER-CONTENT-1, owned by W).
COMMISSION_CLAIM = ("Commissioning requires one-time root-gated action "
                    "(recorded, NOT operator-authenticated: Router 2.0) and "
                    "binds the card to its own unmodified project at "
                    "commission time; project content after commissioning "
                    "is not protected until W.")


def _sha_of(path):
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _utc(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _manifest_check():
    """(fields, problem or None). Lines: 'sha256 <path> <hex>',
    'recorded <path> <hex>' (not readable here), 'installed <ts> by <user>'."""
    try:
        with open(T2_MANIFEST, encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return {}, "no readable T2_MANIFEST (%s): T2 is not installed, or " \
                   "its record is gone" % (e.strerror or e,)
    f, drift, n = {}, [], 0
    for ln in lines:
        parts = ln.split()
        if len(parts) == 4 and parts[0] == "installed" and parts[2] == "by":
            f["installed"] = "%s by %s" % (parts[1], parts[3])
        elif len(parts) == 3 and parts[0] == "sha256":
            n += 1
            try:
                if _sha_of(parts[1]) != parts[2]:
                    drift.append(parts[1])
            except OSError as e:
                drift.append("%s (unreadable: %s)" % (parts[1], e.strerror))
        elif len(parts) == 3 and parts[0] == "recorded":
            f.setdefault("recorded_at_install", []).append(
                "%s %s" % (parts[1], parts[2][:12]))
    f["code_checked"] = n
    if not n:
        return f, "T2_MANIFEST lists no files"
    if drift:
        return f, "writer code DRIFT from the install: %s" % ", ".join(drift)
    return f, None


def _receipts():
    """(records, problem or None); [] when the writer has never run."""
    import importlib.util
    if not os.path.exists(WRITER_RECEIPTS):
        try:
            os.listdir(os.path.dirname(WRITER_RECEIPTS))
        except OSError as e:
            return None, "receipt store unreadable (%s)" % (e.strerror or e,)
        return [], None
    try:
        spec = importlib.util.spec_from_file_location(
            "_tbreceipt_ro", os.path.join(CTL_DIR, "tbreceipt.py"))
        tbr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tbr)
        ok, n, bad = tbr.verify_chain(WRITER_RECEIPTS)
        if not ok:
            return None, "receipt chain BROKEN at record %s of %s" % (bad, n)
        with open(WRITER_RECEIPTS, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()], None
    except OSError as e:
        return None, "receipts unreadable (%s)" % (e.strerror or e,)
    except Exception as e:
        return None, "receipts could not be verified: %s" % (e,)


def _root_gated(r):
    """An accepted writer receipt under the T2 rev-8 authority class:
    written by tbctl (which only root can run), authority "recorded".
    Structural only -- it never means the operator was authenticated
    (operator P1: operator identity is Router 2.0 scope)."""
    idn = r.get("identity") or {}
    return (r.get("decision") == "accepted"
            and idn.get("exec_user") == "tbctl"
            and idn.get("principal") == "root-gated"
            and idn.get("authority") == "recorded"
            and idn.get("actor") == "operator")


def drive_verdict(it, receipts):
    """CONTAINMENT T2 rev 6 ruling 1: None if the reflector may DRIVE this
    card's linked project -- its exact (card, project pid) linkage is backed
    by a USED commission receipt -- else why not. The SAME rule as
    reflector.linkage_problem (kept here so the sensor never imports the
    executor); tests hold the two together case by case."""
    cid = it.get("id")
    pid = it.get("meeseeks_pid")
    ref = it.get("exec_ref") or {}
    if not pid:
        return "no project linked"
    if ref and (ref.get("kind") != "meeseeks" or ref.get("id") != pid):
        return "exec_ref %r disagrees with meeseeks_pid %r" % (ref, pid)
    own = "card-%d" % int(cid)
    if pid != own:
        return ("linked project %r is not %s, the only project a commission "
                "of card %s creates" % (pid, own, cid))
    index = {r.get("hash"): n for n, r in enumerate(receipts)}
    links = [(n, r) for n, r in enumerate(receipts)
             if r.get("op") == "commission-link"
             and r.get("decision") == "accepted"
             and (r.get("result") or {}).get("id") == cid]
    if not links:
        if any(r.get("op") == "authorize-commission"
               and r.get("decision") == "accepted"
               and (r.get("result") or {}).get("id") == cid for r in receipts):
            return ("card %s has an authorization but no USED commission "
                    "receipt (never linked by commission-link)" % cid)
        return "no commission receipt for card %s" % cid
    why = None
    for n, link in reversed(links):
        res = link.get("result") or {}
        h = res.get("consumes")
        if not _root_gated(link):
            why = "commission-link %s is not a root-gated writer receipt" % (
                link.get("hash", "")[:12])
        elif res.get("project") != pid:
            why = ("the USED commission receipt %s is for project %r; the card "
                   "is linked to %r" % (link.get("hash", "")[:12],
                                        res.get("project"), pid))
        elif not (isinstance(h, str) and len(h) == 64
                  and all(c in "0123456789abcdef" for c in h)):
            why = "malformed commission-link %s: no consume named" % (
                link.get("hash", "")[:12])
        elif h not in index or index[h] >= n:
            why = ("malformed commission-link %s: it spends %s, which is not "
                   "an earlier receipt" % (link.get("hash", "")[:12], h[:12]))
        else:
            a = receipts[index[h]]
            ar = a.get("result") or {}
            spenders = [r for r in receipts if r.get("op") == "commission-link"
                        and r.get("decision") == "accepted"
                        and (r.get("result") or {}).get("consumes") == h]
            if (a.get("op") != "authorize-commission" or not _root_gated(a)
                    or ar.get("id") != cid or ar.get("project") != pid):
                why = ("malformed commission-link %s: %s is not this card's "
                       "root-gated authorization for %s"
                       % (link.get("hash", "")[:12], h[:12], pid))
            elif len(spenders) != 1:
                why = ("REUSE: authorization %s is spent by %d commission-links "
                       "(single use)" % (h[:12], len(spenders)))
            elif str(a.get("ts") or "") > str(link.get("ts") or ""):
                why = ("malformed commission-link %s: dated %s, before the "
                       "authorization it spends (%s)"
                       % (link.get("hash", "")[:12], link.get("ts"), a.get("ts")))
            else:
                return None
    return why


def commission_findings(recs, cards):
    """CONTAINMENT T2 rev 4 ruling B: what the receipts say about
    commissioning, against the board. Lines that start ALERT are REUSE
    attempts (a spent authorization presented again, or a re-authorization
    of a commissioned card) and REWINDS (receipts show a commissioned card
    the board says has no project). Read-only; reports, never acts."""
    def mine(op, cid, decision="accepted"):
        return [r for r in recs if r.get("op") == op
                and r.get("decision") == decision
                and (r.get("result") or {}).get("id") == cid]
    out = []
    status = {c.get("id"): c.get("status") for c in cards}
    for r in recs:
        if r.get("op") in ("authorize-commission", "commission-link") \
                and r.get("decision") == "denied" and (
                    "REUSE" in (r.get("detail") or "")
                    or "already commissioned" in (r.get("detail") or "")):
            cid = (r.get("result") or {}).get("id")
            out.append("%s reuse attempt %s %s on card %s by %s: %s"
                       % ("ALERT" if status.get(cid) == "open" or cid is None
                          else "history (card %s)" % status.get(cid, "absent"),
                          r.get("ts"), r.get("op"), cid,
                          (r.get("identity") or {}).get("invoker_user"),
                          r.get("detail")))
    for c in cards:
        cid, rev = c.get("id"), c.get("rev")
        links = mine("commission-link", cid)
        auths = mine("authorize-commission", cid)
        linked = bool(c.get("meeseeks_pid") or c.get("exec_ref"))
        if links and not linked and c.get("status") == "open":
            out.append("ALERT card %s: commissioned (receipt %s, %s) but the "
                       "board shows no project: REWIND; nothing will "
                       "re-commission it" % (cid, links[-1].get("hash", "")[:12],
                                             links[-1].get("ts")))
        elif auths and not linked and c.get("status") == "open":
            last = auths[-1]
            spent = {(r.get("result") or {}).get("consumes") for r in links}
            if last.get("hash") not in spent:
                out.append("card %s rev %s: authorization %s (%s) CONSUMED, "
                           "never completed; it is spent -- re-run "
                           "tb-commission %s to re-authorize"
                           % (cid, (last.get("result") or {}).get("rev"),
                              last.get("hash", "")[:12], last.get("ts"), cid))
        elif linked and c.get("status") == "open":
            # T2 rev 6 ruling 1: the reflector will NOT drive this linkage.
            # ALERT when the card is in a drivable phase (a bypass attempt);
            # otherwise reported (e.g. pre-T2 legacy card 58, blocked).
            why = drive_verdict(c, recs)
            if why:
                out.append("%scard %s rev %s (%s): linked to %s -- NOT DRIVEN "
                           "by the reflector: %s"
                           % ("ALERT " if c.get("phase") == "in_progress" else "",
                              cid, rev, c.get("phase"), c.get("meeseeks_pid")
                              or (c.get("exec_ref") or {}).get("id"), why))
    return out


def writer_state(agent=None, now=None):
    """box.writer. Never a path argument; read-only."""
    import time as _t
    ok, why = _status_permitted(agent, WRITER_OP)
    if not ok:
        return _unverified("writer", why)
    now = _t.time() if now is None else now
    f = {"read_at": _utc(now), "meaning": WRITER_MEANING,
         "commission_claim": COMMISSION_CLAIM,
         "sudoers": "recorded at install only (not readable here; live "
                    "state UNKNOWN)"}
    problems = []
    mf, prob = _manifest_check()
    f.update(mf)
    if prob:
        problems.append(prob)
    try:
        import board
        bp = board.board_path()
        bst = os.stat(bp)
        f["board_last_write"] = _utc(bst.st_mtime)
        f["direct_board_writes"] = (
            "POSSIBLE for uid %s (board.json is writable outside the writer "
            "in T2; those changes are RECORDED)" % _me()
            if os.access(bp, os.W_OK) or os.access(os.path.dirname(bp), os.W_OK)
            else "NOT possible for uid %s" % _me())
        bsha = _sha_of(bp)
        cards = board.load_board()
    except Exception as e:
        problems.append("board could not be read: %s" % (e,))
        bsha, cards = None, []
    recs, prob = _receipts()
    if prob:
        problems.append(prob)
        f["origin"] = "UNKNOWN (%s)" % prob
        f["commissioning"] = ["UNKNOWN (%s): the reflector drives NO linked "
                              "card while its receipts cannot be verified"
                              % prob]
    elif recs is not None:
        f["receipts"] = len(recs)
        com = commission_findings(recs, cards)
        f["commissioning"] = com or ["no commissioning findings"]
        f["commission_alerts"] = sum(1 for x in com if x.startswith("ALERT"))
        acc = [r for r in recs if r.get("decision") == "accepted"]
        last = recs[-1] if recs else None
        if last:
            f["last_receipt"] = "%s %s %s by %s" % (
                last.get("ts"), last.get("op"), last.get("decision"),
                (last.get("identity") or {}).get("invoker_user"))
        with_after = [r for r in acc if r.get("after")]
        if not with_after:
            f["origin"] = ("no accepted writer change yet: every card's "
                           "origin is RECORDED")
        elif bsha is not None and with_after[-1]["after"] == bsha:
            authd = {}
            for r in acc:
                res, idn = r.get("result") or {}, r.get("identity") or {}
                if res.get("id") is not None and _root_gated(r):
                    authd[(res["id"], res.get("rev"))] = r.get("ts")
            rows = ["#%s rev %s: ROOT-GATED writer change, recorded (receipt %s)"
                    % (c.get("id"), c.get("rev"), authd[(c.get("id"), c.get("rev"))])
                    for c in cards if (c.get("id"), c.get("rev")) in authd]
            f["board_vs_receipts"] = ("unchanged since the last writer "
                                      "receipt (%s)" % with_after[-1].get("ts"))
            f["origin"] = ("%d card(s) ROOT-GATED at their current rev; "
                           "every other card RECORDED (none authenticated)"
                           % len(rows))
            f["rows"] = rows
        else:
            f["board_vs_receipts"] = ("written OUTSIDE the writer since the "
                                      "last receipt (%s)"
                                      % with_after[-1].get("ts"))
            f["origin"] = ("every card RECORDED: the board changed outside "
                           "the writer, so no current card is proven by a "
                           "receipt")
    if problems:
        return _unverified("writer", "; ".join(problems), **f)
    return _ok("writer", **f)


# --- dispatcher + rendering -------------------------------------------

# --- read classes 6-12: D2 read-access closure (release prep R3/S1) ----
#
# Seven reads of stores the box agent's own uid could already open but no
# operation exposed (the 2026-09-01 gate: Meeseeks receipts, transcripts,
# notepads, dreams, meetings, plus the PARTIAL classes). One contract,
# box.status's:
#   * granted PER AGENT, deny by default, in boxread_ops.conf
#     (_status_permitted with the op's own name);
#   * the caller names IDENTIFIERS -- a project, an agent, a kind, a file
#     name checked by regex -- never a path. The path is built here from a
#     fixed root, resolved, and its parent must be exactly the expected
#     directory; files open O_NOFOLLOW and must be regular;
#   * .db is never read as a file: the ledger is one fixed, parameterised,
#     read-only query; no SQL crosses the interface;
#   * bounded (TEXT_MAX bytes, ROWS_MAX rows, LINE_MAX chars a line) and
#     every truncation is stated;
#   * every failure is UNVERIFIED with its reason, never an empty OK;
#   * text leaving a store passes _redact (credential-shaped strings).

LEDGER_OP, PROJECT_OP, LOG_OP = "box.ledger", "box.project", "box.log"
SHORTTERM_OP, MEMORY_OP = "box.shortterm", "box.memory"
DREAM_OP, MEETINGS_OP = "box.dream", "box.meetings"
D2_OPS = (LEDGER_OP, PROJECT_OP, LOG_OP, SHORTTERM_OP, MEMORY_OP, DREAM_OP,
          MEETINGS_OP)

MEESEEKS_DATA = os.environ.get("MEESEEKS_ROOT",
                               "%s/shared/meeseeks/data" % _TB_BASE)
LEDGER_PATH = os.path.join(MEESEEKS_DATA, "ledger.db")
PROJECTS_DIR = os.path.join(MEESEEKS_DATA, "projects")
TELEMETRY_DIR = "%s/shared/telemetry" % _TB_BASE
LTM_DIR = "%s/shared/memory/longterm" % _TB_BASE
SUBC_DIR = "%s/shared/memory/subconscious" % _TB_BASE
AGENT_LOG_DIR = "%s/shared/logs" % _TB_BASE      # sun/moon unit output (J2)

TEXT_MAX = 16384          # 4 reads land in one prompt: well under FILE_MAX
ROWS_MAX = 50
LINE_MAX = 400
LEDGER_N, DETAIL_MAX = 20, 300
LOG_N, LOG_MAX, LOG_SCAN = 20, 100, 262144
ST_N, ST_ROWS_MAX, ST_DAYS_MAX = 10, 30, 7
MEM_LT_N, MEM_SC_N, MEM_TEXT = 8, 5, 1200
DREAM_N, DREAM_MAX, DREAM_TEXT = 3, 10, 2048
MEET_N, MEET_MAX = 10, 30

PROJECT_KINDS = ("receipts", "transcripts", "notepad")
PROJECT_SUFFIX = (".txt", ".md", ".py", ".json")
LOG_FILES = {"consolidation": (LTM_DIR, "consolidation_decisions.log"),
             "flags": (SUBC_DIR, "flag_decisions.log"),
             "sun": (AGENT_LOG_DIR, "agent-sun.log"),
             "moon": (AGENT_LOG_DIR, "agent-moon.log")}

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}$")   # no dotfiles
_NOTE_RE = re.compile(r"^(s[0-9]{2})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,95})$")
_SECTION_RE = re.compile(r"^s[0-9]{2}$")
_DATE_RE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_WEEK_RE = re.compile(r"^20[0-9]{2}-W[0-5][0-9]$")
_THREAD_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_MEET_RE = re.compile(r"^(20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z)-"
                      r"([a-z][a-z0-9_]*(?:-[a-z][a-z0-9_]*)*)\.txt$")

# Credential-shaped strings, the publication gate's G2 classes. A COPY, not
# an import: publish_export.py is publication machinery and does not ship.
# The selftest asserts the PATTERNS are identical wherever both exist;
# labels here are neutral (no vendor names in shipped source).
_REDACT = [
    ("provider key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{20,}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("resend key", re.compile(r"\bre_[A-Za-z0-9]{24,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned credential literal",
     re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b\s*[=:]\s*['\"][^'\"\s]{12,}['\"]")),
]


def _redact(text):
    """(text, n): every credential-shaped span replaced by its class."""
    n = 0
    for name, rx in _REDACT:
        text, k = rx.subn("[REDACTED:%s]" % name, text)
        n += k
    return text, n


def _one_line(s, cap):
    s = " | ".join(x.strip() for x in str(s).splitlines() if x.strip())
    if len(s) > cap:
        s = s[:cap] + " [+%d chars]" % (len(s) - cap)
    return s


def _int_arg(v, default, cap, what):
    """(n, None) or (None, reason). Clamped to cap; never below 1."""
    if v is None:
        return default, None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, "%s is not a number: %r" % (what, v)
    if n < 1:
        return None, "%s must be at least 1, got %d" % (what, n)
    return min(n, cap), None


def _store(path):
    """(resolved dir, None) or (None, reason)."""
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        return None, "store missing: %s" % path
    if not os.access(real, os.R_OK | os.X_OK):
        return None, "store not readable by this process: %s" % path
    return real, None


def _read_in(parent, name, max_bytes, offset=0, tail=False):
    """(raw, size, None) or (None, None, reason). `parent` is a RESOLVED
    directory; `name` must resolve to exactly parent/name, open O_NOFOLLOW,
    and be a regular file. tail=True reads the LAST max_bytes."""
    real = os.path.realpath(os.path.join(parent, name))
    if os.path.dirname(real) != parent:
        return None, None, "resolves outside its store"
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None, "no such file: %s" % name
    except PermissionError:
        return None, None, "permission denied reading %s" % name
    except OSError as e:
        return None, None, "%s could not be opened: %s" % (name, e.strerror or e)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None, None, "%s is not a regular file" % name
        with os.fdopen(fd, "rb") as fh:
            if tail:
                offset = max(0, st.st_size - max_bytes)
            fh.seek(offset)
            raw = fh.read(max_bytes)
    except OSError as e:
        return None, None, "%s could not be read: %s" % (name, e.strerror or e)
    return raw, st.st_size, None


def _granted(agent, op, kind):
    ok, why = _status_permitted(agent, op)
    return None if ok else _unverified(kind, why)


def _project_kind_dir(pid, kind):
    """(resolved dir, None) or (None, reason)."""
    if not _PROJECT_RE.match(pid or ""):
        return None, "not a project name: %r" % (pid,)
    root, why = _store(PROJECTS_DIR)
    if why:
        return None, "Meeseeks project " + why
    pdir = os.path.join(root, pid)
    if os.path.realpath(pdir) != pdir or not os.path.isdir(pdir):
        return None, "no such project: %s" % pid
    if kind is None:
        return pdir, None
    kdir = os.path.join(pdir, kind)
    if os.path.realpath(kdir) != kdir or not os.path.isdir(kdir):
        return None, "project %s has no %s" % (pid, kind)
    return kdir, None


def ledger(args, agent=None):
    """box.ledger <project> [rows]: newest events, fixed read-only query."""
    g = _granted(agent, LEDGER_OP, "ledger")
    if g:
        return g
    parts = _split(args, 2)
    if not parts or not parts[0]:
        return _unverified("ledger", "expected: <project> [rows], got %r" % (args,))
    pid = parts[0]
    n, why = _int_arg(parts[1], LEDGER_N, ROWS_MAX, "rows")
    if why:
        return _unverified("ledger", why, project=pid)
    _d, why = _project_kind_dir(pid, None)
    if why:
        return _unverified("ledger", why)
    if not os.path.isfile(LEDGER_PATH):
        return _unverified("ledger", "Meeseeks ledger missing", project=pid)
    import sqlite3
    try:
        con = sqlite3.connect("file:%s?mode=ro" % LEDGER_PATH, uri=True,
                              timeout=2)
        try:
            cols = set(r[1] for r in con.execute("PRAGMA table_info(events)"))
            if not {"ts", "pid", "ref", "kind", "detail"} <= cols:
                return _unverified("ledger", "ledger has no events table of "
                                   "the expected shape", project=pid)
            top = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM events"
                              ).fetchone()[0]
            got = con.execute("SELECT ts, ref, kind, detail FROM events "
                              "WHERE pid = ? ORDER BY rowid DESC LIMIT ?",
                              (pid, n)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return _unverified("ledger", "ledger could not be read: %s" % (e,),
                           project=pid)
    rows, red = [], 0
    for ts, ref, kind, detail in got:
        d, k = _redact(_one_line(detail or "", DETAIL_MAX))
        red += k
        rows.append("%s %s %s %s" % (ts, ref, kind, d))
    return _ok("ledger", project=pid, requested=n, returned=len(rows),
               ledger_max_rowid=top, redacted=red,
               note=("newest first; the newest %d events of this project "
                     "only" % n), rows=rows,
               source="meeseeks ledger events table (read-only query)")


def project(args, agent=None):
    """box.project <project> <kind> [file] [page]: list, or one file."""
    g = _granted(agent, PROJECT_OP, "project")
    if g:
        return g
    parts = (_clean(args) or "").split()
    if not 2 <= len(parts) <= 4:
        return _unverified("project", "expected: <project> <receipts|"
                           "transcripts|notepad> [file] [page], got %r" % (args,))
    pid, kind = parts[0], parts[1]
    if kind not in PROJECT_KINDS:
        return _unverified("project", "kind must be one of %s, got %r"
                           % ("|".join(PROJECT_KINDS), kind))
    kdir, why = _project_kind_dir(pid, kind)
    if why:
        return _unverified("project", why)
    if len(parts) == 2:
        found = []
        dirs = [(kdir, "")]
        if kind == "notepad":
            dirs = []
            for s in sorted(os.listdir(kdir)):
                sd = os.path.join(kdir, s)
                if _SECTION_RE.match(s) and not os.path.islink(sd) \
                        and os.path.isdir(sd):
                    dirs.append((sd, s + "/"))
        for d, pre in dirs:
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if not _FILE_RE.match(f) or os.path.islink(p) \
                        or not os.path.isfile(p) \
                        or not f.lower().endswith(PROJECT_SUFFIX):
                    continue
                st = os.stat(p)
                found.append((st.st_mtime, pre + f, st.st_size))
        found.sort(reverse=True)
        rows = ["%s  %d bytes  %s" % (name, size, _utc(mt))
                for mt, name, size in found[:ROWS_MAX]]
        return _ok("project", project=pid, project_kind=kind,
                   files=len(found), listed=len(rows),
                   note="newest first; read one: ARGS %s %s <name> [page]"
                        % (pid, kind), rows=rows,
                   source="meeseeks project store")
    name = parts[2]
    page, why = _int_arg(parts[3] if len(parts) == 4 else None, 1, 10 ** 6,
                         "page")
    if why:
        return _unverified("project", why, project=pid)
    if kind == "notepad":
        mo = _NOTE_RE.match(name)
        if not mo:
            return _unverified("project", "notepad file must be named "
                               "sNN/<file>, got %r" % (name,))
        sec, fname = mo.group(1), mo.group(2)
        parent = os.path.join(kdir, sec)
        if os.path.realpath(parent) != parent or not os.path.isdir(parent):
            return _unverified("project", "no notepad section %s" % sec)
    else:
        if not _FILE_RE.match(name):
            return _unverified("project", "not a file name: %r" % (name,))
        parent, fname = kdir, name
    low = fname.lower()
    if any(s in low for s in DENY_SUBSTR) or low.endswith(DENY_SUFFIX) \
            or ".bak-" in low:
        return _unverified("project", "denied: restricted file", file=name)
    if not low.endswith(PROJECT_SUFFIX):
        return _unverified("project", "not a readable project file type: %r"
                           % (name,))
    raw, size, why = _read_in(parent, fname, TEXT_MAX,
                              offset=(page - 1) * TEXT_MAX)
    if why:
        return _unverified("project", why, project=pid, file=name)
    pages = max(1, -(-size // TEXT_MAX))
    if page > pages:
        return _unverified("project", "page %d does not exist (%d page(s))"
                           % (page, pages), project=pid, file=name)
    if b"\x00" in raw:
        return _unverified("project", "binary content, not readable as text",
                           project=pid, file=name)
    text, red = _redact(raw.decode("utf-8", "replace"))
    return _ok("project", project=pid, project_kind=kind, file=name,
               size=size, page="%d of %d" % (page, pages),
               truncated=pages > 1, redacted=red, text=text,
               source="meeseeks project store")


def log(args, agent=None, now=None):
    """box.log <cli[:YYYY-MM-DD]|consolidation|flags|sun|moon> [lines]."""
    g = _granted(agent, LOG_OP, "log")
    if g:
        return g
    parts = _split(args, 2)
    if not parts or not parts[0]:
        return _unverified("log", "expected: <log> [lines], got %r" % (args,))
    name = parts[0]
    if name == "cli" or name.startswith("cli:"):
        import time as _time
        date = name[4:] if name.startswith("cli:") else _time.strftime(
            "%Y-%m-%d", _time.gmtime(now if now is not None else _time.time()))
        if not _DATE_RE.match(date):
            return _unverified("log", "cli date must be YYYY-MM-DD, got %r"
                               % (date,))
        parent, fname = TELEMETRY_DIR, "cli-%s.log" % date
    elif name in LOG_FILES:
        parent, fname = LOG_FILES[name]
    else:
        return _unverified("log", "unknown log %r; one of: cli, "
                           "cli:YYYY-MM-DD, %s" % (name, ", ".join(LOG_FILES)))
    n, why = _int_arg(parts[1], LOG_N, LOG_MAX, "lines")
    if why:
        return _unverified("log", why, log=name)
    store, why = _store(parent)
    if why:
        return _unverified("log", why, log=name)
    raw, size, why = _read_in(store, fname, LOG_SCAN, tail=True)
    if why:
        return _unverified("log", why, log=name)
    if b"\x00" in raw:
        return _unverified("log", "binary content, not readable as text",
                           log=name)
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > LOG_SCAN and lines:
        lines = lines[1:]                 # first line is a partial line
    lines = [l for l in lines if l.strip()][-n:]
    rows, red = [], 0
    for l in lines:
        t, k = _redact(_one_line(l, LINE_MAX))
        red += k
        rows.append(t)
    return _ok("log", log=name, file=fname, requested=n, returned=len(rows),
               window_filled=len(rows) >= n, redacted=red,
               note=("the last %d non-empty lines only (scan window %d "
                     "bytes); older lines exist beyond it" % (n, LOG_SCAN)),
               rows=rows, source="%s/%s" % (parent, fname))


def shortterm_read(args, agent=None):
    """box.shortterm <agent|all|thread:ID> [rows] [days]."""
    g = _granted(agent, SHORTTERM_OP, "shortterm")
    if g:
        return g
    parts = _split(args, 3)
    if not parts or not parts[0]:
        return _unverified("shortterm", "expected: <agent|all|thread:ID> "
                           "[rows] [days], got %r" % (args,))
    who = parts[0]
    n, why = _int_arg(parts[1], ST_N, ST_ROWS_MAX, "rows")
    if not why:
        days, why = _int_arg(parts[2], 1, ST_DAYS_MAX, "days")
    if why:
        return _unverified("shortterm", why, who=who)
    try:
        import shortterm as _st
        import agents as _ag
    except Exception as e:
        return _unverified("shortterm", "short-term library unavailable: %s"
                           % (e,))
    store, why = _store(_st.STORE_DIR)
    if why:
        return _unverified("shortterm", "short-term " + why, who=who)
    import glob as _glob
    files = sorted(_glob.glob(os.path.join(store, "*.jsonl")))
    if not files:
        return _unverified("shortterm", "short-term store has no day files")
    if not os.access(files[-1], os.R_OK):
        return _unverified("shortterm", "newest day file is not readable: %s"
                           % os.path.basename(files[-1]))
    # The library returns [] on ANY failure; the checks above make an empty
    # result here mean "no rows in the window", never "could not read".
    if who.startswith("thread:"):
        tid = who[len("thread:"):]
        if not _THREAD_RE.match(tid):
            return _unverified("shortterm", "not a thread id: %r" % (tid,))
        got = _st.recent_thread(tid, n=n, days=days)
    elif who == "all":
        got = _st.recent(None, n=n, days=days)
    else:
        if not _AGENT_NAME_RE.match(who):
            return _unverified("shortterm", "not an agent name: %r" % (who,))
        try:
            known = _ag.existing_agents()
        except Exception as e:
            return _unverified("shortterm", "agent roster unavailable: %s"
                               % (e,))
        if who not in known:
            return _unverified("shortterm", "no such agent: %s" % who)
        got = _st.recent(who, n=n, days=days)
    rows, red = [], 0
    for r in got:
        t = ("%s %s thread=%s source=%s origin=%s | prompt: %s | response: %s"
             % (r.get("ts"), r.get("agent"), r.get("thread_id"),
                r.get("source"), r.get("origin"),
                _one_line(r.get("prompt") or "", LINE_MAX),
                _one_line(r.get("response") or "", LINE_MAX)))
        t, k = _redact(t)
        red += k
        rows.append(t)
    return _ok("shortterm", who=who, requested=n, days=days,
               returned=len(rows), redacted=red,
               note=("oldest first; the last %d exchange(s) across the newest "
                     "%d day file(s) only" % (n, days)), rows=rows,
               source="short-term store (day files)")


def memory(args, agent=None):
    """box.memory <longterm|subconscious> <agent> <query words>."""
    g = _granted(agent, MEMORY_OP, "memory")
    if g:
        return g
    a = _clean(args)
    parts = a.split(None, 2) if a else []
    if len(parts) < 3:
        return _unverified("memory", "expected: <longterm|subconscious> "
                           "<agent> <query words>, got %r" % (args,))
    store, target, query = parts
    if store not in ("longterm", "subconscious"):
        return _unverified("memory", "store must be longterm or subconscious, "
                           "got %r" % (store,))
    if not _AGENT_NAME_RE.match(target):
        return _unverified("memory", "not an agent name: %r" % (target,))
    caller = (_clean(agent) or "").lower()
    rows, red = [], 0
    try:
        if store == "longterm":
            import longterm as _lt
            got = _lt.query_memories(target, caller, query, n_results=MEM_LT_N)
            for m in got:
                t, k = _redact("[%s] (%s) %s" % (m.consolidated_at, m.provenance,
                                                 _one_line(m.text, MEM_TEXT)))
                red += k
                rows.append(t)
            meaning = "consolidated long-term memory (durable lessons)"
        else:
            import subconscious as _sc
            got = _sc.read_fragments(target, caller=caller, query=query,
                                     n=MEM_SC_N)
            for f in got:
                t, k = _redact("[%s] salience=%s %s" % (
                    f.ts_utc, f.salience, _one_line(f.text, MEM_TEXT)))
                red += k
                rows.append(t)
            meaning = ("RAW, unconsolidated subconscious fragments -- "
                       "charge intact, not durable memory")
    except PermissionError as e:
        return _unverified("memory", "the store's scope rule refused this "
                           "read: %s" % (e,), store=store, target=target)
    except Exception as e:
        return _unverified("memory", "%s store unavailable: %s: %s"
                           % (store, type(e).__name__, e), store=store,
                           target=target)
    return _ok("memory", store=store, target=target, query=query[:120],
               returned=len(rows), meaning=meaning, redacted=red,
               note="nearest first; similarity search, not a complete list",
               rows=rows, source="%s store (semantic query)" % store)


def dream(args, agent=None):
    """box.dream <agent> [entries] [week]: dream journal entries."""
    g = _granted(agent, DREAM_OP, "dream")
    if g:
        return g
    parts = _split(args, 3)
    if not parts or not parts[0]:
        return _unverified("dream", "expected: <agent> [entries] [week], "
                           "got %r" % (args,))
    target = parts[0]
    if not _AGENT_NAME_RE.match(target):
        return _unverified("dream", "not an agent name: %r" % (target,))
    n, why = _int_arg(parts[1], DREAM_N, DREAM_MAX, "entries")
    if why:
        return _unverified("dream", why, target=target)
    week = parts[2]
    if week is not None and not _WEEK_RE.match(week):
        return _unverified("dream", "week must look like 2026-W40, got %r"
                           % (week,))
    store, why = _store(DREAMS_DIR)
    if why:
        return _unverified("dream", "dream " + why, target=target)
    fname = ("%s.jsonl" % target if week is None
             else "%s.jsonl.consolidated-%s" % (target, week))
    raw, size, why = _read_in(store, fname, LOG_SCAN, tail=True)
    if why:
        return _unverified("dream", why, target=target)
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > LOG_SCAN and lines:
        lines = lines[1:]
    entries, bad = [], 0
    for l in lines:
        if not l.strip():
            continue
        try:
            obj = json.loads(l)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError:
            bad += 1
            continue
        entries.append(obj)
    rows, red = [], 0
    for e in reversed(entries[-n:]):
        t, k = _redact("%s tier=%s model=%s fragments=%s | %s" % (
            e.get("ts_utc"), e.get("tier"), e.get("model"),
            e.get("fragments_consolidated"),
            _one_line(e.get("dream") or "", DREAM_TEXT)))
        red += k
        rows.append(t)
    return _ok("dream", target=target, file=fname, requested=n,
               returned=len(rows), entries_in_window=len(entries),
               unparsable_lines=bad, redacted=red,
               note="newest first; unparsable lines are counted, not shown",
               rows=rows, source="dream journal")


def meetings(args, agent=None):
    """box.meetings [participant] [count]: transcript listing."""
    g = _granted(agent, MEETINGS_OP, "meetings")
    if g:
        return g
    parts = _split(args, 2)
    if parts is None:
        return _unverified("meetings", "expected: [participant] [count], "
                           "got %r" % (args,))
    who, cnt = parts
    if who is not None and who.isdigit() and cnt is None:
        who, cnt = None, who
    if who is not None and not _AGENT_NAME_RE.match(who):
        return _unverified("meetings", "not a participant name: %r" % (who,))
    n, why = _int_arg(cnt, MEET_N, MEET_MAX, "count")
    if why:
        return _unverified("meetings", why)
    store, why = _store(MEETINGS_DIR)
    if why:
        return _unverified("meetings", "meeting " + why)
    found = []
    for f in os.listdir(store):
        mo = _MEET_RE.match(f)
        p = os.path.join(store, f)
        if not mo or os.path.islink(p) or not os.path.isfile(p):
            continue
        people = mo.group(2).split("-")
        if who is not None and who not in people:
            continue
        found.append((mo.group(1), f, people, os.path.getsize(p)))
    found.sort(reverse=True)
    rows = ["%s  participants=%s  %d bytes  %s" % (stamp, ",".join(pp), size,
                                                   os.path.join(MEETINGS_DIR, f))
            for stamp, f, pp, size in found[:n]]
    return _ok("meetings", participant=who, total=len(found),
               listed=len(rows),
               note="newest first; read one with box.file on its path",
               rows=rows, source="meeting transcripts")


def _split(args, n):
    parts = (_clean(args) or "").split()
    return parts + [None] * (n - len(parts)) if len(parts) <= n else None


def read(op, args="", agent=None):
    """Map a read operation to its function. An unknown or malformed
    operation is UNVERIFIED -- never guessed at, never partially run.
    `agent` is the caller's identity; box.status is granted per agent."""
    o = (_clean(op) or "").lower()
    if o == "box.cards":
        return cards_active()
    if o == "box.units":
        return units()
    if o == "box.card":
        return card_state(args)
    if o == "box.service":
        return service_status(args)
    if o == "box.file":
        return read_file(args)
    if o == "box.status":
        return status_card(args, agent)
    if o == WRITER_OP:
        return writer_state(agent)
    if o == "box.journal":
        parts = _split(args, 2)
        if not parts or not parts[0]:
            return _unverified("journal", "expected: <unit> [lines], got %r"
                               % (args,))
        return journal(parts[0], parts[1])
    if o == LEDGER_OP:
        return ledger(args, agent)
    if o == PROJECT_OP:
        return project(args, agent)
    if o == LOG_OP:
        return log(args, agent)
    if o == SHORTTERM_OP:
        return shortterm_read(args, agent)
    if o == MEMORY_OP:
        return memory(args, agent)
    if o == DREAM_OP:
        return dream(args, agent)
    if o == MEETINGS_OP:
        return meetings(args, agent)
    return _unverified("read", "no such read operation: %r" % (op,))


def render(d):
    """Text form of any result. UNVERIFIED renders AS UNVERIFIED with its
    reason; it never degrades into prose that could be read as data."""
    if not isinstance(d, dict) or "status" not in d:
        return "UNVERIFIED: result was not a structured read"
    if d.get("status") != "OK":
        head = "UNVERIFIED (%s): %s" % (d.get("kind"), d.get("reason"))
        rec = d.get("recorded")
        if rec is None:
            return head
        # BOARD-TRUTH S1: a withheld board read keeps its content, labelled
        # so that no line of it can be read as current state.
        lines = [head]
        for k, v in sorted(d.items()):
            if k in ("status", "kind", "reason", "recorded") or v is None:
                continue
            lines.append("  %s: %s" % (k, v))
        lines.append("  AS LAST RECORDED -- NOT CURRENT STATE:")
        if isinstance(rec, dict):
            for k, v in sorted(rec.items()):
                if v is not None:
                    lines.append("    %s: %s" % (k, v))
        else:
            for row in rec:
                lines.append("  - %s" % (row,))
        return "\n".join(lines)
    lines = ["OK (%s)" % d.get("kind")]
    for k, v in sorted(d.items()):
        if k in ("status", "kind", "rows", "text") or v is None:
            continue
        lines.append("  %s: %s" % (k, v))
    for row in d.get("rows") or []:
        lines.append("  - %s" % (row,))
    if d.get("text") is not None:
        lines.append("---")
        lines.append(d["text"])
    return "\n".join(lines)


def _selftest():
    if not __debug__:
        print("boxread selftest REFUSES to run under -O: assertions are "
              "stripped, so a pass would prove nothing")
        raise SystemExit(2)

    # --- denied paths stay denied, by location and by name -------------
    for bad in ("/etc/passwd", "/tmp/x.py", "/root/a.txt", "relative.py",
                "/etc/treebeard/credentials/treebeard.env",
                "/etc/treebeard/credentials/providers.env",
                "/etc/treebeard/personality/x.toml",
                "/opt/treebeard/shared/ui/operator_token",
                "/opt/treebeard/shared/lib/../../../etc/shadow",
                "/opt/treebeard/shared/lib/subdir/deep.py",
                "/opt/treebeard/shared/meeseeks/data/ledger.db",
                "/etc/systemd/system/ssh.service"):
        r = read_file(bad)
        assert r["status"] == "UNVERIFIED", (bad, r)
    # A .bak of an allowed file is still not readable.
    assert read_file("/opt/treebeard/shared/lib/board.py.bak-x"
                     )["status"] == "UNVERIFIED"

    # --- an allowed file reads, and states its own truncation ----------
    me = os.path.realpath(__file__)
    if os.path.dirname(me) in ALLOW_DIRS:
        r = read_file(me)
        assert r["status"] == "OK" and "boxread" in r["text"], r
        r2 = read_file(me, max_bytes=100)
        assert r2["truncated"] and "TRUNCATED" in r2["text"], r2
        assert read_file(me, max_bytes="huge")["status"] == "UNVERIFIED"

    # --- malformed input fails safe, never reaches a command -----------
    for bad in (None, "", "   ", 12, "a" * 600, "unit\nname",
                "bad; rm -rf /", "ssh.service", "sshd", "../x"):
        assert service_status(bad)["status"] == "UNVERIFIED", bad
        assert journal(bad)["status"] == "UNVERIFIED", bad
    assert read_file(None)["status"] == "UNVERIFIED"
    assert card_state("not-a-number")["status"] == "UNVERIFIED"
    assert card_state(999999)["status"] == "UNVERIFIED"
    assert journal("treebeard-will.service", n="lots")["status"] \
        == "UNVERIFIED"

    # --- unknown operations are refused, not approximated --------------
    for bad in ("box.exec", "board.done", "", None, "box.file/../"):
        assert read(bad, "x")["status"] == "UNVERIFIED", bad
    assert read("box.journal", "")["status"] == "UNVERIFIED"
    # Case and whitespace are normalised, not a second grammar.
    assert read(" BOX.CARDS ")["kind"] == "cards"

    # --- a failed command is UNVERIFIED, never data --------------------
    ok, reason = _run(["/bin/false"])
    assert ok is False and "rc=1" in reason, reason
    ok, reason = _run(["/nonexistent-binary-xyz"])
    assert ok is False, reason

    # --- unknown units are absent, not quietly empty -------------------
    assert service_status("treebeard-no-such-unit.service"
                          )["status"] == "UNVERIFIED"
    assert journal("treebeard-no-such-unit.service"
                   )["status"] == "UNVERIFIED"

    # --- journal bounds cannot be raised by asking for more ------------
    j = journal("treebeard-will.service", n=10 ** 6)
    if j["status"] == "OK":
        assert j["requested"] == JOURNAL_MAX, j
        assert j["access"] == "full" and "window" in j["note"]
        assert len(j["rows"]) <= JOURNAL_MAX

    # --- a unit owned by another user is UNVERIFIED, not empty ---------
    js = journal("treebeard-agent@sun.service")
    if js["status"] == "UNVERIFIED" and js.get("unit_user"):
        assert js["unit_user"] != _me()
        assert "NOT evidence" in js["reason"]

    # --- UNVERIFIED never renders as data ------------------------------
    assert render(_unverified("file", "denied")).startswith("UNVERIFIED")
    assert render({"nonsense": 1}).startswith("UNVERIFIED")
    assert render("string").startswith("UNVERIFIED")

    # --- live reads, when the box is reachable from here ---------------
    c = cards_active()
    assert c["status"] in ("OK", "UNVERIFIED")
    u = units()
    if u["status"] == "OK":
        assert any(r.get("unit", "").endswith(".timer") for r in u["rows"]), \
            "timers must be enumerated, not only services"
        assert any(r.get("unit", "").endswith(".service") for r in u["rows"])
    # No operation name and its argument may share a line: that shape
    # is what produced the malformed block in production.
    for _l in OPS_HELP.splitlines():
        _s = _l.strip()
        if _s.startswith("box."):
            assert "<" not in _s and "[" not in _s, _s
    assert "ARGS:" in OPS_HELP

    # --- box.service says what its result is NOT ----------------------
    _real_show = _show
    try:
        globals()["_show"] = lambda u: ({"Id": u, "ActiveState": "inactive",
                                         "Result": "success"}, None)
        sv = service_status("treebeard-career.service")
    finally:
        globals()["_show"] = _real_show
    assert sv["status"] == "OK", sv
    assert "  meaning: unit exit status, not job outcome" in render(sv), sv
    assert "box.status" in OPS_HELP

    # --- box.status: fixtures through TREEBEARD_BASE, in a child -------
    _status_fixture_parent()
    # --- box.writer: fixtures through TREEBEARD_BASE, in a child -------
    assert "box.writer" in OPS_HELP
    _writer_fixture_parent()

    # --- D2 reads (release prep R3/S1) --------------------------------
    # Every op read() dispatches is documented, each documented op has its
    # ARGS on its own line before the next op (the 2026-09-01 lesson), and
    # no op line carries an argument (the guard above, kept).
    _lines = OPS_HELP.splitlines()
    _ops = [l.split()[0] for l in _lines if l.startswith("box.")]
    for _op in D2_OPS:
        assert _op in _ops, "%s missing from OPS_HELP" % _op
    for _i, _l in enumerate(_lines):
        if _l.startswith("box."):
            _nxt = [x for x in _lines[_i + 1:]]
            _end = next((j for j, x in enumerate(_nxt) if x.startswith("box.")),
                        len(_nxt))
            _args = [x for x in _nxt[:_end] if x.strip().startswith("ARGS:")]
            assert len(_args) == 1, "%s needs exactly one ARGS line" % _l.split()[0]
    for _op in D2_OPS:
        assert read(_op, "", agent=None)["status"] == "UNVERIFIED", _op
    # the redaction list is the publication gate's G2 list, where both exist
    try:
        import publish_export as _pe
    except Exception:
        _pe = None
    if _pe is not None:
        assert [r.pattern for _n, r in _REDACT] == \
            [r.pattern for _n, r in _pe.SECRET_PATTERNS], \
            "boxread _REDACT drifted from publish_export.SECRET_PATTERNS"
    _read_fixture_parent()

    print("boxread selftest: ALL PASS")


# --- box.status fixtures ------------------------------------------------
# The parent builds a throwaway TREEBEARD_BASE and a throwaway grant file,
# then re-runs THIS module against them in a child, so STATUS_DIR is
# derived exactly as in production -- from TREEBEARD_BASE -- and nothing
# real (store, policy) is read. Fixed clock: every age is exact.

_FX_NOW = 1790870400                     # 2026-10-01T16:00:00Z
_FX_SECRET = "SECRET-OUTSIDE-THE-STORE"


def _fx_card(age_s, status="ok", win=26, **over):
    import datetime
    ep = _FX_NOW - age_s
    c = {"agent": "x", "status": status,
         "summary": "career hunter: SUCCEEDED (worthwhile=2 packets=2)",
         "detail": {"counts": {"kept": 3}}, "stale_after_hours": win,
         "updated_utc": datetime.datetime.fromtimestamp(
             ep, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "updated_epoch": ep}
    c.update(over)
    return json.dumps(c)


def _status_fixture_parent():
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="boxread-status-")
    try:
        store = os.path.join(base, "shared", "status")
        os.makedirs(store)
        os.makedirs(os.path.join(base, "outside"))
        secret = _fx_card(60, summary=_FX_SECRET)
        for p in (os.path.join(base, "outside", "secret.json"),
                  os.path.join(base, "shared", "secret.json")):
            with open(p, "w") as fh:
                fh.write(secret)
        cards = {
            "fresh": _fx_card(3600, run_id="r53bc1f99313"),
            "norun": _fx_card(3600),
            "edge": _fx_card(26 * 3600),
            "stale": _fx_card(26 * 3600 + 1),
            "weekly": _fx_card(100 * 3600, status="warn", win=192),
            "future": _fx_card(-3600),
            "skew": _fx_card(-60),
            "malformed": "{not json",
            "empty": "",
            "array": "[1, 2]",
            "nowindow": _fx_card(60, stale_after_hours=None),
            "zerowindow": _fx_card(60, stale_after_hours=0),
            "noepoch": _fx_card(60, updated_epoch=None),
            "boolepoch": _fx_card(60, updated_epoch=True),
            "badstatus": _fx_card(60, status="great"),
            "nostatus": _fx_card(60, status=None),
            "utcmismatch": _fx_card(60, updated_utc="2026-01-01T00:00:00Z"),
            "badrun": _fx_card(60, run_id="../../etc"),
            "longsum": _fx_card(60, summary="x\n" * 400),
            "unreadable": _fx_card(60),
            "bad-name": _fx_card(60),
        }
        for k, v in cards.items():
            with open(os.path.join(store, k + ".json"), "w") as fh:
                fh.write(v)
        with open(os.path.join(store, "huge.json"), "w") as fh:
            fh.write(" " * (STATUS_MAX + 1))
        os.chmod(os.path.join(store, "unreadable.json"), 0)
        os.symlink(os.path.join(base, "outside", "secret.json"),
                   os.path.join(store, "leak.json"))
        os.symlink("../secret.json", os.path.join(store, "uplink.json"))
        os.symlink("fresh.json", os.path.join(store, "inlink.json"))
        os.mkdir(os.path.join(store, "adir.json"))
        os.mkfifo(os.path.join(store, "fifo.json"))
        pol = os.path.join(base, "boxread_ops.conf")
        with open(pol, "w") as fh:
            fh.write("# fixture grant\ntreebeard -> box.status\n"
                     "clark -> board.list\n")
        # BOARD-TRUTH S1: a fixture board (the child sets its age). The
        # path is pinned explicitly so an inherited TASKBOARD_PATH can
        # never point the child at a real board.
        bdir = os.path.join(base, "shared", "board")
        os.makedirs(bdir)
        with open(os.path.join(bdir, "board.json"), "w") as fh:
            json.dump([{"id": 7, "kind": "task", "text": "FIXTURE-OPEN-CARD",
                        "status": "open", "phase": None, "owner": None,
                        "source": "operator", "created":
                        "2026-08-01T00:00:00+00:00"},
                       {"id": 8, "kind": "task", "text": "FIXTURE-DONE-CARD",
                        "status": "done", "phase": None, "owner": None,
                        "source": "operator", "created":
                        "2026-08-01T00:00:00+00:00"}], fh)
        env = dict(os.environ, TREEBEARD_BASE=base, BOXREAD_OPS_CONF=pol,
                   TASKBOARD_PATH=os.path.join(bdir, "board.json"))
        p = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--status-fixtures"], env=env, text=True,
                           capture_output=True, timeout=60)
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        assert p.returncode == 0 and "STATUS FIXTURES: ALL PASS" in p.stdout, \
            "box.status fixture child failed rc=%d" % p.returncode
    finally:
        try:
            os.chmod(os.path.join(base, "shared", "status",
                                  "unreadable.json"), 0o600)
        except OSError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def _status_fixture_child():
    if not __debug__:
        print("boxread status fixtures: refusing to run under -O "
              "(asserts disabled)")
        raise SystemExit(2)
    base = os.environ["TREEBEARD_BASE"]
    store = os.path.realpath(os.path.join(base, "shared", "status"))
    assert os.path.realpath(STATUS_DIR) == store, (STATUS_DIR, store)
    T, N = "treebeard", _FX_NOW

    def st(card, agent=T):
        return status_card(card, agent, now=N)

    # --- fresh: every normalized field, from the card, nothing else ----
    r = st("fresh")
    assert r["status"] == "OK", r
    assert r["card"] == "fresh" and r["state"] == "ok", r
    assert r["run_id"] == "r53bc1f99313", r
    assert r["updated_utc"] == "2026-10-01T15:00:00Z", r
    assert r["age"] == "1h 0m (3600s)", r
    assert r["freshness"].startswith("FRESH"), r
    assert r["source"] == "statusboard %s/fresh.json" % STATUS_DIR, r
    assert "detail" not in r and "counts" not in render(r), r
    assert st("norun")["run_id"] == "not recorded on card"
    assert st("badrun")["run_id"] == "not recorded on card"
    assert st("edge")["status"] == "OK"            # age == window: fresh
    w = st("weekly")                                 # its OWN window
    assert w["status"] == "OK" and w["state"] == "warn", w
    assert st("skew")["status"] == "OK"            # small clock skew
    ls = st("longsum")
    assert ls["status"] == "OK" and "\n" not in ls["summary"], ls
    assert ls["summary"].endswith("[TRUNCATED]"), ls
    assert len(ls["summary"]) <= SUMMARY_MAX + 12, ls
    assert st("inlink")["status"] == "OK"          # symlink inside store

    # --- stale: UNVERIFIED, provenance kept, STATE WITHHELD ------------
    s = st("stale")
    assert s["status"] == "UNVERIFIED", s
    assert s["reason"].startswith("STALE:") and "26h" in s["reason"], s
    assert "2026-09-30T13:59:59Z" in s["reason"], s
    assert "state" not in s and "summary" not in s, s
    txt = render(s)
    assert txt.startswith("UNVERIFIED (status): STALE:"), txt
    assert "SUCCEEDED" not in txt and "ok" not in txt.split("window")[-1], txt
    # the same card IS fresh one second earlier: the window is the card's
    assert status_card("stale", T, now=N - 1)["status"] == "OK"

    # --- every incomplete card is UNVERIFIED with a stated reason ------
    expect = {
        "nosuch": "no such card",
        "future": "in the future",
        "malformed": "not valid JSON",
        "empty": "card is empty",
        "array": "not a JSON object",
        "nowindow": "no freshness window",
        "zerowindow": "no freshness window",
        "noepoch": "no updated_epoch",
        "boolepoch": "no updated_epoch",
        "badstatus": "no valid status",
        "nostatus": "no valid status",
        "utcmismatch": "does not match updated_epoch",
        "huge": "larger than",
        "adir": "not a regular file",
        "fifo": "not a regular file",
    }
    if os.geteuid() != 0:                       # root reads mode 000 anyway
        expect["unreadable"] = "permission denied"
    for cid, why in expect.items():
        r = st(cid)
        assert r["status"] == "UNVERIFIED", (cid, r)
        assert why in r.get("reason", ""), (cid, r)
        out = render(r)
        assert out.startswith("UNVERIFIED (status): ") and why in out, out
        assert "state" not in r and "summary" not in r, (cid, r)

    # --- confinement: no id is a path; nothing outside the store opens --
    opened = []
    real_open = os.open

    def spy(path, *a, **k):
        opened.append(os.path.realpath(path))
        return real_open(path, *a, **k)
    os.open = spy
    try:
        hostile = ("leak", "uplink", "../secret", "../../outside/secret",
                   "/etc/passwd", os.path.join(base, "outside", "secret"),
                   "a/b", "..", ".", "/", "secret.json", "fresh.json",
                   "fresh/", "fresh\\x", "~root", "fresh\x00", "%2e%2e",
                   " ../secret ", "bad-name", "-", "1abc", "x" * 65,
                   None, "", 7)
        for bad in hostile:
            r = st(bad)
            assert r["status"] == "UNVERIFIED", (bad, r)
            assert r.get("reason"), (bad, r)
            assert _FX_SECRET not in render(r), (bad, r)
        n0 = len(opened)
        for pid in ("../secret", "../../outside/secret", "/etc/passwd",
                    os.path.join(base, "outside", "secret"), "a/b", "..",
                    ".", "/", "secret.json", "fresh/", "~root", "%2e%2e"):
            r = st(pid)
            assert "paths are refused" in r["reason"], (pid, r)
        assert len(opened) == n0, "a path-shaped id reached the filesystem"
        lk = st("leak")
        assert "outside the statusboard store" in lk["reason"], lk
        assert "outside the statusboard store" in st("uplink")["reason"]
        allr = st("all")
        assert _FX_SECRET not in render(allr), allr
        assert st("ALL")["status"] == "OK"
    finally:
        os.open = real_open
    assert opened, "the spy saw no open at all"
    for p in opened:
        assert os.path.dirname(p) == store, ("opened outside store", p)

    # --- all: one row per card, each with its own verdict --------------
    a = st("all")
    assert a["status"] == "OK" and a["cards"] == len(a["rows"]), a
    rows = {row.split(":", 1)[0]: row for row in a["rows"]}
    assert rows["fresh"].startswith("fresh: FRESH state=ok"), rows["fresh"]
    assert "run_id=r53bc1f99313" in rows["fresh"], rows["fresh"]
    assert rows["stale"].startswith("stale: UNVERIFIED (STALE:"), rows
    assert "SUCCEEDED" not in rows["stale"], rows["stale"]
    assert rows["leak"].startswith("leak: UNVERIFIED (card resolves outside")
    assert rows["bad-name"] == "bad-name: UNVERIFIED (not a valid card id)"
    assert a["fresh"] + a["unverified"] == a["cards"], a

    # --- policy: deny by default, per agent, fail-closed ---------------
    global STATUS_POLICY
    assert st("fresh", "clark")["status"] == "UNVERIFIED"
    assert "not granted" in st("fresh", "clark")["reason"]
    assert "no agent identity" in st("fresh", None)["reason"]
    assert "no agent identity" in st("fresh", "")["reason"]
    assert read("box.status", "fresh")["status"] == "UNVERIFIED"
    assert read("box.status", "fresh", agent="clark")["status"] \
        == "UNVERIFIED"
    import time
    _real = _fx_card(0)
    _real = json.loads(_real)
    _ep = int(time.time()) - 60
    _real.update(updated_epoch=_ep, updated_utc=time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_ep)))
    with open(os.path.join(store, "live.json"), "w") as fh:
        fh.write(json.dumps(_real))
    assert read("box.status", "live", agent=T)["status"] == "OK"   # real clock
    assert st("fresh", "TreeBeard")["status"] == "OK"   # dispatch case rule
    real_pol = STATUS_POLICY
    try:
        tmp = os.path.join(base, "pol2.conf")
        for body in ("", "# nothing\n", "treebeard -> box.statusx\n",
                     "treebeard -> box.status\nthis is not a policy line\n",
                     "clark -> box.status\n"):
            with open(tmp, "w") as fh:
                fh.write(body)
            STATUS_POLICY = tmp
            r = st("fresh")
            assert r["status"] == "UNVERIFIED", (body, r)
        STATUS_POLICY = os.path.join(base, "no-such-policy.conf")
        r = st("fresh")
        assert r["status"] == "UNVERIFIED" and "unavailable" in r["reason"], r
    finally:
        STATUS_POLICY = real_pol

    _run_id_fixtures(store, T)
    _board_fixtures(base, N)
    print("STATUS FIXTURES: ALL PASS")


def _run_id_fixtures(store, T):
    """BOARD-TRUTH S5: the generic run_id, written by the REAL statusboard
    writer into this fixture store, read back by box.status."""
    import statusboard
    assert os.path.realpath(statusboard.STATUS_DIR) == store, \
        (statusboard.STATUS_DIR, store)
    statusboard.write_status("rid_on", "ok", "with a run id",
                             stale_after_hours=1,
                             run_id="clark_intake-20261001T201500Z")
    statusboard.write_status("rid_off", "ok", "without a run id",
                             stale_after_hours=1)
    on = read("box.status", "rid_on", agent=T)
    assert on["status"] == "OK", on
    assert on["run_id"] == "clark_intake-20261001T201500Z", on
    assert "run_id: clark_intake-20261001T201500Z" in render(on), on
    off = read("box.status", "rid_off", agent=T)
    assert off["status"] == "OK" and off["run_id"] == "not recorded on card"
    with open(os.path.join(store, "rid_off.json")) as fh:
        assert "run_id" not in json.load(fh), "absent run_id was written"
    # The writer refuses an id the reader would not show.
    for bad in ("../x", "", "a b", "x" * 65, 7):
        try:
            statusboard.write_status("rid_bad", "ok", "x", run_id=bad)
        except ValueError:
            continue
        raise AssertionError("write_status accepted run_id %r" % (bad,))
    rid = statusboard.make_run_id("sun", "2026-10-02T10:00:00Z")
    assert rid == "sun-20261002T100000Z", rid
    assert _RUN_RE.match(statusboard.make_run_id("will")), "reader rule"
    print("  run_id fixtures: present, absent, refused -- ok")


def _board_fixtures(base, N):
    """BOARD-TRUTH S1: board freshness on box.cards and box.card, judged by
    statusboard's ONE rule, at a fixed clock."""
    import statusboard
    bp = os.environ["TASKBOARD_PATH"]
    assert bp == os.path.join(base, "shared", "board", "board.json"), bp
    win = statusboard.BOARD_STALE_AFTER_H * 3600
    # ONE definition: boxread holds no threshold of its own.
    with open(os.path.abspath(__file__)) as fh:
        src = fh.read()
    assert not re.search(r"(?m)^\s*BOARD_STALE_AFTER_H\s*=(?!=)", src), \
        "boxread defines a second board threshold"

    def at(age_s):
        os.utime(bp, (N - age_s, N - age_s))

    # fresh: OK, last write + age + meaning on the read itself
    at(3600)
    c = cards_active(now=N)
    assert c["status"] == "OK", c
    assert c["board_last_write"] == "2026-10-01T15:00:00Z", c
    assert c["board_age"] == "1h 0m (3600s)", c
    assert c["freshness"].startswith("FRESH"), c
    assert "planning record" in c["meaning"], c
    assert "NOT execution state" in c["meaning"], c
    assert c["count"] == 1 and "FIXTURE-OPEN-CARD" in c["rows"][0], c
    assert "FIXTURE-DONE-CARD" not in render(c), c
    k = card_state("7", now=N)
    assert k["status"] == "OK" and k["board_age"] == "1h 0m (3600s)", k
    at(win)                                   # age == window: still fresh
    assert cards_active(now=N)["status"] == "OK"

    # stale: UNVERIFIED, age stated, cards kept ONLY as recorded
    at(win + 1)
    s = cards_active(now=N)
    assert s["status"] == "UNVERIFIED", s
    assert s["reason"].startswith("STALE: board last written "
                                  "2026-09-29T15:59:59Z"), s
    assert "2d 0h 0m (172801s)" in s["reason"], s
    assert "withheld as current" in s["reason"], s
    assert "rows" not in s and "count" not in s, s
    assert any("FIXTURE-OPEN-CARD" in r for r in s["recorded"]), s
    txt = render(s)
    assert txt.startswith("UNVERIFIED (cards): STALE:"), txt
    head, rec = txt.split("AS LAST RECORDED -- NOT CURRENT STATE:", 1)
    assert "FIXTURE-OPEN-CARD" in rec and "FIXTURE-OPEN-CARD" not in head
    assert "board_age: 2d 0h 0m (172801s)" in head, txt
    assert "planning record" in head, txt
    k = card_state("7", now=N)
    assert k["status"] == "UNVERIFIED" and k["reason"].startswith("STALE:")
    assert "state" not in k and k["recorded"]["text"] == "FIXTURE-OPEN-CARD"
    kt = render(k)
    assert kt.index("AS LAST RECORDED") < kt.index("FIXTURE-OPEN-CARD"), kt
    # the same board one second younger is fresh: the boundary is the rule's
    assert cards_active(now=N - 1)["status"] == "OK"

    # unjudgeable (board file absent): UNKNOWN freshness, never "empty"
    os.environ["TASKBOARD_PATH"] = bp + ".absent"
    try:
        u = cards_active(now=N)
        assert u["status"] == "UNVERIFIED", u
        assert "freshness could not be established" in u["reason"], u
        assert u.get("count") is None and u["recorded"] == [], u
    finally:
        os.environ["TASKBOARD_PATH"] = bp
    print("  board fixtures: fresh, edge, stale, absent -- ok")


# --- box.writer fixtures (CONTAINMENT T2) -------------------------------
# A throwaway TREEBEARD_BASE with a ctl/ tree (the live tbreceipt.py copied
# in), a manifest, a receipt chain and a board; the child mutates each
# input in turn. Nothing real is read except the copied tbreceipt source.

def _writer_fixture_parent():
    import shutil
    import tempfile
    real_tbr = os.path.join(CTL_DIR, "tbreceipt.py")
    if not os.path.isfile(real_tbr):
        print("  writer fixtures: SKIP (no %s on this host)" % real_tbr)
        return
    base = tempfile.mkdtemp(prefix="boxread-writer-")
    try:
        ctl = os.path.join(base, "shared", "ctl")
        os.makedirs(os.path.join(ctl, "receipts"))
        shutil.copy(real_tbr, os.path.join(ctl, "tbreceipt.py"))
        bdir = os.path.join(base, "shared", "board")
        os.makedirs(bdir)
        pol = os.path.join(base, "boxread_ops.conf")
        with open(pol, "w") as fh:
            fh.write("treebeard -> box.status\ntreebeard -> box.writer\n")
        env = dict(os.environ, TREEBEARD_BASE=base, BOXREAD_OPS_CONF=pol,
                   TASKBOARD_PATH=os.path.join(bdir, "board.json"))
        p = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--writer-fixtures"], env=env, text=True,
                           capture_output=True, timeout=60)
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        assert p.returncode == 0 and "WRITER FIXTURES: ALL PASS" in p.stdout, \
            "box.writer fixture child failed rc=%d" % p.returncode
    finally:
        try:
            os.chmod(os.path.join(base, "shared", "ctl", "receipts"), 0o700)
        except OSError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def _writer_fixture_child():
    if not __debug__:
        raise SystemExit(2)
    import importlib.util
    base = os.environ["TREEBEARD_BASE"]
    assert os.path.realpath(CTL_DIR) == os.path.realpath(
        os.path.join(base, "shared", "ctl")), CTL_DIR
    spec = importlib.util.spec_from_file_location(
        "_tbr_fx", os.path.join(CTL_DIR, "tbreceipt.py"))
    tbr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tbr)
    import board
    bp = board.board_path()
    T = "treebeard"

    def put(items):
        with open(bp, "w") as fh:
            json.dump(items, fh)
        return _sha_of(bp)

    def receipt(op, after, card=None, rev=None, principal="root-gated",
                user="root", decision="accepted", detail="fx", **res):
        # rev 8 shape; principal "operator" = the pre-rev-8 "authenticated"
        # shape, which must never count as root-gated
        rg = principal == "root-gated"
        return tbr.append(
            WRITER_RECEIPTS, op=op, decision=decision,
            caller_uid=0 if rg else 1001,
            args_hash="0" * 64, after_hash=after, detail=detail,
            identity={"exec_uid": 981, "exec_user": "tbctl",
                      "invoker_uid": 0 if rg else 1001, "invoker_user": user,
                      "principal": principal, "actor": "operator",
                      "authority": "recorded" if rg else "authenticated",
                      "actor_basis": "recorded: root-gated" if rg
                      else "authenticated"},
            result=None if card is None else dict(res, id=card, rev=rev))

    def manifest(extra=""):
        with open(T2_MANIFEST, "w") as fh:
            fh.write("installed 2026-10-04T00:00:00Z by treebeard\n"
                     "sha256 %s %s\n" % (os.path.join(CTL_DIR, "tbreceipt.py"),
                                         _sha_of(os.path.join(CTL_DIR, "tbreceipt.py")))
                     + "recorded /etc/sudoers.d/treebeard-t2 %s\n" % ("a" * 64)
                     + extra)

    # not granted -> refused before anything is read
    assert writer_state("clark")["status"] == "UNVERIFIED"
    # no manifest: T2 not installed is NOT reported as healthy
    put([{"id": 1, "kind": "task", "text": "x", "status": "open", "rev": 1}])
    r = writer_state(T)
    assert r["status"] == "UNVERIFIED" and "T2_MANIFEST" in r["reason"], r
    manifest()
    # installed, writer never used: OK, every card RECORDED
    r = writer_state(T)
    assert r["status"] == "OK", r
    assert "every card's origin is RECORDED" in r["origin"], r
    assert "POSSIBLE" in r["direct_board_writes"], r
    assert "not readable here" in r["sudoers"], r
    assert r["commission_claim"].endswith("is not protected until W."), r
    # a root-gated write through the writer: card 1 rev 2 ROOT-GATED
    sha = put([{"id": 1, "kind": "task", "text": "x", "status": "open", "rev": 2},
               {"id": 2, "kind": "task", "text": "y", "status": "open", "rev": 1}])
    receipt("set-phase", sha, card=1, rev=2)
    r = writer_state(T)
    assert r["status"] == "OK" and r["rows"] == [
        "#1 rev 2: ROOT-GATED writer change, recorded (receipt %s)"
        % json.loads(open(WRITER_RECEIPTS).read().splitlines()[-1])["ts"]], r
    assert "1 card(s) ROOT-GATED" in r["origin"], r
    assert "none authenticated" in r["origin"], r
    assert "AUTHENTICATED" not in render(r).replace("NOT operator-authenticated", ""), r
    # a pre-rev-8 "authenticated"-shaped receipt never counts
    sha = put([{"id": 1, "kind": "task", "text": "x", "status": "open", "rev": 2},
               {"id": 2, "kind": "task", "text": "y", "status": "open", "rev": 2}])
    receipt("set-phase", sha, card=2, rev=2, principal="operator", user="tbop")
    r = writer_state(T)
    assert [x for x in r["rows"] if x.startswith("#2")] == [], r
    # a DIRECT board.json write (claiming operator) after the receipt:
    # every card RECORDED, none root-gated
    put([{"id": 1, "kind": "task", "text": "x", "status": "open", "rev": 3,
          "by": "operator", "source": "operator"}])
    r = writer_state(T)
    assert r["status"] == "OK" and "OUTSIDE the writer" in r["board_vs_receipts"], r
    assert "every card RECORDED" in r["origin"] and not r.get("rows"), r
    # code drift: UNVERIFIED
    manifest("sha256 %s %s\n" % (bp, "0" * 64))
    r = writer_state(T)
    assert r["status"] == "UNVERIFIED" and "DRIFT" in r["reason"], r
    manifest()
    # chain tamper: UNVERIFIED, origin UNKNOWN
    keep = open(WRITER_RECEIPTS).read()
    lines = keep.splitlines()
    rec = json.loads(lines[0]); rec["detail"] = "tampered"
    with open(WRITER_RECEIPTS, "w") as fh:
        fh.write("\n".join([json.dumps(rec, sort_keys=True)] + lines[1:]) + "\n")
    r = writer_state(T)
    assert r["status"] == "UNVERIFIED" and "BROKEN" in r["reason"], r
    assert r["origin"].startswith("UNKNOWN"), r
    with open(WRITER_RECEIPTS, "w") as fh:
        fh.write(keep)
    # receipts unreadable (the T2 store without the tbaudit grant): UNKNOWN
    os.chmod(os.path.dirname(WRITER_RECEIPTS), 0)
    try:
        if not os.access(WRITER_RECEIPTS, os.R_OK):
            r = writer_state(T)
            assert r["status"] == "UNVERIFIED", r
            assert r["origin"].startswith("UNKNOWN"), r
            assert r["commissioning"][0].startswith("UNKNOWN") and \
                "drives NO linked card" in r["commissioning"][0], r
    finally:
        os.chmod(os.path.dirname(WRITER_RECEIPTS), 0o700)
    assert render(writer_state(T)).startswith("OK (writer)")
    # --- T2 rev 4 ruling B: commissioning findings -------------------------
    assert writer_state(T)["commission_alerts"] == 0
    # card 5: authorized + consumed at rev 2, then commissioned (linked)
    ses = {"pid": 4242, "start": 99}
    h5 = receipt("authorize-commission", None, card=5, rev=2, session=ses,
                 project="card-5")
    receipt("commission-link", None, card=5, rev=3, consumes=h5,
            project="card-5")
    # card 6: authorized + consumed, never completed (crash)
    h6 = receipt("authorize-commission", None, card=6, rev=2, session=ses,
                 project="card-6")
    # card 7: linked with no receipt (legacy / outside the writer)
    put([{"id": 5, "kind": "task", "text": "x", "status": "open", "rev": 3,
          "phase": "in_progress", "meeseeks_pid": "card-5"},
         {"id": 6, "kind": "task", "text": "x", "status": "open", "rev": 2,
          "phase": "ready"},
         {"id": 7, "kind": "task", "text": "x", "status": "open", "rev": 4,
          "phase": "blocked", "meeseeks_pid": "card-7"}])
    r = writer_state(T)
    com = r["commissioning"]
    assert r["commission_alerts"] == 0, com
    assert any("card 6 rev 2: authorization %s" % h6[:12] in x
               and "CONSUMED, never completed" in x for x in com), com
    assert any(x.startswith("card 7 rev 4 (blocked): linked to card-7 -- NOT "
                            "DRIVEN by the reflector: no commission receipt")
               for x in com), com
    assert not any("card 5" in x for x in com), com
    # REWIND: card 5's board.json bytes rewound to QUEUED, unlinked
    put([{"id": 5, "kind": "task", "text": "x", "status": "open", "rev": 2,
          "phase": "ready"}])
    r = writer_state(T)
    assert r["commission_alerts"] == 1, r["commissioning"]
    assert any(x.startswith("ALERT card 5: commissioned") and "REWIND" in x
               for x in r["commissioning"]), r["commissioning"]
    # REUSE: the spent authorization presented again, and a re-authorization
    receipt("commission-link", None, card=5, rev=2, decision="denied",
            detail="REUSE: authorization %s for card 5 rev 2 is SPENT (used "
                   "by x)" % h5[:12])
    receipt("authorize-commission", None, card=5, rev=2, decision="denied",
            detail="card 5 was already commissioned (receipt x)")
    r = writer_state(T)
    alerts = [x for x in r["commissioning"] if x.startswith("ALERT")]
    assert r["commission_alerts"] == 3 and len(alerts) == 3, alerts
    assert sum("reuse attempt" in x for x in alerts) == 2, alerts
    assert any(h5[:12] in x and "card 5 rev 2" in x for x in alerts), alerts
    assert r["status"] == "OK", r      # findings, not a failed read
    # T2 rev 6 ruling 1: a self-linked in_progress card is an ALERT, NOT DRIVEN
    put([{"id": 8, "kind": "task", "text": "x", "status": "open", "rev": 5,
          "phase": "in_progress", "meeseeks_pid": "card-8",
          "exec_ref": {"kind": "meeseeks", "id": "card-8"}},
         {"id": 5, "kind": "task", "text": "x", "status": "open", "rev": 3,
          "phase": "in_progress", "meeseeks_pid": "selfmade",
          "exec_ref": {"kind": "meeseeks", "id": "selfmade"}}])
    r = writer_state(T)
    com = r["commissioning"]
    assert any(x.startswith("ALERT card 8 rev 5 (in_progress): linked to card-8 "
                            "-- NOT DRIVEN") and "no commission receipt" in x
               for x in com), com
    assert any(x.startswith("ALERT card 5 rev 3 (in_progress): linked to "
                            "selfmade -- NOT DRIVEN") and "is not card-5" in x
               for x in com), com
    # once the card is closed: history, not alerts (the deploy's own probe)
    put([{"id": 5, "kind": "task", "text": "x", "status": "closed", "rev": 3,
          "outcome": "cancelled"}])
    r = writer_state(T)
    assert r["commission_alerts"] == 0, r["commissioning"]
    assert sum(x.startswith("history (card closed) reuse attempt")
               for x in r["commissioning"]) == 2, r["commissioning"]
    print("WRITER FIXTURES: ALL PASS")


# --- D2 read fixtures (release prep R3/S1) ------------------------------
# Same discipline as the status/writer fixtures: the parent builds a
# throwaway TREEBEARD_BASE + TREEBEARD_ETC + grant file and re-runs this
# module in a child, so every store path is derived exactly as in
# production and nothing real is read. Fake longterm/subconscious modules
# are injected in the child (memscan's technique): no Chroma, no Ollama.

_FX_KEY = "sk-ant-" + "Q" * 40          # credential-shaped, composed


def _read_fixture_parent():
    import shutil
    import sqlite3
    import tempfile
    base = tempfile.mkdtemp(prefix="boxread-d2-")
    try:
        etc = os.path.join(base, "etc")
        os.makedirs(etc)
        for a in ("treebeard", "han", "clark"):
            with open(os.path.join(etc, a + ".charter.txt"), "w") as fh:
                fh.write("charter\n")
        sh = os.path.join(base, "shared")
        md = os.path.join(sh, "meeseeks", "data")
        pr = os.path.join(md, "projects")
        c1 = os.path.join(pr, "card-1")
        for d in ("receipts", "transcripts", "notepad/s01", "notepad/s01/.git",
                  "notepad/zz"):
            os.makedirs(os.path.join(c1, d))
        os.makedirs(os.path.join(pr, "card-2"))
        os.makedirs(os.path.join(base, "outside", "receipts"))

        def w(p, s):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(s)
        w(os.path.join(base, "outside", "secret.txt"), "OUTSIDE-SECRET\n")
        w(os.path.join(base, "outside", "receipts", "r.txt"), "OUTSIDE-SECRET\n")
        w(os.path.join(c1, "receipts", "s01-1.txt"), "receipt one\n")
        w(os.path.join(c1, "receipts", "notes.bin"), "x\n")
        w(os.path.join(c1, "receipts", "bin.txt"), "a\x00b\n")
        w(os.path.join(c1, "transcripts", "m0001.txt"),
          ("line\n" * 4000) + "token=" + _FX_KEY + "\n")
        w(os.path.join(c1, "notepad", "s01", "task.md"), "the task\n")
        w(os.path.join(c1, "notepad", "zz", "x.md"), "not a section\n")
        os.symlink(os.path.join(base, "outside", "secret.txt"),
                   os.path.join(c1, "receipts", "leak.txt"))
        os.symlink(os.path.join(base, "outside"), os.path.join(pr, "card-9"))
        con = sqlite3.connect(os.path.join(md, "ledger.db"))
        con.execute("CREATE TABLE events (ts TEXT, pid TEXT, ref TEXT, "
                    "kind TEXT, detail TEXT)")
        for i in range(60):
            con.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        ("2026-01-01T00:%02d:00Z" % i, "card-1", "s01",
                         "stage", "event %d %s" % (i, _FX_KEY if i == 59 else "")))
        con.execute("INSERT INTO events VALUES ('t','card-2','s01','spawn','x')")
        con.commit()
        con.close()
        tl = os.path.join(sh, "telemetry")
        w(os.path.join(tl, "cli-2026-01-01.log"),
          "".join("STATUS line %d\n" % i for i in range(150))
          + "WARN key " + _FX_KEY + "\n")
        w(os.path.join(sh, "memory", "longterm", "consolidation_decisions.log"),
          "decided a\ndecided b\n")
        w(os.path.join(sh, "logs", "agent-sun.log"), "sun ran\n")
        st = os.path.join(sh, "memory", "shortterm")
        rows = [{"ts": "2026-01-01T00:00:0%dZ" % i, "agent": a, "thread_id": t,
                 "source": "cli", "origin": "operator-cli", "prompt": "p%d" % i,
                 "response": "r%d" % i}
                for i, (a, t) in enumerate([("han", "th-1"), ("clark", "th-2"),
                                            ("han", "th-1")])]
        w(os.path.join(st, "2026-01-01.jsonl"),
          "".join(json.dumps(r) + "\n" for r in rows))
        dr = os.path.join(sh, "memory", "dreams")
        w(os.path.join(dr, "han.jsonl"),
          "".join(json.dumps({"ts_utc": "2026-01-0%dT00:00:00Z" % i,
                              "tier": "cheap", "model": "m",
                              "fragments_consolidated": i,
                              "dream": "dream %d" % i}) + "\n"
                  for i in range(1, 6)) + "{not json\n")
        w(os.path.join(dr, "han.jsonl.consolidated-2026-W40"),
          json.dumps({"ts_utc": "w40", "dream": "week dream"}) + "\n")
        w(os.path.join(dr, "han.latest.txt"), "latest dream text\n")
        w(os.path.join(dr, "han.lock"), "")
        w(os.path.join(dr, "archive", "old.txt"), "archived\n")
        mt = os.path.join(sh, "memory", "meetings")
        w(os.path.join(mt, "2026-01-01T000000Z-treebeard-clark.txt"), "m1\n")
        w(os.path.join(mt, "2026-01-02T000000Z-clark-han-treebeard.txt"), "m2\n")
        w(os.path.join(mt, "notes.txt"), "not a meeting name\n")
        os.symlink(os.path.join(base, "outside", "secret.txt"),
                   os.path.join(mt, "2026-01-03T000000Z-han-clark.txt"))
        pol = os.path.join(base, "boxread_ops.conf")
        with open(pol, "w") as fh:
            fh.write("# fixture grant\n" + "".join(
                "treebeard -> %s\n" % op for op in D2_OPS))
        env = dict(os.environ, TREEBEARD_BASE=base, TREEBEARD_ETC=etc,
                   BOXREAD_OPS_CONF=pol)
        for k in ("MEESEEKS_ROOT", "TREEBEARD_SHORTTERM_DIR"):
            env.pop(k, None)
        p = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--read-fixtures"], env=env, text=True,
                           capture_output=True, timeout=120)
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        assert p.returncode == 0 and "D2 READ FIXTURES: ALL PASS" in p.stdout, \
            "D2 read fixture child failed rc=%d" % p.returncode
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _read_fixture_child():
    if not __debug__:
        print("boxread D2 fixtures: refusing to run under -O")
        raise SystemExit(2)
    import types
    base = os.environ["TREEBEARD_BASE"]
    assert os.path.realpath(PROJECTS_DIR).startswith(os.path.realpath(base))
    T = "treebeard"
    n = [0]

    def ok(r, kind):
        assert r["status"] == "OK", r
        assert r["kind"] == kind, r
        n[0] += 1
        return r

    def no(r, frag=""):
        assert r["status"] == "UNVERIFIED", r
        assert frag in r.get("reason", ""), (frag, r)
        n[0] += 1
        return r
    # --- grants: every op refuses an ungranted agent and no agent ---------
    for op in D2_OPS:
        no(read(op, "card-1", agent="clark"), "not granted")
        no(read(op, "card-1", agent=None), "no agent identity")
    # --- box.ledger ---------------------------------------------------------
    r = ok(read(LEDGER_OP, "card-1", agent=T), "ledger")
    assert r["returned"] == LEDGER_N and r["rows"][0].startswith(
        "2026-01-01T00:59:00Z"), r            # newest first
    assert _FX_KEY not in render(r) and r["redacted"] == 1, r
    assert ok(read(LEDGER_OP, "card-1 999", agent=T), "ledger")["returned"] == ROWS_MAX
    assert ok(read(LEDGER_OP, "card-2", agent=T), "ledger")["returned"] == 1
    for bad, frag in (("../x", "not a project"), ("CARD-1", "not a project"),
                      ("card-1;", "not a project"), ("card-7", "no such project"),
                      ("card-9", "no such project"), ("card-1 0", "at least 1"),
                      ("card-1 x", "not a number"), ("", "expected")):
        no(read(LEDGER_OP, bad, agent=T), frag)
    real = LEDGER_PATH
    globals()["LEDGER_PATH"] = os.path.join(base, "outside", "secret.txt")
    no(read(LEDGER_OP, "card-1", agent=T), "could not be read")   # not sqlite
    import sqlite3
    emp = os.path.join(base, "outside", "empty.db")
    sqlite3.connect(emp).close()
    globals()["LEDGER_PATH"] = emp
    no(read(LEDGER_OP, "card-1", agent=T), "expected shape")
    globals()["LEDGER_PATH"] = os.path.join(base, "nope.db")
    no(read(LEDGER_OP, "card-1", agent=T), "ledger missing")
    globals()["LEDGER_PATH"] = real
    # --- box.project --------------------------------------------------------
    r = ok(read(PROJECT_OP, "card-1 receipts", agent=T), "project")
    names = [x.split()[0] for x in r["rows"]]
    assert names == ["s01-1.txt"] or set(names) == {"s01-1.txt", "bin.txt"}, r
    assert "leak.txt" not in names and "notes.bin" not in names, r
    r = ok(read(PROJECT_OP, "card-1 notepad", agent=T), "project")
    assert [x.split()[0] for x in r["rows"]] == ["s01/task.md"], r
    r = ok(read(PROJECT_OP, "card-1 notepad s01/task.md", agent=T), "project")
    assert r["text"] == "the task\n", r
    r = ok(read(PROJECT_OP, "card-1 transcripts m0001.txt", agent=T), "project")
    assert r["page"] == "1 of 2" and len(r["text"]) == TEXT_MAX and r["truncated"], r
    r = ok(read(PROJECT_OP, "card-1 transcripts m0001.txt 2", agent=T), "project")
    assert _FX_KEY not in r["text"] and r["redacted"] == 1, r
    for bad, frag in (("card-1 transcripts m0001.txt 3", "does not exist"),
                      ("card-1 receipts leak.txt", "resolves outside"),
                      ("card-1 receipts ../../ledger.db", "not a file name"),
                      ("card-1 receipts .hidden", "not a file name"),
                      ("card-1 receipts notes.bin", "file type"),
                      ("card-1 receipts bin.txt", "binary"),
                      ("card-1 receipts nope.txt", "no such file"),
                      ("card-1 notepad s01/../x.md", "sNN/"),
                      ("card-1 notepad zz/x.md", "sNN/"),
                      ("card-1 notepad s01/.git", "sNN/"),
                      ("card-1 notepad s02/x.md", "no notepad section"),
                      ("card-1 ledger", "kind must be"),
                      ("card-9 receipts", "no such project"),
                      ("card-2 receipts", "has no receipts"),
                      ("card-1", "expected"),
                      ("/etc/passwd receipts", "not a project")):
        no(read(PROJECT_OP, bad, agent=T), frag)
    # --- box.log ------------------------------------------------------------
    r = ok(log("cli:2026-01-01", agent=T), "log")
    assert r["returned"] == LOG_N and r["rows"][-1].startswith("WARN key [REDACTED:"), r
    assert _FX_KEY not in render(r) and r["redacted"] == 1, r
    assert ok(log("cli:2026-01-01 999", agent=T), "log")["returned"] == LOG_MAX
    assert ok(log("cli", agent=T, now=1767225600), "log")["file"] == \
        "cli-2026-01-01.log"                                 # 2026-01-01 UTC
    r = ok(read(LOG_OP, "consolidation", agent=T), "log")
    assert r["rows"] == ["decided a", "decided b"] and not r["window_filled"], r
    assert ok(read(LOG_OP, "sun 5", agent=T), "log")["rows"] == ["sun ran"]
    for bad, frag in (("cli:2026-13-1", "YYYY-MM-DD"), ("cli:../../x", "YYYY-MM-DD"),
                      ("cli:2026-02-02", "no such file"), ("flags", "store missing"),
                      ("moon", "no such file"), ("auth", "unknown log"),
                      ("/var/log/auth.log", "unknown log"), ("", "expected")):
        no(read(LOG_OP, bad, agent=T), frag)
    # --- box.shortterm ------------------------------------------------------
    r = ok(read(SHORTTERM_OP, "han", agent=T), "shortterm")
    assert r["returned"] == 2 and all(" han " in x for x in r["rows"]), r
    assert ok(read(SHORTTERM_OP, "all", agent=T), "shortterm")["returned"] == 3
    r = ok(read(SHORTTERM_OP, "thread:th-2", agent=T), "shortterm")
    assert r["returned"] == 1 and "clark" in r["rows"][0], r
    assert ok(read(SHORTTERM_OP, "all 999 999", agent=T), "shortterm")["days"] == ST_DAYS_MAX
    for bad, frag in (("ghost", "no such agent"), ("x/../y", "not an agent"),
                      ("thread:a/b", "not a thread id"), ("han 0", "at least 1"),
                      ("han x", "not a number"), ("", "expected")):
        no(read(SHORTTERM_OP, bad, agent=T), frag)
    sdir = os.path.join(base, "shared", "memory", "shortterm")
    os.chmod(sdir, 0)
    try:
        no(read(SHORTTERM_OP, "all", agent=T), "not readable")
    finally:
        os.chmod(sdir, 0o755)
    # --- box.memory (fake stores) ------------------------------------------
    calls = []

    class _M(object):
        def __init__(self, t):
            self.consolidated_at, self.provenance, self.text = "c", "dream:W1", t

    class _F(object):
        def __init__(self, t):
            self.ts_utc, self.salience, self.text = "f", 0.9, t
    lt = types.ModuleType("longterm")

    def _q(target, caller, query_text, n_results=5):
        calls.append(("lt", target, caller, query_text, n_results))
        if target == "clark" and caller != "treebeard":
            raise PermissionError("scope")
        if target == "boom":
            raise ConnectionError("chroma down")
        return [_M("wall lesson " + _FX_KEY)]
    lt.query_memories = _q
    sc = types.ModuleType("subconscious")

    def _r(target, caller=None, query=None, n=10):
        calls.append(("sc", target, caller, query, n))
        if target == "denied":
            raise PermissionError("own scope only")
        return [_F("raw fragment")]
    sc.read_fragments = _r
    sys.modules["longterm"], sys.modules["subconscious"] = lt, sc
    r = ok(read(MEMORY_OP, "longterm clark the wall", agent=T), "memory")
    assert calls[-1] == ("lt", "clark", "treebeard", "the wall", MEM_LT_N), calls
    assert _FX_KEY not in render(r) and r["redacted"] == 1, r
    r = ok(read(MEMORY_OP, "subconscious han status claims", agent=T), "memory")
    assert "RAW" in r["meaning"] and calls[-1][2] == "treebeard", r
    no(read(MEMORY_OP, "subconscious denied x", agent=T), "scope rule refused")
    no(read(MEMORY_OP, "longterm boom x", agent=T), "unavailable: ConnectionError")
    for bad, frag in (("longterm clark", "expected"), ("episodic clark x", "store must be"),
                      ("longterm ../x q", "not an agent"), ("", "expected")):
        no(read(MEMORY_OP, bad, agent=T), frag)
    # --- box.dream + box.file on the dreams dir ----------------------------
    r = ok(read(DREAM_OP, "han", agent=T), "dream")
    assert r["returned"] == DREAM_N and r["rows"][0].startswith("2026-01-05"), r
    assert r["unparsable_lines"] == 1, r
    assert ok(read(DREAM_OP, "han 99", agent=T), "dream")["returned"] == 5
    r = ok(read(DREAM_OP, "han 1 2026-W40", agent=T), "dream")
    assert "week dream" in r["rows"][0], r
    for bad, frag in (("ghost", "no such file"), ("han 1 W40", "week must"),
                      ("han 1 2026-W40/../x", "week must"), ("../x", "not an agent"),
                      ("", "expected")):
        no(read(DREAM_OP, bad, agent=T), frag)
    ddir = os.path.join(base, "shared", "memory", "dreams")
    assert read_file(os.path.join(ddir, "han.latest.txt"))["status"] == "OK"
    for p in (os.path.join(ddir, "han.jsonl"), os.path.join(ddir, "han.lock"),
              os.path.join(ddir, "archive", "old.txt")):
        no(read_file(p))
    # --- box.meetings + box.file on the meetings dir -----------------------
    r = ok(read(MEETINGS_OP, "", agent=T), "meetings")
    assert r["total"] == 2 and r["rows"][0].startswith("2026-01-02"), r
    assert ok(read(MEETINGS_OP, "han", agent=T), "meetings")["total"] == 1
    assert ok(read(MEETINGS_OP, "1", agent=T), "meetings")["listed"] == 1
    no(read(MEETINGS_OP, "Han!", agent=T), "not a participant")
    no(read(MEETINGS_OP, "han 0", agent=T), "at least 1")
    mdir = os.path.join(base, "shared", "memory", "meetings")
    assert read_file(os.path.join(mdir, "2026-01-01T000000Z-treebeard-clark.txt")
                     )["status"] == "OK"
    for p in (os.path.join(mdir, "2026-01-03T000000Z-han-clark.txt"),
              os.path.join(mdir, "..", "shortterm", "2026-01-01.jsonl")):
        r = read_file(p)
        assert r["status"] == "UNVERIFIED" and "OUTSIDE-SECRET" not in render(r), r
        n[0] += 1
    os.chmod(mdir, 0)
    try:
        no(read(MEETINGS_OP, "", agent=T), "not readable")
    finally:
        os.chmod(mdir, 0o755)
    print("D2 read fixtures: %d checks" % n[0])
    print("D2 READ FIXTURES: ALL PASS")


if __name__ == "__main__":
    if sys.argv[1:] == ["--read-fixtures"]:
        _read_fixture_child()
    elif sys.argv[1:] == ["--status-fixtures"]:
        _status_fixture_child()
    elif sys.argv[1:] == ["--writer-fixtures"]:
        _writer_fixture_child()
    else:
        _selftest()
