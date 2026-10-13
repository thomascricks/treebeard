#!/usr/bin/env python3
"""Board <-> Meeseeks lifecycle reflector (Treebeard MVP control plane).

The bridge between an approved board card and the autonomous development
pipeline that already exists. It drives and reflects -- it does NOT
sequence development stages; meeseeks does that internally.

CONTAINMENT T2 revision 4, ruling B (Tex): the reflector does NOT
commission. There is no commissioning code here -- no dormant path, flag or
alternate entry. A card gets its project only through the single root-run
command /usr/local/sbin/tb-commission, as root (queue + consume + commission,
with a tbctl-owned one-shot receipt; rev 8: root-gated and RECORDED, the
operator is not authenticated -- Router 2.0). A QUEUED card without a
project is reported as awaiting the operator and is otherwise left alone,
so no board.json state -- rewound or forged -- can make this process create
or link one.

CONTAINMENT T2 revision 6, ruling 1 (Tex): the reflector DRIVES a linked
project only when that exact (card, project pid) linkage is backed by a USED
commission receipt (drive_authority / linkage_problem). board.json stays
treebeard-writable until W, so a self-created project linked by a direct
board write is otherwise a commissioning bypass. Absent, mismatched, unused,
malformed, reused or unverifiable receipts mean: not driven, and reported.
This only VALIDATES root-gated authority already exercised; it grants none,
writes no receipt and repairs nothing.

INVARIANT (2026-08-27): meeseeks PERSISTED STATE is the only authoritative
signal. Process exit status and stdout are not control-plane signals --
cmd_run exits 0 both when a gate is reached and when the step cap trips.

INVARIANT: only the exact section state AWAITING_TREEBEARD can produce the
board phase `review`. Unknown or future states map to `in_progress`, never
`review`.

BOARD-TRUTH L (2026-10-01):
  - Every board write is board.transition() with actor "reflector" (L-R1
    INTERIM routing; no new writer, op or authority).
  - TERMINAL execution (project CLOSED_BLOCKED / FACTORY_EXHAUSTED, a
    section UNREVIEWABLE, or a section BLOCKED for factory exhaustion) maps
    to `failed`, is NEVER driven (fixes M4: UNREVIEWABLE was driven as
    in_progress), and is written as the board FAILED state only when the
    migration flag exists. The flag is ABSENT until the migration step
    creates it (Moe flag 1): before that, card 58 is not moved.
  - A finished project under a card with open children is a non-fault
    HOLD, and any per-card board refusal is a per-card fault -- a single
    card can no longer abort the tick (fixes M5).
  - After the migration, each live tick writes the board.meta.json
    reconcile stamp (L-R7). Both post-migration behaviours key off ONE
    operator flag, /etc/treebeard/board-truth/l-migrated (migrated()).

Layering: board.py knows board-native phase transitions only. The meaning
of a meeseeks state lives here and nowhere else.

CANONICAL LOCATION (2026-08-31, S-BRIDGE-1 ruling 3): this module lives at
shared/lib/reflector.py and nowhere else. An operator-only compatibility
launcher may exist outside the installed tree; it is a thin wrapper that
execs this module -- NOT a second copy. board.py is a sync pair by an
earlier ruling; the reflector deliberately is not. Do not reintroduce a
second copy of this module anywhere.
"""
import argparse
import contextlib
import fcntl
import os
import stat
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import board  # noqa: E402  (resolves to the lib copy beside this module)
import uistate  # noqa: E402  (ONE definition of terminal execution)

