#!/usr/bin/env python3
"""board.py -- hierarchical task board. One file, stdlib only. v2.

Storage: a single JSON array of item objects at TASKBOARD_PATH
(default ./board.json). Each item:
    id      : int, monotonic, never reused within a live board
    kind    : "epic" | "feature" | "story" | "task"
    text    : str
    parent  : int or None
    status  : "open" | "done" | "closed"   (closed: see BOARD-TRUTH L below)
    created : ISO-8601 UTC timestamp
    source  : "operator" | "treebeard" | "agent:<name>"   (v2)
    owner   : agent name or None                          (v2)

v2 additions (the Will substrate):
    - source precedence: operator tasks always outrank treebeard's,
      treebeard's outrank agent-derived, regardless of posting time.
      list renders roots in that order; an agent cannot self-promote.
    - owner: whose plate an item sits on. `plate NAME` lists an agent's
      open items -- the emptiness test Will uses for derive-your-own-work.
    - v1 boards load cleanly: missing source -> "operator", owner -> None.

BOARD-TRUTH L (2026-10-01, Tex rulings L-R1..L-R9) -- the canonical
lifecycle. Every mutation of the board goes through ONE function,
transition(), under ONE cross-process lock (board_lock). The legacy entry
points (add_item, set_phase, set_meeseeks_pid, mark_done, assign, top) are
thin wrappers over it, so the reflector, Will and the dispatch board ops all
reach transition() without a new writer, a new op or a new authority.
INTERIM until W: board_writer (CONTAINMENT T2) will call transition()
verbatim; the `actor` argument is SELF-DECLARED attribution, not
authentication -- it can only narrow what a caller may do, never widen it.

    status  : "open" | "done" | "closed"                   (L-R2)
              done   = successful completion ONLY
              closed = terminal, with `outcome` in
                       cancelled | superseded | deferred | failed
    phase   : None | awaiting_approval | ready | in_progress | review |
              returned | blocked       (open cards only; cleared on close)
    rev     : int, +1 on every transition (absent on never-touched cards = 0)
    updated, by, history[] (bounded), exec_ref {kind,id,run_id?},
    reason, outcome, ref, last_failure      -- written only by transition()

Canonical states (canonical_state): BACKLOG, AWAITING_APPROVAL, QUEUED,
ACTIVE, IN_REVIEW, BLOCKED, RETURNED, DONE, FAILED, CLOSED.

Hierarchy rules (enforced on add, never on read):
    epic    -> no parent
    feature -> parent must be an epic
    story   -> parent must be a feature
    task    -> generic; parentless, or --under anything

Writes are atomic: temp file in the same directory, then os.replace.
A missing or corrupt board file is treated as an empty board.

Extensibility: every command is a handler registered in COMMANDS; the
argparse subparsers are built from that dict. To add a command, write
one cmd_* function and add one dict entry -- nothing else changes.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

KINDS = ("epic", "feature", "story", "task")

# Which parent kinds each child kind accepts. None in the tuple means
# "parentless is allowed". "task" accepts anything including parentless.
PARENT_RULES = {
    "epic": (None,),
    "feature": ("epic",),
    "story": ("feature",),
    "task": (None,) + KINDS,
}

# Precedence: lower rank sorts first. Operator always outranks treebeard,
# treebeard always outranks any agent-derived item. All agents share one
# rank -- no agent outranks another by construction.
SOURCE_RANK = {"operator": 0, "treebeard": 1}
AGENT_RANK = 2

# Same name alphabet ask.py / memory_scopes.conf accept.
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def source_rank(source):
    """Rank a source string for precedence sorting. Unknown or malformed
    sources sort last -- junk never outranks real work."""
    if source in SOURCE_RANK:
        return SOURCE_RANK[source]
    if isinstance(source, str) and source.startswith("agent:"):
        return AGENT_RANK
    return AGENT_RANK + 1


def valid_source(source):
    """operator | treebeard | agent:<valid-name>."""
    if source in SOURCE_RANK:
        return True
    if isinstance(source, str) and source.startswith("agent:"):
        return bool(_AGENT_RE.match(source[len("agent:"):]))
    return False


# State root, not code root: the board is mutable state, so its default follows
# TREEBEARD_BASE like every other store. A hard literal pointed a relocated or
# freshly installed system at a board that does not exist on that host.
_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
DEFAULT_BOARD_PATH = f"{_TB_BASE}/shared/board/board.json"


def board_path():
    """Resolve the board file path from the environment at call time so
    tests (and agents) can repoint it without reimporting.

    The default is ABSOLUTE and canonical. A relative default silently
    resolved against the caller's cwd, so an invocation from the wrong
    directory read or created a phantom empty board instead of failing
    (three incidents 2026-08-27). Callers that want a different board
    set TASKBOARD_PATH explicitly, as every alias and wrapper does."""
    return os.environ.get("TASKBOARD_PATH", DEFAULT_BOARD_PATH)


def _upgrade(it):
    """v1 -> v2 in memory: absent fields get safe defaults. Pre-Will
    items were all operator-posted by definition."""
    it.setdefault("source", "operator")
    it.setdefault("owner", None)
    it.setdefault("priority", 0)
    it.setdefault("phase", None)
    it.setdefault("meeseeks_pid", None)
    return it


def load_board():
    """Read the board. Missing file or corrupt JSON degrades to an empty
    board -- a board that dies on a bad byte is not a board. Non-list
    JSON (e.g. someone hand-wrote an object) also degrades.

    ABSENT is not the same as EMPTY. Degrading silently on an absent default
    board hid a broken bootstrap: a fresh install that never created the board
    read as a working board with no cards. The return value is unchanged ([])
    so no caller behaviour shifts; the condition is merely announced once."""
    try:
        with open(board_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return [_upgrade(it) for it in data] if isinstance(data, list) else []
    except FileNotFoundError:
        if "TASKBOARD_PATH" not in os.environ:
            import sys as _sys
            print("board: no board file at %s -- treating as empty. Set "
                  "TASKBOARD_PATH, or initialize it (install.sh dirs)."
                  % DEFAULT_BOARD_PATH, file=_sys.stderr)
        return []
    except (OSError, ValueError):
        return []


def save_board(items):
    """Atomic write: temp file in the destination's own directory, then
    os.replace. Same-directory matters -- os.replace across filesystems
    is not atomic, and temp dirs are often a different mount."""
    path = board_path()
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".board-", dir=d)
    os.fchmod(fd, 0o640)  # mkstemp creates 0600 regardless of umask; enforce tree convention
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray temp file behind on any failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def next_id(items):
    """Monotonic within the live board: one past the current max id."""
    return (max((it["id"] for it in items), default=0)) + 1


def find(items, iid):
    for it in items:
        if it["id"] == iid:
            return it
    return None


def children_of(items, iid):
    return [it for it in items if it["parent"] == iid]


# ---------------------------------------------------------------------------
# BOARD-TRUTH L: the lock, the canonical states, and the ONE transition.
# ---------------------------------------------------------------------------

class BoardError(ValueError):
    """Every lifecycle refusal. A ValueError subclass on purpose: callers
    that already catch ValueError (Will, the CLI funnels) keep working."""


class TransitionRefused(BoardError):
    """Illegal edge, missing required field, or a guard said no."""


class PermissionRefused(BoardError):
    """The actor class may not perform this op."""


class StaleRevision(BoardError):
    """expect_rev does not match the card's current rev."""


class ChildrenOpen(TransitionRefused):
    """complete on a card that still has open descendants."""


class BoardLockTimeout(BoardError):
    """Another writer held the board lock past the timeout."""


# --- the lock (Moe flag 2; L-R1 "serialization is mandatory") -------------
# ONE lock per board file, beside it, shared by every writer and both
# board.py copies (they resolve the same TASKBOARD_PATH). Opened O_RDONLY so
# a lockfile created by any uid stays usable by every other uid that can
# read it; kernel flock state is the only authority, so the file itself
# carries no meaning and is never removed. Blocking with a bound: the
# reflector and Will fire in the same second, and the loser must WAIT for
# the winner's write rather than read a board that is about to change.
_LOCK = {"depth": 0, "path": None}


def lock_path():
    return os.environ.get(
        "BOARD_LOCK",
        os.path.join(os.path.dirname(os.path.abspath(board_path())),
                     ".board.lock"))


def _lock_timeout():
    try:
        return float(os.environ.get("BOARD_LOCK_TIMEOUT_S", "30"))
    except ValueError:
        return 30.0


@contextlib.contextmanager
def board_lock(timeout=None):
    """Exclusive cross-process lock around load -> modify -> save.
    Re-entrant within one process (a nested call on the SAME board just
    deepens); raises BoardLockTimeout when not acquired in time."""
    path = lock_path()
    if _LOCK["depth"]:
        if _LOCK["path"] != path:
            raise BoardError("nested board_lock on a different board")
        _LOCK["depth"] += 1
        try:
            yield
        finally:
            _LOCK["depth"] -= 1
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o660)
    except OSError as e:
        raise BoardError("cannot open board lock %s: %s" % (path, e))
    try:
        wait = _lock_timeout() if timeout is None else float(timeout)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BoardLockTimeout(
                        "board lock %s busy for %.1fs" % (path, wait))
                time.sleep(0.02)
        _LOCK.update(depth=1, path=path)
        try:
            yield
        finally:
            _LOCK.update(depth=0, path=None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- canonical states (L-R2) ----------------------------------------------
STATUSES = ("open", "done", "closed")
CLOSE_OUTCOMES = ("cancelled", "superseded", "deferred")
# L-R2 choice: `failed` IS RETAINED as the failure outcome of status closed.
# One terminal-unsuccessful status means every legacy `== "done"` reader
# fails safe (a failed card is never counted as a success), and FAILED stays
# distinct from the operator's cancelled/superseded/deferred by its outcome.
FAIL_OUTCOME = "failed"
OUTCOMES = CLOSE_OUTCOMES + (FAIL_OUTCOME,)
APPROVAL_PHASE = "awaiting_approval"

_STATE_OF_PHASE = {None: "BACKLOG", APPROVAL_PHASE: "AWAITING_APPROVAL",
                   "ready": "QUEUED", "in_progress": "ACTIVE",
                   "review": "IN_REVIEW", "blocked": "BLOCKED",
                   "returned": "RETURNED"}
_PHASE_OF_STATE = {v: k for k, v in _STATE_OF_PHASE.items()}
OPEN_STATES = tuple(_STATE_OF_PHASE.values())
TERMINAL_STATES = ("DONE", "FAILED", "CLOSED")


def canonical_state(it):
    """The card's canonical state from (status, phase, outcome). Anything
    not in the contract is UNKNOWN -- never coerced to a known state."""
    st = it.get("status")
    if st == "done":
        return "DONE"
    if st == "closed":
        oc = it.get("outcome")
        if oc == FAIL_OUTCOME:
            return "FAILED"
        return "CLOSED" if oc in CLOSE_OUTCOMES else "UNKNOWN"
    if st == "open":
        return _STATE_OF_PHASE.get(it.get("phase"), "UNKNOWN")
    return "UNKNOWN"


def is_open(it):
    return it.get("status") == "open"


# --- approval criteria (L-R3) ---------------------------------------------
# A card needs Gate 1 approval ONLY when it changes operator-authorized
# scope, takes an external action, deploys, or touches authority /
# governance / containment -- declared as approval_class at create -- or
# explicitly carries approval_required. Routine agent tasks carry neither
# and never need it. A present-but-unrecognised class FAILS CLOSED.
APPROVAL_CLASSES = ("scope", "external_action", "deployment", "authority")


def requires_approval(it):
    if it.get("approval_required") is True:
        return True
    return it.get("approval_class") is not None


# --- actors ----------------------------------------------------------------
# SELF-DECLARED attribution, NOT authentication (interim until W/T2). The
# table below can only make a caller do LESS than its uid already could.
def actor_class(actor):
    if actor == "operator":
        return "operator"
    if actor == "reflector":
        return "executor"
    if actor == "will":
        return "scheduler"
    if isinstance(actor, str) and actor.startswith("agent:") \
            and _AGENT_RE.match(actor[len("agent:"):]):
        return "agent"
    return None


# --- exec_ref: identifies execution, confers NO authority (Tex) ------------
EXEC_KINDS = ("meeseeks", "status", "deploy")
_REF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")   # statusboard's rule


def valid_exec_ref(ref):
    if not isinstance(ref, dict) or set(ref) - {"kind", "id", "run_id"}:
        return False
    if ref.get("kind") not in EXEC_KINDS:
        return False
    if not isinstance(ref.get("id"), str) or not _REF_ID_RE.match(ref["id"]):
        return False
    rid = ref.get("run_id")
    return rid is None or (isinstance(rid, str) and bool(_RUN_ID_RE.match(rid)))


def card_exec_ref(it):
    """The card's execution link: exec_ref, or the legacy meeseeks_pid
    alias. Used ONLY to locate execution facts -- never to authorise."""
    ref = it.get("exec_ref")
    if ref:
        return ref
    if it.get("meeseeks_pid"):
        return {"kind": "meeseeks", "id": it["meeseeks_pid"]}
    return None


# --- the transition table ----------------------------------------------------
# op: (legal FROM states, TO state or None = no state change, actor classes)
_ALL_OPEN = frozenset(OPEN_STATES)
_OPS = {
    "create":         (frozenset(), "BACKLOG",
                       {"operator", "scheduler", "agent"}),
    "assign":         (_ALL_OPEN, None, {"operator"}),
    "prioritize":     (_ALL_OPEN, None, {"operator"}),
    "link":           (_ALL_OPEN, None, {"operator", "executor"}),
    "queue":          (frozenset({"BACKLOG"}), "QUEUED", {"operator"}),
    "submit":         (frozenset({"BACKLOG"}), "AWAITING_APPROVAL",
                       {"operator", "agent"}),
    "approve":        (frozenset({"AWAITING_APPROVAL"}), "QUEUED",
                       {"operator"}),
    "reject":         (frozenset({"AWAITING_APPROVAL"}), "BACKLOG",
                       {"operator"}),
    # QUEUED only: there is still NO edge into ACTIVE that skips Gate 1.
    "start":          (frozenset({"QUEUED"}), "ACTIVE",
                       {"operator", "executor"}),
    "request_review": (frozenset({"QUEUED", "ACTIVE", "BLOCKED", "RETURNED"}),
                       "IN_REVIEW", {"operator", "executor", "agent"}),
    "block":          (frozenset({"QUEUED", "ACTIVE", "IN_REVIEW",
                                  "RETURNED"}), "BLOCKED",
                       {"operator", "executor"}),
    "resume":         (frozenset({"IN_REVIEW", "BLOCKED", "RETURNED"}),
                       "ACTIVE", {"operator", "executor"}),
    "return":         (frozenset({"ACTIVE", "IN_REVIEW"}), "RETURNED",
                       {"operator"}),
    "complete":       (_ALL_OPEN, "DONE", {"operator", "executor"}),
    "fail":           (_ALL_OPEN, "FAILED", {"operator", "executor"}),
    "close":          (_ALL_OPEN, "CLOSED", {"operator"}),
    "reopen":         (frozenset({"FAILED", "CLOSED"}), "BACKLOG",
                       {"operator"}),
}
OPS = tuple(_OPS)
# Ops an agent may NEVER perform, whatever else changes: agents may request
# review; they never close governed work (L-R4).
AGENT_FORBIDDEN = ("complete", "fail", "close", "approve", "reject",
                   "reopen", "queue", "start", "link", "assign",
                   "prioritize", "block", "resume", "return")
HISTORY_MAX = 50
# The legacy `phase` target -> op. in_progress is start from QUEUED, resume
# otherwise; resolved UNDER the lock so the choice cannot race.
_PHASE_OP = {"ready": "queue", "review": "request_review",
             "blocked": "block", "returned": "return"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require(cond, msg, exc=TransitionRefused):
    if not cond:
        raise exc(msg)


def _open_descendants(items, iid):
    out = []
    for ch in children_of(items, iid):
        if ch.get("status") == "open":
            out.append(ch)
        out.extend(_open_descendants(items, ch["id"]))
    return out


def _stamp(it, op, actor, frm, to, extra=None):
    """rev +1, updated, by, bounded history. The ONLY place these move."""
    rev = int(it.get("rev") or 0) + 1
    now = _now()
    it["rev"] = rev
    it["updated"] = now
    it["by"] = actor
    h = {"rev": rev, "at": now, "by": actor, "op": op, "from": frm, "to": to}
    if extra:
        h.update(extra)
    hist = list(it.get("history") or [])
    hist.append(h)
    it["history"] = hist[-HISTORY_MAX:]
    return h


def _set_state(it, state):
    if state in _PHASE_OF_STATE:
        it["status"] = "open"
        it["phase"] = _PHASE_OF_STATE[state]
    elif state == "DONE":
        it["status"] = "done"
        it["phase"] = None
    elif state in ("FAILED", "CLOSED"):
        it["status"] = "closed"
        it["phase"] = None


def _create(items, actor, cls, kind, text, parent=None, source=None,
            owner=None, approval_class=None, approval_required=False):
    if kind not in KINDS:
        raise BoardError(f"unknown kind {kind!r}")
    if source is None:
        source = "operator" if cls == "operator" else actor
    if not valid_source(source):
        raise BoardError(f"invalid source {source!r}")
    # Provenance follows the actor: an agent stamps only itself, Will
    # stamps only agent sources. Only the operator chooses freely.
    if cls == "agent":
        _require(source == actor, "agent %s may not create as %r"
                 % (actor, source), PermissionRefused)
    if cls == "scheduler":
        _require(source.startswith("agent:"), "will creates agent-sourced "
                 "items only, not %r" % source, PermissionRefused)
    if owner is not None and not _AGENT_RE.match(owner):
        raise BoardError(f"invalid owner {owner!r}")
    if approval_class is not None and approval_class not in APPROVAL_CLASSES:
        raise BoardError(f"invalid approval_class {approval_class!r}")
    if parent is None:
        if None not in PARENT_RULES[kind]:
            raise BoardError(f"{kind} requires a parent id")
    else:
        par = find(items, parent)
        if par is None:
            raise BoardError(f"no item with id {parent}")
        allowed = tuple(k for k in PARENT_RULES[kind] if k is not None)
        if par["kind"] not in allowed:
            raise BoardError(
                f"a {kind} cannot go under a {par['kind']}"
                + (f" (needs: {', '.join(allowed)})" if allowed
                   else " (takes no parent)"))
    item = {
        "id": next_id(items),
        "kind": kind,
        "text": text,
        "parent": parent,
        "status": "open",
        "created": _now(),
        "source": source,
        "owner": owner,
        "priority": 0,
        "phase": None,
        "meeseeks_pid": None,
    }
    if approval_class is not None:
        item["approval_class"] = approval_class
    if approval_required:
        item["approval_required"] = True
    _stamp(item, "create", actor, None, "BACKLOG")
    items.append(item)
    return item


def transition(iid, op, actor, expect_rev=None, **args):
    """THE board mutation. Validates op, actor class, from-state, guards and
    (optionally) expect_rev; applies; stamps rev/updated/by/history; saves
    -- all under board_lock(). No write on any refusal. Returns a result
    dict {id, op, from, to, rev[, receipt][, idempotent]}.

    exec_ref is DATA: it is recorded and used to find execution facts, and
    NOTHING here reads it to decide whether `actor` may act (Tex, L G2)."""
    if op == "phase":
        target = args.pop("phase", None)
        if target not in PHASES:
            raise BoardError(f"unknown phase {target!r}")
    elif op not in _OPS:
        raise BoardError(f"unknown op {op!r}")
    cls = actor_class(actor)
    if cls is None:
        raise PermissionRefused(f"unknown actor {actor!r}")
    with board_lock():
        items = load_board()
        if op == "create":
            _require(cls in _OPS["create"][2], "%s may not create" % actor,
                     PermissionRefused)
            item = _create(items, actor, cls, **args)
            save_board(items)
            return {"id": item["id"], "op": "create", "from": None,
                    "to": "BACKLOG", "rev": item["rev"]}
        it = find(items, iid)
        if it is None:
            raise BoardError(f"no item with id {iid}")
        cur = canonical_state(it)
        if op == "phase":
            op = (("start" if cur == "QUEUED" else "resume")
                  if target == "in_progress" else _PHASE_OP[target])
            if _OPS[op][1] == cur or cur not in _OPS[op][0]:
                raise TransitionRefused(
                    f"illegal phase transition {it.get('phase')!r} -> "
                    f"{target!r}")
        froms, to, allowed = _OPS[op]
        _require(cls in allowed and not (cls == "agent"
                                         and op in AGENT_FORBIDDEN),
                 f"{actor} may not {op}", PermissionRefused)
        rev = int(it.get("rev") or 0)
        if op == "approve":
            _require(expect_rev is not None, "approve is revision-bound: "
                     "give the rev you approved")
            # Idempotent: the same (id, rev) approved again returns the
            # receipt it already has, and writes nothing.
            if cur != "AWAITING_APPROVAL":
                for h in reversed(it.get("history") or []):
                    if h.get("op") == "approve" \
                            and h.get("approved_rev") == int(expect_rev):
                        return {"id": iid, "op": "approve", "from": h["from"],
                                "to": h["to"], "rev": rev,
                                "receipt": h.get("receipt"),
                                "idempotent": True}
        if expect_rev is not None and int(expect_rev) != rev:
            raise StaleRevision(f"card {iid} is at rev {rev}, "
                                f"not {expect_rev}")
        _require(cur in froms, f"{op} is not legal from {cur} (card {iid})")
        extra = {}
        linked = card_exec_ref(it) is not None
        if op == "assign":
            owner = args.get("owner")
            _require(owner is None or bool(_AGENT_RE.match(owner)),
                     f"invalid agent name {owner!r}")
            it["owner"] = owner
            extra["owner"] = owner
        elif op == "prioritize":
            it["priority"] = int(args.get("priority") or 0)
            extra["priority"] = it["priority"]
        elif op == "link":
            ref = args.get("exec_ref")
            _require(valid_exec_ref(ref), f"invalid exec_ref {ref!r}")
            if ref["kind"] == "meeseeks":
                _require(bool(_MEESEEKS_PID_RE.match(ref["id"])),
                         f"invalid meeseeks_pid {ref['id']!r}")
                it["meeseeks_pid"] = ref["id"]
            it["exec_ref"] = dict(ref)
            extra["exec_ref"] = dict(ref)
        elif op == "queue":
            _require(not requires_approval(it),
                     f"card {iid} requires operator approval (Gate 1): "
                     "submit, then approve(id, rev)")
        elif op == "submit":
            if cls == "agent":
                _require(it.get("owner") == actor[len("agent:"):],
                         f"{actor} may submit only its own card",
                         PermissionRefused)
            extra["submitted_rev"] = rev
        elif op == "approve":
            extra["approved_rev"] = rev
            extra["receipt"] = "approve:%s:%d" % (iid, rev)
        elif op == "reject":
            note = args.get("note")
            _require(bool(note), "reject needs a note")
            extra["note"] = str(note)[:500]
        elif op == "start":
            ref = args.get("exec_ref")
            if ref is not None:
                _require(valid_exec_ref(ref), f"invalid exec_ref {ref!r}")
                it["exec_ref"] = dict(ref)
                if ref["kind"] == "meeseeks":
                    it["meeseeks_pid"] = ref["id"]
            _require(card_exec_ref(it) is not None,
                     f"ACTIVE requires an exec_ref (card {iid} is not linked)")
            extra["exec_ref"] = card_exec_ref(it)
        elif op in ("request_review", "block") and cur == "QUEUED":
            # Crash-recovery edges: only a card that already owns execution.
            _require(linked, f"{op} from QUEUED needs a linked card")
        if op == "request_review" and cls == "agent":
            _require(cur == "ACTIVE" and it.get("owner")
                     == actor[len("agent:"):],
                     f"{actor} may request review only of its own ACTIVE "
                     "card", PermissionRefused)
        if op == "block":
            reason = args.get("reason")
            _require(bool(reason), "BLOCKED requires a reason")
            it["reason"] = str(reason)[:500]
            extra["reason"] = it["reason"]
        if op == "resume" and cur == "RETURNED":
            _require(cls == "operator", "only the operator resumes a "
                     "RETURNED card", PermissionRefused)
        if op in ("resume", "request_review") and it.get("reason") \
                and cur == "BLOCKED":
            it.pop("reason", None)
        closing = []
        if op == "complete":
            if cls == "executor":
                _require(cur not in ("BACKLOG", "AWAITING_APPROVAL"),
                         f"executor cannot complete a {cur} card")
            blockers = _open_descendants(items, iid)
            if blockers:
                if not args.get("cascade"):
                    raise ChildrenOpen(
                        f"id {iid} has {len(blockers)} open child item(s); "
                        "close them first or use --cascade")
                _require(cls == "operator", "only the operator cascades",
                         PermissionRefused)
                closing = blockers
            it["closed"] = _now()
            it.pop("reason", None)
        if op in ("fail", "close"):
            reason = args.get("reason")
            _require(bool(reason), f"{op} requires a reason")
            # A terminal parent never strands open children: close them
            # first (the migration runs children first for this reason).
            kids = _open_descendants(items, iid)
            if kids:
                raise ChildrenOpen(
                    f"id {iid} has {len(kids)} open child item(s); "
                    f"{op} them first")
            if op == "close":
                oc = args.get("outcome")
                _require(oc in CLOSE_OUTCOMES, f"close outcome must be one "
                         f"of {', '.join(CLOSE_OUTCOMES)}")
            else:
                oc = FAIL_OUTCOME
            it["outcome"] = oc
            it["reason"] = str(reason)[:500]
            if args.get("ref"):
                it["ref"] = str(args["ref"])[:200]
            it["closed"] = _now()
            extra.update(outcome=oc, reason=it["reason"])
            if it.get("ref"):
                extra["ref"] = it["ref"]
        if op == "reopen":
            if it.get("outcome") == FAIL_OUTCOME:
                it["last_failure"] = {"at": it.get("closed"),
                                      "reason": it.get("reason"),
                                      "exec_ref": card_exec_ref(it)}
            extra["prior_outcome"] = it.get("outcome")
            for k in ("outcome", "reason", "ref", "closed"):
                it.pop(k, None)
        if to is not None:
            _set_state(it, to)
        h = _stamp(it, op, actor, cur, to or cur, extra)
        for ch in closing:
            chcur = canonical_state(ch)
            ch["closed"] = it["closed"]
            _set_state(ch, "DONE")
            _stamp(ch, "complete", actor, chcur, "DONE",
                   {"cascade_from": iid})
        save_board(items)
        out = {"id": iid, "op": op, "from": cur, "to": to or cur,
               "rev": it["rev"]}
        if "receipt" in h:
            out["receipt"] = h["receipt"]
        return out


# ---------------------------------------------------------------------------
# Legacy library entry points -- thin wrappers over transition().
# ---------------------------------------------------------------------------

def _default_actor(source):
    """The CLI and un-attributed library callers ARE the operator's shell,
    except that an agent-sourced create is attributed to that agent (the
    dispatch board.task path)."""
    if isinstance(source, str) and source.startswith("agent:"):
        return source
    return "operator"


def add_item(kind, text, parent_id, source="operator", owner=None,
             actor=None):
    """Library entry point (Will posts through this): validate hierarchy
    and source, append, save. Returns the new id. Raises ValueError on
    any refusal -- no write on error."""
    if not valid_source(source):
        raise BoardError(f"invalid source {source!r}")
    r = transition(None, "create",
                   actor if actor is not None else _default_actor(source),
                   kind=kind, text=text, parent=parent_id, source=source,
                   owner=owner)
    return r["id"]


# Will's plate (L carried item f): owned, open, and still the agent's to
# act on. Work leaves a plate legitimately by review, block, approval
# wait, or by the operator completing/failing/closing it.
PLATE_STATES = ("BACKLOG", "QUEUED", "ACTIVE", "RETURNED")
REVIEW_CAP = 3


def plate(items, agent):
    """An agent's plate: open items owned by them that are still theirs to
    work. Emptiness of this list is Will's derive-your-own-work trigger."""
    return [it for it in items
            if it.get("owner") == agent
            and canonical_state(it) in PLATE_STATES]


def review_load(items, agent):
    """The agent's cards awaiting review (IN_REVIEW)."""
    return [it for it in items
            if it.get("owner") == agent
            and canonical_state(it) == "IN_REVIEW"]


def may_derive(items, agent):
    """Will derives only when the plate is empty AND fewer than REVIEW_CAP
    of the agent's cards sit in review -- an agent cannot empty its plate
    into review to farm derivations."""
    return not plate(items, agent) and \
        len(review_load(items, agent)) < REVIEW_CAP


# --- board.meta.json sidecar (L-R7): freshness/reconcile metadata ONLY ------
# Never a second board: a flat object of whitelisted scalar keys (plus one
# list of card ids). load_board() never reads it.
META_KEYS = ("reconciled_utc", "reconciled_epoch", "reconciler", "run_id",
             "linked_checked", "inconsistent", "faults", "board_sha256")


def meta_path():
    base, _ext = os.path.splitext(board_path())
    return base + ".meta.json"


def board_digest():
    import hashlib
    try:
        with open(board_path(), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def write_meta(actor, **fields):
    """Reconcile stamp. Executor or operator only; whitelisted keys; scalar
    values (or a list of int ids for `inconsistent`). Atomic, under the
    board lock so board_sha256 names exactly the board that was checked."""
    if actor_class(actor) not in ("executor", "operator"):
        raise PermissionRefused(f"{actor} may not write board meta")
    bad = set(fields) - set(META_KEYS)
    if bad:
        raise BoardError("meta keys not allowed: %s" % sorted(bad))
    for k, v in fields.items():
        if k == "inconsistent":
            if not (isinstance(v, list)
                    and all(isinstance(x, int) for x in v)):
                raise BoardError("inconsistent must be a list of card ids")
        elif not isinstance(v, (str, int, float, bool, type(None))):
            raise BoardError(f"meta value for {k} must be a scalar")
    with board_lock():
        doc = {"schema": "board-meta/1", "board": os.path.basename(
            board_path()), "written_by": actor}
        doc.update(fields)
        doc["board_sha256"] = board_digest()
        path = meta_path()
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".board-meta-", dir=d)
        os.fchmod(fd, 0o640)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return doc


def read_meta(path=None):
    """The sidecar as a dict, or None when absent/unreadable/not an object."""
    try:
        with open(path or meta_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _add(kind, text, parent_id, source, owner):
    """CLI funnel over add_item: uniform error path, prints the id."""
    try:
        iid = add_item(kind, text, parent_id, source=source, owner=owner)
    except ValueError as e:
        _fail(str(e))
    print(iid)
    return 0


def _fail(msg):
    """Uniform error path: message to stderr, nonzero exit, no write."""
    print(f"board: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Commands. One handler per command; registered in COMMANDS below.
# ---------------------------------------------------------------------------

def cmd_epic(args):
    return _add("epic", args.text, None, args.source, args.owner)


def cmd_feature(args):
    return _add("feature", args.text, args.parent_id, args.source, args.owner)


def cmd_story(args):
    return _add("story", args.text, args.parent_id, args.source, args.owner)


def cmd_task(args):
    return _add("task", args.text, args.under, args.source, args.owner)


def cmd_list(args):
    """Indented tree. Roots order by (source precedence, id): operator
    items first, then treebeard's, then agent-derived -- regardless of
    posting time. Children print under their parent in id order.
    Roots are items whose parent is None OR whose parent id is not in
    the shown set (orphans surface rather than vanish)."""
    items = load_board()
    shown = items if args.all else [it for it in items
                                    if it["status"] == "open"]
    ids = {it["id"] for it in shown}

    def line(it, depth):
        mark = {"done": "x", "closed": "-"}.get(it["status"], " ")
        own = f" @{it['owner']}" if it["owner"] else ""
        src = "" if it["source"] == "operator" else f" <{it['source']}>"
        print("  " * depth
              + f"[{mark}] {it['id']} ({it['kind']}) {it['text']}{own}{src}")

    def walk(it, depth):
        line(it, depth)
        for ch in sorted(children_of(shown, it["id"]), key=lambda i: i["id"]):
            walk(ch, depth + 1)

    roots = [it for it in shown
             if it["parent"] is None or it["parent"] not in ids]
    for it in sorted(roots, key=lambda i: (source_rank(i["source"]), -i.get("priority", 0), i["id"])):
        walk(it, 0)
    return 0


def cmd_plate(args):
    """Open items on one agent's plate, precedence-ordered. Prints
    nothing when empty (clean emptiness test for scripts and Will)."""
    if not _AGENT_RE.match(args.agent):
        _fail(f"invalid agent name {args.agent!r}")
    items = load_board()
    for it in sorted(plate(items, args.agent),
                     key=lambda i: (source_rank(i["source"]),
                                    -i.get("priority", 0), i["id"])):
        print(f"{it['id']} ({it['kind']}) {it['text']} <{it['source']}>")
    return 0


def _cli(fn, *a, **k):
    """CLI funnel: any lifecycle refusal is a clean stderr + exit 1."""
    try:
        return fn(*a, **k)
    except ValueError as e:
        _fail(str(e))


def cmd_assign(args):
    """Put an item on an agent's plate (or clear with --none)."""
    if not args.none and (not args.agent or not _AGENT_RE.match(args.agent)):
        _fail(f"invalid agent name {args.agent!r}")
    _cli(transition, args.id, "assign", "operator",
         owner=None if args.none else args.agent)
    return 0


def cmd_top(args):
    """Pin an item above its rank-peers (higher priority sorts first
    within the same source rank; rank itself never changes)."""
    def _pin():
        with board_lock():
            items = load_board()
            if find(items, args.id) is None:
                raise BoardError(f"no item with id {args.id}")
            pr = 0 if args.unpin else 1 + max(
                (i.get("priority", 0) for i in items), default=0)
            return transition(args.id, "prioritize", "operator", priority=pr)
    _cli(_pin)
    return 0


def mark_done(iid, cascade=False, actor="operator"):
    """Library entry point for completion -- the `complete` transition.
    done = SUCCESSFUL completion only (L-R2): status done, closed stamp,
    phase cleared, optional operator-only subtree cascade. Raises
    ChildrenOpen (a ValueError) on open descendants -- no write on error."""
    transition(iid, "complete", actor, cascade=cascade)
    return 0


def cmd_done(args):
    """Mark done. Refuses if the item has open children, unless
    --cascade, which closes the whole subtree with it."""
    return _cli(mark_done, args.id, cascade=args.cascade)


def cmd_clear(args):
    """Sweep done items (default) or nuke the board (--all). A done
    parent with open children is kept so the open children are never
    orphaned by a sweep.

    L-R2: NOT a governed operation. It is refused on the canonical board
    and survives only for fixture boards (TASKBOARD_PATH elsewhere)."""
    if os.path.realpath(board_path()) == os.path.realpath(DEFAULT_BOARD_PATH):
        _fail("clear is not a governed operation (BOARD-TRUTH L-R2); it "
              "runs only on fixture boards, never on %s" % DEFAULT_BOARD_PATH)
    with board_lock():
        items = load_board()
        if args.all:
            save_board([])
            return 0

        def has_open_descendant(iid):
            for ch in children_of(items, iid):
                if ch["status"] == "open" or has_open_descendant(ch["id"]):
                    return True
            return False

        kept = [it for it in items
                if it["status"] == "open" or has_open_descendant(it["id"])]
        save_board(kept)
    return 0


# ---------------------------------------------------------------------------
# Lifecycle phase (control-plane; MVP board<->meeseeks bridge). Board-native:
# it knows which phase follows which, NOT what any phase means to meeseeks.
# BOARD-TRUTH L: the legal edges now live in _OPS above (one table, one
# function). Every S-BRIDGE-1 edge survives as an op:
#   ready -> in_progress          start  (exec_ref REQUIRED)
#   ready -> review / blocked     request_review / block, CRASH-RECOVERY:
#     commission() writes the pid BEFORE the phase, so a crash between the
#     two leaves a linked card at `ready`. Linked cards only. Do not
#     "simplify" these away.
#   in_progress -> review/blocked/returned, review -> returned/in_progress/
#   blocked, returned -> in_progress/review/blocked, blocked -> in_progress/
#   review                        request_review / block / return / resume
# STILL DELIBERATELY ABSENT: any edge into `ready` except Gate 1 from None
# (and approve from awaiting_approval), any edge out of done.
# ---------------------------------------------------------------------------
PHASES = ("ready", "in_progress", "review", "returned", "blocked")
_MEESEEKS_PID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


def set_phase(iid, new_phase, actor="operator", reason=None):
    """Legacy phase setter over transition(op="phase"): legal edges only;
    illegal raises, no write. done is a status not a phase (complete).
    awaiting_approval is NOT settable here: only submit reaches it."""
    if new_phase not in PHASES:
        raise BoardError(f"unknown phase {new_phase!r}")
    kw = {}
    if new_phase == "blocked":
        kw["reason"] = reason or "unspecified (legacy phase command)"
    transition(iid, "phase", actor, phase=new_phase, **kw)
    return new_phase


def set_meeseeks_pid(iid, pid, actor="operator"):
    """Attach the durable meeseeks project pointer (the `link` op: an
    exec_ref of kind meeseeks). Validated, no write on error."""
    if not _MEESEEKS_PID_RE.match(pid or ""):
        raise BoardError(f"invalid meeseeks_pid {pid!r}")
    transition(iid, "link", actor, exec_ref={"kind": "meeseeks", "id": pid})
    return pid


def cmd_phase(args):
    _cli(set_phase, args.id, args.phase)
    return 0


def cmd_link(args):
    _cli(set_meeseeks_pid, args.id, args.pid)
    return 0


# ---------------------------------------------------------------------------
# Registry + argparse assembly. Add a command: one function, one entry.
# ---------------------------------------------------------------------------

def _common_add_flags(p):
    """Source and owner ride every add command. Source is trusted at the
    CLI because the shell IS the operator; Will stamps agent sources
    programmatically via add_item and never exposes this flag to models."""
    p.add_argument("--source", default="operator",
                   help="operator | treebeard | agent:<name>")
    p.add_argument("--owner", default=None, metavar="AGENT",
                   help="put the new item on this agent's plate")


COMMANDS = {
    "epic": (cmd_epic, "add an epic (no parent)",
             lambda p: (p.add_argument("text"), _common_add_flags(p))),
    "feature": (cmd_feature, "add a feature under an epic",
                lambda p: (p.add_argument("parent_id", type=int),
                           p.add_argument("text"), _common_add_flags(p))),
    "story": (cmd_story, "add a story under a feature",
              lambda p: (p.add_argument("parent_id", type=int),
                         p.add_argument("text"), _common_add_flags(p))),
    "task": (cmd_task, "add a generic task, optionally under anything",
             lambda p: (p.add_argument("text"),
                        p.add_argument("--under", type=int, default=None,
                                       metavar="ID"),
                        _common_add_flags(p))),
    "list": (cmd_list, "show the board as an indented tree",
             lambda p: p.add_argument("--all", action="store_true",
                                      help="include done items")),
    "plate": (cmd_plate, "open items on an agent's plate",
              lambda p: p.add_argument("agent")),
    "assign": (cmd_assign, "put an item on an agent's plate",
               lambda p: (p.add_argument("id", type=int),
                          p.add_argument("agent", nargs="?", default=None),
                          p.add_argument("--none", action="store_true",
                                         help="clear the owner"))),
    "top": (cmd_top, "pin an item above its rank-peers",
            lambda p: (p.add_argument("id", type=int),
                       p.add_argument("--unpin", action="store_true"))),
    "done": (cmd_done, "mark an item done",
             lambda p: (p.add_argument("id", type=int),
                        p.add_argument("--cascade", action="store_true",
                                       help="also close open children"))),
    "phase": (cmd_phase, "set a card lifecycle phase",
              lambda p: (p.add_argument("id", type=int),
                         p.add_argument("phase", choices=PHASES))),
    "link": (cmd_link, "attach a meeseeks project id to a card",
             lambda p: (p.add_argument("id", type=int),
                        p.add_argument("pid"))),
    "clear": (cmd_clear, "remove done items (--all removes everything)",
              lambda p: p.add_argument("--all", action="store_true",
                                       help="delete the entire board")),
}


def build_parser():
    ap = argparse.ArgumentParser(prog="board",
                                 description="hierarchical task board")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, (handler, help_text, setup) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        setup(p)
        p.set_defaults(func=handler)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