# 2026-08-31 (S-BRIDGE-1 S6, PO ruling 3): the default is DERIVED from
# TREEBEARD_BASE, matching the sms.py store_dir() shape ruled correct before
# the RC4 freeze. The hardcoded default made this module unpublishable: on a
# relocated install `import meeseeks` reached a path that does not exist and
# the module failed at import. An explicit MEESEEKS_DIR still wins.
#
# A FUNCTION rather than a bare expression, for the same reason store_dir()
# is one: the resolution must be assertable WITHOUT re-importing this module,
# and a module-level constant can only be re-resolved in a subprocess that
# cannot complete `import meeseeks` under a base that does not exist. See
# MeeseeksDirResolutionTests in test_reflector.py.
def _meeseeks_dir():
    base = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
    return os.environ.get("MEESEEKS_DIR",
                          os.path.join(base, "shared", "meeseeks"))


_MEESEEKS_DIR = _meeseeks_dir()
if _MEESEEKS_DIR not in sys.path:
    sys.path.insert(0, _MEESEEKS_DIR)
import meeseeks  # noqa: E402

GATE_STATE = "AWAITING_TREEBEARD"   # the ONLY state that yields `review`
DONE_STATE = "DONE"
ACTOR = "reflector"                 # board.transition() actor (executor)
TERMINAL_PROJECT_STATES = ("CLOSED_BLOCKED", "FACTORY_EXHAUSTED")


def migrated_flag():
    return os.environ.get("BOARD_L_MIGRATED_FLAG",
                          "/etc/treebeard/board-truth/l-migrated")


def migrated():
    """True only when the operator's migration step has created this
    regular file (root, /etc/treebeard). Absent, unreadable, or not a
    regular file -> False: the safe direction is no write."""
    try:
        return stat.S_ISREG(os.stat(migrated_flag()).st_mode)
    except OSError:
        return False


def fail_close_enabled():
    """Moe flag 1: the reflector writes FAILED only after the migration.
    Before it, card 58 is not moved (shipped OFF)."""
    return migrated()


def reconcile_stamp_enabled():
    """L-R7 stamp, also after the migration only: before it the board is
    KNOWN to need correction, so "unchanged, reconciled" must not turn
    S1's STALE verdict into OK. Until then S1 stays exactly as S shipped."""
    return migrated()


class ReflectorError(RuntimeError):
    """Execution fault. The card and project are left recoverable."""


def pid_for(card_id):
    return "card-%d" % int(card_id)


def _card(items, card_id):
    it = board.find(items, card_id)
    if it is None:
        raise ReflectorError("no board item with id %s" % card_id)
    return it


def awaiting_operator(it):
    """A QUEUED card with no project: only a root-run tb-commission can
    give it one. Reported, never acted on."""
    return (it.get("status") == "open"
            and it.get("phase") == "ready"
            and not it.get("meeseeks_pid"))


# --- drive authority (T2 rev 6, ruling 1) -----------------------------------
# Paths are constants, not environment: a caller cannot point the check at
# another store. The store is read through group tbaudit; this process can
# neither write nor forge it, and a store it could write is refused.
RECEIPTS = "/opt/treebeard/shared/ctl/receipts/board.receipts"
TBRECEIPT = "/opt/treebeard/shared/ctl/tbreceipt.py"


class DriveAuthorityUnknown(ReflectorError):
    """The receipts cannot be read or verified: nothing is driven."""


def _root_only(path):
    """None if `path` and its directory are root-owned and NOT writable by
    this process; else the reason."""
    for q in (path, os.path.dirname(path)):
        try:
            st = os.stat(q)
        except OSError as e:
            return "%s unreadable (%s)" % (q, e.strerror or e)
        if st.st_uid != 0:
            return "%s is not root-owned" % q
        if os.access(q, os.W_OK):
            return "%s is writable by this process" % q
    return None


def drive_receipts():
    """The verified receipt records, or DriveAuthorityUnknown."""
    for q in (RECEIPTS, TBRECEIPT):
        why = _root_only(q)
        if why:
            raise DriveAuthorityUnknown("receipt store untrusted: %s" % why)
    try:
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location("_tbr_drive", TBRECEIPT)
        tbr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tbr)
        ok, n, bad = tbr.verify_chain(RECEIPTS)
        if not ok:
            raise DriveAuthorityUnknown("receipt chain BROKEN at record %s of "
                                        "%s" % (bad, n))
        with open(RECEIPTS, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except DriveAuthorityUnknown:
        raise
    except Exception as e:
        raise DriveAuthorityUnknown("receipts could not be verified: %s" % (e,))


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


def linkage_problem(it, receipts):
    """None if the card's exact (card, project pid) linkage is backed by a
    USED commission receipt; else why it is not. The same rule is in
    boxread.drive_verdict (the sensor); tests hold the two together."""
    cid = it.get("id")
    pid = it.get("meeseeks_pid")
    ref = it.get("exec_ref") or {}
    if not pid:
        return "no project linked"
    if ref and (ref.get("kind") != "meeseeks" or ref.get("id") != pid):
        return "exec_ref %r disagrees with meeseeks_pid %r" % (ref, pid)
    if pid != pid_for(cid):
        return ("linked project %r is not %s, the only project a commission "
                "of card %s creates" % (pid, pid_for(cid), cid))
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


def drive_authority(card_id, receipts=None):
    """None if the reflector may drive this card's project; else the reason.
    Raises DriveAuthorityUnknown if the receipts cannot be verified."""
    if receipts is None:
        receipts = drive_receipts()
    return linkage_problem(_card(board.load_board(), card_id), receipts)


def drive(pid, max_steps=None):
    """Step the project until it stops progressing or the cap trips.
    Neither outcome is read from here -- state decides, not this loop."""
    con = meeseeks.connect()
    cap = int(max_steps or meeseeks.run_max_default())
    steps = 0
    for _ in range(cap):
        prog, _msg = meeseeks.step_once(con, pid)
        steps += 1
        if not prog:
            break
    return steps


def project_facts(pid):
    """(phase, reason) from persisted meeseeks state. Positive allowlist."""
    con = meeseeks.connect()
    prow = meeseeks.get_project(con, pid)
    if prow is None:
        raise ReflectorError("no meeseeks project %r" % pid)
    try:
        pstate = prow["state"]
    except (IndexError, KeyError, TypeError):
        pstate = None
    rows = con.execute("SELECT * FROM sections WHERE pid=?",
                       (pid,)).fetchall()
    secs = [dict(r) for r in rows]
    if not secs:                      # all() over empty is True -- guard it
        raise ReflectorError(
            "project %r has no sections; refusing to infer a phase" % pid)
    # Terminal FIRST: a dead project is never driven and never `blocked`.
    if pstate in TERMINAL_PROJECT_STATES:
        return "failed", "project %s" % pstate
    for s_ in secs:
        if uistate.section_terminal(s_.get("state"),
                                    s_.get("blocked_reason")):
            return "failed", "section %s %s%s" % (
                s_.get("sid"), s_.get("state"),
                (": " + s_["blocked_reason"]) if s_.get("blocked_reason")
                else "")
    states = [s_.get("state") for s_ in secs]
    if any(s == "BLOCKED" for s in states):
        why = [s_.get("blocked_reason") for s_ in secs
               if s_.get("state") == "BLOCKED" and s_.get("blocked_reason")]
        return "blocked", (why[0] if why else "meeseeks section BLOCKED")
    unfinished = [s for s in states if s != DONE_STATE]
    if not unfinished:
        return "done", None
    if all(s == GATE_STATE for s in unfinished):
        return "review", None
    return "in_progress", None


def project_phase(pid):
    """Map persisted meeseeks state to a board phase (or done / failed)."""
    return project_facts(pid)[0]


class Held(Exception):
    """A non-fault hold: the target is known but is not written now."""


def _apply(card_id, it, target, reason):
    """Write target through board.transition (actor reflector). Raises Held
    for the two non-fault holds; board refusals propagate."""
    pid = it.get("meeseeks_pid")
    if target == "done":
        if it.get("status") == "open":
            try:
                board.mark_done(card_id, actor=ACTOR)
            except board.ChildrenOpen as e:
                raise Held("execution DONE; board completion held: %s" % e)
        return "done"
    if target == "failed":
        if not fail_close_enabled():
            raise Held("execution terminal (%s); fail-close is OFF until the "
                       "migration -- card not moved" % reason)
        board.transition(card_id, "fail", ACTOR, reason=reason or
                         "terminal execution",
                         exec_ref={"kind": "meeseeks", "id": pid})
        return "failed"
    kw = {"reason": reason} if target == "blocked" else {}
    board.set_phase(card_id, target, actor=ACTOR, **kw)
    return target


def reflect(card_id):
    """Idempotent: write the card's state only when it should change.
    Returns the target; a Held target is not written."""
    items = board.load_board()
    it = _card(items, card_id)
    pid = it.get("meeseeks_pid")
    if not pid:
        raise ReflectorError("card %s has no meeseeks_pid" % card_id)
    target, reason = project_facts(pid)
    if target in ("done", "failed") or it.get("phase") != target:
        try:
            return _apply(card_id, it, target, reason)
        except Held:
            return target
    return target


def run_card(card_id, max_steps=None, do_drive=True):
    it = _card(board.load_board(), card_id)
    # Neither start nor sync commissions (T2 rev 4 ruling B): a card without
    # a project is the operator's to commission with tb-commission.
    if not it.get("meeseeks_pid"):
        raise ReflectorError("card %s has no project to reflect; "
                             "commissioning is the operator's "
                             "/usr/local/sbin/tb-commission" % card_id)
    if do_drive:
        why = drive_authority(card_id)      # Unknown propagates: not driven
        if why:
            raise ReflectorError("card %s NOT driven: %s" % (card_id, why))
        drive(pid_for(card_id), max_steps)
    return reflect(card_id)


# --- Autonomous tick (S-BRIDGE-1, PO rulings 2026-08-31) ----------------
# The tick is the ONLY autonomous entry point, and it never crosses Gate 2:
# it does not call review, approve, reject, retask, close or retro.
#
# DRIVE SET, as ruled:
#   ready        (no project) REPORTED as awaiting the operator's
#                tb-commission; never commissioned here (T2 rev 4 ruling B)
#   in_progress  reconcile, then drive
#   blocked      reconcile only -- a blocked project pauses AS A UNIT until a
#                human resolves it, even where sibling sections could still
#                advance. `blocked` has to mean something operationally.
#   review       reconcile only, NEVER driven. Reconciliation is how a card
#                reaches `done` after a human approves at Gate 2; skipping it
#                would put a manual `sync` back into the Gate 2 path.
#   returned     HELD: reconciled, but a tick never moves it to in_progress
#                and never drives it. project_phase() cannot return
#                `returned` -- it is board-only -- so an unguarded reflect()
#                would silently erase an operator pause and resume spending.
#                Only an operator resumes a returned card.
#
# `review` and `blocked` are non-drivable BY THE REFLECTOR. meeseeks.step_once
# no-ops on those section states today (_pick selects only TESTING /
# REVIEW1-3 / QUEUED), but that is a property of a callee and could change.
# The refusal is expressed here so it cannot be lost to a change there.

DRIVABLE_PHASES = ("in_progress",)
HELD_PHASES = ("returned",)


def lock_path():
    """One canonical lock per board. Timer and interactive invocations read
    the same TASKBOARD_PATH, so they contend for the same lock by
    construction rather than by a constant that could drift away from the
    board it guards. REFLECTOR_LOCK overrides it."""
    return os.environ.get(
        "REFLECTOR_LOCK",
        os.path.join(os.path.dirname(os.path.abspath(board.board_path())),
                     ".reflector-tick.lock"))


@contextlib.contextmanager
def _tick_lock():
    """Non-blocking exclusive flock held for the WHOLE tick. Yields True if
    acquired, False if another tick owns it.

    The lockfile is opened append-mode and never truncated or removed: a
    stale file on disk carries no meaning and cannot block anything, because
    kernel flock state is the sole authority. The lock is tied to this
    process's open file description, so it is released on normal return, on
    exception, and on process death alike -- it cannot be stranded."""
    fh = open(lock_path(), "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def tick_steps():
    """Per-card step budget for ONE tick. Deliberately NOT meeseeks
    run_max_default(): the control plane owns its own bounded work budget.
    Rate/burst control lives here; the total project ceiling stays call_cap."""
    return int(os.environ.get("REFLECTOR_TICK_STEPS", "4"))


def linked_active(items):
    """Cards carrying a project pointer whose board work is still OPEN.
    `== open`, not `!= done`: a FAILED/CLOSED card is finished too, and
    must not be reconciled forever (L C2)."""
    return [it for it in items
            if it.get("meeseeks_pid") and it.get("status") == "open"]


def _reconcile_card(card_id, dry_run=False):
    """Reflect one card, honouring the operator hold. _apply() is the sole
    writer of card state. Returns (phase, action); action starts with
    "held" for a non-fault hold."""
    it = _card(board.load_board(), card_id)
    pid = it.get("meeseeks_pid")
    if not pid:
        raise ReflectorError("card %s has no meeseeks_pid" % card_id)
    cur = it.get("phase")
    target, reason = project_facts(pid)
    if cur in HELD_PHASES and target == "in_progress":
        return cur, "held"
    if target == "failed" and not fail_close_enabled():
        return target, ("held: execution terminal (%s); fail-close OFF until "
                        "the migration, card not moved" % reason)
    if dry_run:
        return target, ("would" if target != cur else "unchanged")
    if target == cur:
        return target, "unchanged"
    try:
        return _apply(card_id, it, target, reason), "reflected"
    except Held as h:
        return target, "held: %s" % h


def tick(dry_run=False, steps=None):
    """Entry point. Acquires the tick lock BEFORE any scan, board read,
    mutation or meeseeks call, and holds it for the entire pass.

    Contention is a CLEAN NO-OP at zero faults, so the caller exits 0:
    nothing is broken -- the guard did precisely its job, and a busy lock
    must not manufacture a service failure. --dry-run takes the same lock,
    so a dry run can never report on state a live tick is midway through
    mutating."""
    with _tick_lock() as held:
        if not held:
            return (["[tick] tick already running (%s); nothing done"
                     % lock_path()], 0)
        return _tick_locked(dry_run, steps)


def _tick_locked(dry_run=False, steps=None):
    """One autonomous pass: scan once, report QUEUED cards awaiting the
    operator's tb-commission, reconcile every linked active card, drive
    only in_progress cards for at most `steps` each, then reconcile again
    whatever was driven -- so a project that reaches the gate mid-tick does
    not sit at the wrong board phase until the next timer firing. Returns
    (lines, faults).

    A fault on one card never stops the others; the tick reports every one
    and the caller exits non-zero so the unit is visibly failed."""
    cap = int(steps) if steps else tick_steps()
    out = []
    faults = 0

    items = board.load_board()
    waiting = [it for it in items if awaiting_operator(it)]
    active = linked_active(items)
    out.append("[tick] scan: %d awaiting operator, %d linked active, steps=%d%s"
               % (len(waiting), len(active), cap, " DRY-RUN" if dry_run else ""))
    for it in waiting:
        out.append("[tick] card %s QUEUED without a project: awaiting the "
                   "operator's tb-commission (the reflector does not "
                   "commission)" % it["id"])

    phases = {}
    inconsistent = []
    for it in linked_active(board.load_board()):
        cid = it["id"]
        try:
            ph, action = _reconcile_card(cid, dry_run)
            phases[cid] = ph
            if action.startswith("held:"):
                inconsistent.append(cid)
            out.append("[tick] reconcile card %s: %s (%s)" % (cid, ph, action))
        except Exception as exc:      # M5: one card never stops the others
            faults += 1
            inconsistent.append(cid)
            out.append("[tick] FAULT reconcile card %s: %s: %s"
                       % (cid, type(exc).__name__, exc))

    driven = []
    receipts = None
    for cid in sorted(phases):
        if phases[cid] not in DRIVABLE_PHASES:
            continue
        # T2 rev 6 ruling 1: drive only a linkage backed by a USED commission
        # receipt. UNKNOWN (unreadable/unverifiable receipts) is a fault and
        # drives nothing; an unauthorized linkage is reported, not driven.
        try:
            if receipts is None:
                receipts = drive_receipts()
            why = drive_authority(cid, receipts)
        except DriveAuthorityUnknown as exc:
            faults += 1
            out.append("[tick] NOT driven card %s: drive authority UNKNOWN "
                       "(%s)" % (cid, exc))
            continue
        if why:
            out.append("[tick] NOT driven card %s: linkage has no USED "
                       "commission receipt (%s)" % (cid, why))
            continue
        if dry_run:
            out.append("[tick] WOULD drive card %s for up to %d step(s)"
                       % (cid, cap))
            continue
        try:
            n = drive(pid_for(cid), cap)
            driven.append(cid)
            out.append("[tick] drive card %s: %d step(s)" % (cid, n))
        except Exception as exc:
            faults += 1
            out.append("[tick] FAULT drive card %s: %s: %s"
                       % (cid, type(exc).__name__, exc))

    for cid in driven:
        try:
            ph, action = _reconcile_card(cid)
            if action.startswith("held:") and cid not in inconsistent:
                inconsistent.append(cid)
            out.append("[tick] post-drive reconcile card %s: %s (%s)"
                       % (cid, ph, action))
        except Exception as exc:
            faults += 1
            if cid not in inconsistent:
                inconsistent.append(cid)
            out.append("[tick] FAULT post-drive reconcile card %s: %s: %s"
                       % (cid, type(exc).__name__, exc))

    if not dry_run and reconcile_stamp_enabled():
        # L-R7 reconcile stamp: freshness/reconcile metadata ONLY. A failure
        # here is a fault line, never a crash of the tick.
        try:
            now = time.time()
            utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            board.write_meta(
                ACTOR, reconciled_utc=utc, reconciled_epoch=int(now),
                reconciler="reflector tick",
                run_id="reflector-" + utc.replace("-", "").replace(":", ""),
                linked_checked=len(phases) + sum(
                    1 for c in inconsistent if c not in phases),
                inconsistent=sorted(set(inconsistent)), faults=faults)
            out.append("[tick] reconcile stamp written (%d inconsistent)"
                       % len(set(inconsistent)))
        except Exception as exc:
            faults += 1
            out.append("[tick] FAULT reconcile stamp: %s: %s"
                       % (type(exc).__name__, exc))

    out.append("[tick] summary: reconciled=%d driven=%d faults=%d"
               % (len(phases), len(driven), faults))
    return out, faults


def cmd_tick(args):
    lines, faults = tick(dry_run=args.dry_run, steps=args.steps)
    for line in lines:
        print(line)
    return 1 if faults else 0


def cmd_start(args):
    print(run_card(args.id, max_steps=args.max_steps, do_drive=True))
    return 0


def cmd_sync(args):
    print(run_card(args.id, do_drive=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reflector",
                                 description="board <-> meeseeks bridge")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start", help="drive + reflect a commissioned card "
                       "(commissioning is the operator's tb-commission)")
    p.add_argument("id", type=int)
    p.add_argument("--max-steps", type=int, default=None,
                   help="override the meeseeks run cap (acceptance runs)")
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("sync", help="reflect current project state only")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("tick",
                       help="one autonomous pass (timer entry point)")
    p.add_argument("--dry-run", action="store_true",
                   help="report intended actions; write nothing")
    p.add_argument("--steps", type=int, default=None,
                   help="override REFLECTOR_TICK_STEPS for this run")
    p.set_defaults(func=cmd_tick)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
