"""board_writer.py -- the trusted writer of board.json (runs as tbctl).

CONTAINMENT T2 (review: T2 G2 PASS WITH CONDITIONS, 2026-10-02). The ONE
supported invocation is

    setpriv --reuid=tbctl --regid=tbctl --clear-groups --no-new-privs \
        /usr/bin/python3 -I -B /opt/treebeard/shared/ctl/board_writer.py <op> [args]

run by ROOT (the operator's existing sudo; /usr/local/sbin/tb-commission
does it for commissioning). No sudoers rule names tbctl, and tbctl has no
password or login shell, so only root can start this as tbctl.

Pattern = dispatch: fresh-read root-owned policy every call (fail-closed),
validate op and arg shape, then delegate the mutation to board.transition()
(which owns transition legality, the actor table and the atomic write).
A receipt is emitted for every outcome once the process is known to be
tbctl. The op set is fixed and closed. No generic path/write primitive and
no arbitrary Python/module/command execution.

What T2 changes (invariants I3-I4; I2 deferred, ruling P1 2026-10-04):
  * AUTHORITY IS ROOT-GATED AND RECORDED, NEVER SUPPLIED. Running as tbctl
    is the only authority fact, and only root can produce it. Operator
    identity is not yet separated from the service identity (Router 2.0),
    so nothing here can tell the operator from anyone else who reaches
    root: every receipt says authority "recorded", never "authenticated". No
    environment variable (SUDO_*), argument, flag or principal map is read
    as identity. `--source`, `--as` and any operator claim are refused as
    inputs. The recorded actor is `operator` (the board's role name).
  * NOTHING TREEBEARD-WRITABLE IS TRUSTED. The interpreter must be
    /usr/bin/python3 with -I -B; the environment is discarded; sys.path is
    rebuilt to the stdlib plus the two hard-coded root-owned code dirs; and
    every directory on it, every ancestor, every loaded module file and the
    policy files are checked for non-root write access (mode AND POSIX
    ACL) before any domain code runs. Any failure refuses.
  * Receipts carry the effective execution identity (tbctl), the derived
    invoker (root), the authority class ("recorded"), the recorded actor
    and the resulting card revision.

CONTAINMENT T2 revision 4, ruling B, as revision 8 (ruling P1):
commissioning is ONE root-run command, `sudo /usr/local/sbin/tb-commission
<card>`, from the operator's own session. It calls two ops here:
  * authorize-commission <card>  queues the card (BACKLOG -> QUEUED) if
    needed and appends the CONSUME receipt: the authorization, bound to
    (card, rev) and to the tb-commission run that asked for it (its pid and
    kernel start time). The binding names WHICH run, for single use; it is
    not identity and grants nothing. It is recorded here, in tbctl-owned
    state, BEFORE
    any project exists, so it is spent from that moment: if anything after
    it fails, the session ends and nothing can ever present it again.
  * commission-link <card> <consume>  the one board write the consume
    authorizes: link card-N and start (QUEUED -> ACTIVE) at the consumed
    revision, only from the SAME live tb-commission run, only if the
    consume is the card's latest and has never been used. Its receipt names
    the consume it spends.
A card is commissioned once: authorize-commission refuses a card the
receipts show was already commissioned, whatever board.json now says, so a
rewound board cannot recreate the authority. Recovery from a crash is a new
tb-commission run (a new authorization), never the old receipt.

`selfcheck` is the one diagnostic: it reports the interpreter flags, the
effective sys.path, the trust verdicts and every loaded module's file, and
writes nothing. It runs for any caller and never touches the board.
"""
import sys
import os

TRUSTED_PY = "/usr/bin/python3"
CTL = "/opt/treebeard/shared/ctl"
LIB = "/opt/treebeard/shared/lib"
BOARD = "/opt/treebeard/shared/board/board.json"
POLICY = "/etc/treebeard/board_ops.conf"
RECEIPTS = CTL + "/receipts/board.receipts"
EXEC_USER = "tbctl"

# The fixed, closed operation surface: T1's five plus the two commissioning
# ops of rev 4 ruling B (W adds the lifecycle ops). selfcheck is read-only
# and outside policy.
OPS = {"task", "list", "set-phase", "set-meeseeks_pid", "mark-done",
       "authorize-commission", "commission-link"}
COMMISSION_OPS = ("authorize-commission", "commission-link")
WRAPPER = "/usr/local/sbin/tb-commission"
WRAPPER_PY = "/usr/bin/python3"
# Ruling P1 (2026-10-04): the only authority class T2 has. Written into
# every receipt; consumers check it structurally, and it never means more
# than "root ran the writer".
PRINCIPAL = "root-gated"
AUTHORITY = "recorded"
ACTOR_BASIS = ("recorded: root-gated (only root can execute as tbctl); the "
               "operator is NOT authenticated -- operator identity is not yet "
               "separated from the service identity (Router 2.0)")
NOT_IMPLEMENTED = ("NOT_IMPLEMENTED: operator authentication deferred to "
                   "Router 2.0")


class Refused(Exception):
    pass


# --- 1. interpreter, environment and path discipline -----------------------

def interpreter_problems():
    f = sys.flags
    out = []
    for name in ("isolated", "ignore_environment", "no_user_site",
                 "safe_path", "dont_write_bytecode"):
        if not getattr(f, name, False):
            out.append("interpreter flag %s is not set (run with -I -B)" % name)
    # The PATH as invoked, not only its target: the venv's python3 is a
    # symlink to the same binary, but starting it runs the venv's (treebeard-
    # writable) site-packages .pth files before this code can check anything.
    if sys.executable not in (TRUSTED_PY, os.path.realpath(TRUSTED_PY)):
        out.append("interpreter %r is not %s" % (sys.executable, TRUSTED_PY))
    if sys.prefix != sys.base_prefix:
        out.append("running inside a virtualenv (%s)" % sys.prefix)
    return out


def scrub_environment():
    os.environ.clear()
    os.environ.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                       "TASKBOARD_PATH": BOARD})


def rebuild_sys_path():
    """stdlib entries only (no dist-packages), then CTL and LIB. Entries
    that do not exist are dropped rather than left as a later plant site."""
    stdlib_root = os.path.join(sys.base_prefix, "lib", "python")
    keep = [p for p in sys.path
            if p.startswith(stdlib_root) and "-packages" not in p
            and os.path.exists(p)]
    sys.path[:] = keep + [CTL, LIB]


def import_board():
    """The domain module, by its exact path. tbctl may traverse lib/ but not
    list it (R5: tbctl may traverse the library directory and read only
    board.py), and the
    path finder lists a directory before it imports from it, so a plain
    `import board` finds nothing there (rev 8.3)."""
    if "board" in sys.modules:
        return sys.modules["board"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("board", os.path.join(LIB, "board.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["board"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules["board"]
        raise
    return mod


# --- 2. trust: no non-root write access anywhere on the authority path -----

_ACL_USER, _ACL_GROUP, _ACL_MASK = 0x02, 0x08, 0x10


def _acl_writers(path):
    """Named ACL entries that grant write, after the mask. [] if no ACL."""
    try:
        raw = os.getxattr(path, "system.posix_acl_access")
    except OSError:
        return []
    import struct
    ents = [struct.unpack_from("<HHI", raw, 4 + 8 * i)
            for i in range((len(raw) - 4) // 8)]
    mask = next((perm for tag, perm, _id in ents if tag == _ACL_MASK), 7)
    return [("user" if tag == _ACL_USER else "group", _id) for tag, perm, _id in ents
            if tag in (_ACL_USER, _ACL_GROUP) and _id != 0
            and perm & mask & 2]


def untrusted_reason(path):
    """None if only root can modify `path` itself; else the reason."""
    try:
        st = os.stat(path)
    except OSError as e:
        return "cannot stat %s (%s)" % (path, e)
    if st.st_uid != 0:
        return "%s is owned by uid %d, not root" % (path, st.st_uid)
    if st.st_mode & 0o002:
        return "%s is world-writable" % path
    if st.st_mode & 0o020 and st.st_gid != 0:
        return "%s is writable by group gid %d" % (path, st.st_gid)
    w = _acl_writers(path)
    if w:
        return "%s grants write via ACL to %r" % (path, w)
    return None


def chain_problems(path):
    """Check `path` and every ancestor up to /."""
    out = []
    p = os.path.realpath(path)
    while True:
        r = untrusted_reason(p)
        if r:
            out.append(r)
        if p == "/":
            return out
        p = os.path.dirname(p)


def trust_problems(extra_files=()):
    out = []
    for p in sys.path:
        out += chain_problems(p)
        pc = os.path.join(p, "__pycache__")
        if os.path.isdir(pc):
            out += chain_problems(pc)
    out += chain_problems(TRUSTED_PY)
    out += chain_problems(os.path.abspath(__file__))
    for f in extra_files:
        out += chain_problems(f)
    for name, mod in sorted(sys.modules.items()):
        for attr in ("__file__", "__cached__"):
            f = getattr(mod, attr, None)
            if f and os.path.exists(f):
                r = untrusted_reason(f)
                if r:
                    out.append("module %s: %s" % (name, r))
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


# --- 3. identity: the execution identity only -----------------------------

def exec_identity():
    import pwd
    try:
        me = pwd.getpwnam(EXEC_USER)
    except KeyError:
        raise Refused("execution user %s does not exist" % EXEC_USER)
    if os.getuid() != me.pw_uid or os.geteuid() != me.pw_uid:
        raise Refused("not running as %s (uid=%d euid=%d): the writer runs "
                      "only as %s, which only root can start (%s)"
                      % (EXEC_USER, os.getuid(), os.geteuid(), EXEC_USER,
                         NOT_IMPLEMENTED))
    if os.getgid() != me.pw_gid or any(g != me.pw_gid for g in os.getgroups()):
        raise Refused("unexpected group credential for %s" % EXEC_USER)
    return {"uid": me.pw_uid, "user": EXEC_USER}


# --- 4. arguments: provenance never comes from here ------------------------

def parse(argv):
    """(op, params, actor). The actor is always `operator` (recorded); no
    caller can name another, and none is evidence of anything."""
    if not argv:
        raise Refused("no operation")
    op, rest = argv[0], list(argv[1:])
    if op not in OPS:
        raise Refused("unknown operation %r" % op)
    pos, flags = [], {}
    it = iter(rest)
    for a in it:
        if a in ("--source", "--actor", "--by", "--origin", "--as"):
            raise Refused("%s is not accepted: the writer derives nothing "
                          "from its caller" % a)
        if a in ("--under", "--reason"):
            v = next(it, None)
            if v is None:
                raise Refused("%s needs a value" % a)
            flags[a] = v
        elif a == "--cascade":
            flags[a] = True
        elif a.startswith("--"):
            raise Refused("unknown flag %r" % a)
        else:
            pos.append(a)
    actor = "operator"
    shapes = {"task": 1, "list": 0, "set-phase": 2, "set-meeseeks_pid": 2,
              "mark-done": 1, "authorize-commission": 1, "commission-link": 2}
    if len(pos) != shapes[op]:
        raise Refused("%s takes %d positional argument(s)" % (op, shapes[op]))
    allowed_flags = {"task": {"--under"}, "set-phase": {"--reason"},
                     "mark-done": {"--cascade"}}.get(op, set())
    for k in flags:
        if k not in allowed_flags:
            raise Refused("%s does not take %s" % (op, k))
    p = {"pos": pos, "flags": flags}
    for i, v in enumerate(pos):
        if op != "task" and i == 0 and not v.isdigit():
            raise Refused("card id must be a number")
    if op == "commission-link" and not _hex64(pos[1]):
        raise Refused("commission-link takes the consume receipt hash")
    if "--under" in flags and not flags["--under"].isdigit():
        raise Refused("--under must be a card id")
    return op, p, actor


def _hex64(v):
    return len(v) == 64 and all(c in "0123456789abcdef" for c in v)


# --- 4b. commissioning (rev 4 ruling B; rev 8) -------------------------------
# SESSION BINDING, not identity: which tb-commission run a consume belongs
# to, from kernel facts only (/proc/<pid>/status, cmdline, stat start time).
# The run is root's (real uid 0, as sudo starts it); that is the same
# root-gate the tbctl execution identity already is, and it says nothing
# about who started the root session (ruling P1).

def _proc_read(pid):
    """(ppid, ruid, argv list, starttime) or None."""
    try:
        with open("/proc/%d/status" % pid) as fh:
            f = dict(l.split(":", 1) for l in fh if ":" in l)
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            argv = [a.decode("utf-8", "replace")
                    for a in fh.read().split(bytes(1))[:-1]]
        with open("/proc/%d/stat" % pid) as fh:
            st = fh.read()
        start = int(st[st.rindex(")") + 2:].split()[19])
        return (int(f["PPid"].split()[0]), int(f["Uid"].split()[0]), argv,
                start)
    except (OSError, KeyError, ValueError, IndexError):
        return None


def commission_session(card, start_pid=None, read=_proc_read):
    """The tb-commission run this process descends from, for THIS card:
    {"pid", "start"} or None. It must run with real uid 0 and the exact
    command line the kernel builds from the wrapper's shebang:
    /usr/bin/python3 -IB /usr/local/sbin/tb-commission <card>."""
    want = [WRAPPER_PY, "-IB", WRAPPER, str(card)]
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(32):
        if pid <= 1:
            return None
        r = read(pid)
        if r is None:
            return None
        ppid, ruid, argv, start = r
        if ruid == 0 and argv == want:
            return {"pid": pid, "start": start}
        pid = ppid
    return None


def read_receipts(tbreceipt, path=None):
    import json
    path = RECEIPTS if path is None else path
    ok, n, bad = tbreceipt.verify_chain(path)
    if not ok:
        raise Refused("receipt chain BROKEN at record %s of %s" % (bad, n))
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _accepted(receipts, op, card):
    return [r for r in receipts if r.get("decision") == "accepted"
            and r.get("op") == op
            and (r.get("result") or {}).get("id") == card]


def commissioned(receipts, card):
    """The accepted commission-link receipts for `card`."""
    return _accepted(receipts, "commission-link", card)


def root_gated(r):
    """An accepted receipt written by the tbctl writer under the T2 (rev 8)
    authority class. Structural: it is "recorded", never "authenticated"."""
    idn = r.get("identity") or {}
    return (r.get("decision") == "accepted"
            and idn.get("exec_user") == EXEC_USER
            and idn.get("principal") == PRINCIPAL
            and idn.get("authority") == AUTHORITY
            and idn.get("actor") == "operator")


def consume_problem(receipts, card, digest, session):
    """None if `digest` is an unspent authorization for `card`, bound to
    `session`; else why not. The ONE check, shared with commission_op."""
    rec = next((r for r in receipts if r.get("hash") == digest), None)
    if rec is None:
        return "no receipt %s" % digest[:12]
    res = rec.get("result") or {}
    if rec.get("op") != "authorize-commission" or rec.get("decision") != "accepted":
        return "receipt %s is not an accepted authorize-commission" % digest[:12]
    if not root_gated(rec):
        return ("receipt %s is not a root-gated writer receipt (authority "
                "%r)" % (digest[:12], (rec.get("identity") or {}).get("authority")))
    if res.get("id") != card:
        return "receipt %s authorizes card %s, not %s" % (digest[:12],
                                                          res.get("id"), card)
    used = [r for r in commissioned(receipts, card)
            if (r.get("result") or {}).get("consumes") == digest]
    if used:
        return ("REUSE: authorization %s for card %s rev %s is SPENT (used "
                "by %s)" % (digest[:12], card, res.get("rev"),
                            used[0].get("hash", "")[:12]))
    latest = _accepted(receipts, "authorize-commission", card)[-1]
    if latest.get("hash") != digest:
        return ("REUSE: authorization %s for card %s is SPENT (superseded "
                "by %s)" % (digest[:12], card, latest.get("hash", "")[:12]))
    if session is None or res.get("session") != session:
        return ("REUSE: authorization %s for card %s rev %s is SPENT: it was "
                "bound to tb-commission run %s, and this is %s"
                % (digest[:12], card, res.get("rev"), res.get("session"),
                   session))
    return None


def apply(board, op, p, actor, ctx=None):
    """One transition. Returns (result dict | None, detail)."""
    pos, flags = p["pos"], p["flags"]
    if op in COMMISSION_OPS:
        return apply_commission(board, op, pos, ctx)
    if op == "list":
        rows = [{"id": i["id"], "kind": i.get("kind"), "status": i.get("status"),
                 "phase": i.get("phase"), "rev": i.get("rev")}
                for i in board.load_board()]
        return None, rows
    if op == "task":
        under = int(flags["--under"]) if "--under" in flags else None
        # source=None: board._create derives it from the actor class
        # (operator -> "operator"; agent:<x> -> itself). Never an input.
        r = board.transition(None, "create", actor, kind="task", text=pos[0],
                             parent=under, source=None)
        return r, "task created"
    iid = int(pos[0])
    if op == "set-phase":
        kw = {}
        if pos[1] == "blocked":
            kw["reason"] = flags.get("--reason") or "unspecified (board_writer set-phase)"
        r = board.transition(iid, "phase", actor, phase=pos[1], **kw)
        return r, "phase %s" % pos[1]
    if op == "set-meeseeks_pid":
        if not board._MEESEEKS_PID_RE.match(pos[1]):
            raise board.BoardError("invalid meeseeks_pid %r" % pos[1])
        r = board.transition(iid, "link", actor,
                             exec_ref={"kind": "meeseeks", "id": pos[1]})
        return r, "linked"
    if op == "mark-done":
        r = board.transition(iid, "complete", actor,
                             cascade=bool(flags.get("--cascade")))
        return r, "completed"
    raise Refused("unreachable op %r" % op)


def apply_commission(board, op, pos, ctx):
    """Under the board lock. ctx: receipts, session."""
    iid = int(pos[0])
    if ctx is None:
        raise Refused("%s needs its receipts and session" % op)
    if op == "commission-link":
        # Judge the consume FIRST, so a spent authorization presented from
        # anywhere -- in a session or not -- is recorded as REUSE.
        why = consume_problem(ctx["receipts"], iid, pos[1], ctx["session"])
        if why:
            raise Refused(why)
    if ctx.get("session") is None:
        raise Refused("%s runs only inside `sudo %s %s` (no ancestor run "
                      "of it as root): %s" % (op, WRAPPER, iid, NOT_IMPLEMENTED))
    it = board.find(board.load_board(), iid)
    if it is None or it.get("status") != "open":
        raise board.BoardError("card %s is not an open card" % iid)
    done = commissioned(ctx["receipts"], iid)
    pid = "card-%d" % iid
    if op == "authorize-commission":
        if done:
            raise Refused("card %s was already commissioned (receipt %s); a "
                          "card is commissioned once, whatever board.json now "
                          "says" % (iid, done[-1].get("hash", "")[:12]))
        if board.card_exec_ref(it) is not None:
            raise board.BoardError("card %s is already linked" % iid)
        frm = board.canonical_state(it)
        if frm == "BACKLOG":
            board.transition(iid, "queue", "operator")
        elif frm != "QUEUED":
            raise board.BoardError("card %s is %s: tb-commission takes a "
                                   "BACKLOG or QUEUED card" % (iid, frm))
        it = board.find(board.load_board(), iid)
        return ({"id": iid, "op": "authorize-commission", "from": frm,
                 "to": "QUEUED", "rev": it.get("rev"), "project": pid,
                 "session": ctx["session"]},
                "commissioning authorized and CONSUMED for card %s rev %s"
                % (iid, it.get("rev")))
    digest = pos[1]
    rec = next(r for r in ctx["receipts"] if r.get("hash") == digest)
    r = board.transition(iid, "start", "operator",
                         expect_rev=rec["result"]["rev"],
                         exec_ref={"kind": "meeseeks", "id": pid})
    r = dict(r, op="commission-link", consumes=digest, project=pid)
    return r, "commissioned: card %s -> %s (spends %s)" % (iid, pid, digest[:12])


# --- 4b. fail closed: receipt first, then the board (rev 8.4) ---------------

class _Unrecorded(Exception):
    """The accepted receipt (or the commit after it) failed: refused with no
    further receipt, board.json unchanged."""


def _board_bytes(items):
    """Exactly what board.save_board writes (json.dump, indent 2, newline)."""
    import json
    return (json.dumps(items, indent=2) + "\n").encode("utf-8")


class StagedBoard:
    """For one op, board.save_board keeps the transition's new board in
    memory instead of writing it (load_board serves it for the rest of the
    op). commit() writes it with the real save_board -- the caller does that
    only after the accepted receipt naming its hash is on disk. Root proof
    20261004T165620Z: the board changed although the receipt failed (I2-1)."""

    def __init__(self, board):
        self.board, self.items = board, None
        self._save, self._load = board.save_board, board.load_board

    def __enter__(self):
        import copy

        def save(items):
            self.items = copy.deepcopy(items)

        def load():
            if self.items is None:
                return self._load()
            return copy.deepcopy(self.items)
        self.board.save_board, self.board.load_board = save, load
        return self

    def __exit__(self, *exc):
        self.board.save_board, self.board.load_board = self._save, self._load
        return False

    def after_hash(self, before):
        if self.items is None:
            return before
        import hashlib
        return hashlib.sha256(_board_bytes(self.items)).hexdigest()

    def commit(self):
        self._save(self.items)


# --- 5. main --------------------------------------------------------------

def _sha_file(path):
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _args_hash(argv):
    import hashlib, json
    return hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()


def selfcheck(probs_before):
    import json
    loaded = {}
    try:
        import tbpolicy, tbreceipt  # noqa: F401
        import_board()
    except Exception as e:
        loaded["<import error>"] = "%s: %s" % (type(e).__name__, e)
    for name, mod in sorted(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f:
            loaded[name] = f
    out = {"interpreter": sys.executable, "version": sys.version.split()[0],
           "flags": {k: getattr(sys.flags, k) for k in (
               "isolated", "ignore_environment", "no_user_site", "safe_path",
               "dont_write_bytecode")},
           "sys_path": list(sys.path),
           "environ_keys": sorted(os.environ),
           "uid": os.getuid(), "euid": os.geteuid(),
           "interpreter_problems": probs_before,
           "trust_problems": trust_problems((POLICY,)),
           "modules": loaded}
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if not (probs_before or out["trust_problems"]
                     or "<import error>" in loaded) else 3


def main(argv):
    probs = interpreter_problems()
    scrub_environment()
    rebuild_sys_path()
    if argv[:1] == ["selfcheck"]:
        return selfcheck(probs)
    if probs:
        raise Refused("; ".join(probs))
    t = trust_problems((POLICY,))
    if t:
        raise Refused("untrusted authority path: " + "; ".join(t[:5]))
    ex = exec_identity()            # before any receipt: only tbctl writes them
    import tbpolicy, tbreceipt
    op = argv[0] if argv else ""
    # invoker: DERIVED (only root can start tbctl), recorded, not identity
    ident = {"exec_uid": ex["uid"], "exec_user": ex["user"],
             "invoker_uid": 0, "invoker_user": "root",
             "principal": PRINCIPAL, "authority": AUTHORITY,
             "actor": None, "actor_basis": ACTOR_BASIS}

    def receipt(decision, detail, result=None, before=None, after=None):
        try:
            return tbreceipt.append(RECEIPTS, op=op, decision=decision,
                                    caller_uid=ident["invoker_uid"],
                                    args_hash=_args_hash(argv), before_hash=before,
                                    after_hash=after, detail=detail,
                                    identity=dict(ident), result=result)
        except Exception as e:
            sys.stderr.write("board_writer: receipt not written (%s)\n" % e)
            return None

    try:
        op, p, actor = parse(argv)
        ident["actor"] = actor
        try:
            allowed = tbpolicy.load_ops(POLICY)
        except tbpolicy.PolicyError as e:
            raise Refused("policy: %s" % e)
        if op not in allowed:
            raise Refused("operation %r not in policy" % op)
    except Refused as e:
        receipt("denied", str(e))
        raise
    board = import_board()
    t = trust_problems((POLICY,))     # again, now that domain code is loaded
    if t:
        receipt("denied", "untrusted module after import: " + "; ".join(t[:3]))
        raise Refused("untrusted module after import: " + "; ".join(t[:3]))
    if os.path.realpath(board.board_path()) != os.path.realpath(BOARD):
        receipt("denied", "board path is not the canonical board")
        raise Refused("board path is not the canonical board")
    import json
    try:
        with board.board_lock():
            ctx = None
            if op in COMMISSION_OPS:
                ctx = {"receipts": read_receipts(tbreceipt),
                       "session": commission_session(int(p["pos"][0]))}
            before = _sha_file(BOARD)
            with StagedBoard(board) as staged:
                result, detail = apply(board, op, p, actor, ctx)
            after = staged.after_hash(before)
            # rev 8.4: FAIL CLOSED. Still under the lock, the accepted
            # receipt (naming the new board's hash) is written FIRST; only
            # then is the board committed. No receipt -> no board change.
            if op == "list":
                h = receipt("accepted", "read-only list", before=before, after=after)
            else:
                h = receipt("accepted", detail, result=result, before=before, after=after)
            if h is None:
                raise _Unrecorded("%s: the accepted receipt was not written; "
                                  "board.json NOT changed%s" % (op, (
                                      "; nothing is authorized"
                                      if op in COMMISSION_OPS else "")))
            if staged.items is not None:
                try:
                    staged.commit()
                except Exception as e:
                    receipt("error", "commit FAILED after accepted receipt %s: "
                            "%s: %s; board.json unchanged" % (h[:12], type(e).__name__, e),
                            result={"id": result.get("id"), "op": op, "voids": h}
                            if isinstance(result, dict) else {"op": op, "voids": h})
                    raise _Unrecorded("%s: board write failed after its receipt %s "
                                      "(%s: %s); board.json unchanged"
                                      % (op, h[:12], type(e).__name__, e))
    except _Unrecorded as e:
        raise Refused(str(e))
    except Refused as e:
        # commissioning denials name their card, for the box.writer sensor
        receipt("denied", str(e), result={"id": int(p["pos"][0]), "op": op}
                if op in COMMISSION_OPS else None)
        raise
    except board.BoardError as e:
        receipt("denied", "domain refused: %s" % e)
        raise Refused("domain refused: %s" % e)
    except Exception as e:
        receipt("error", "%s: %s" % (type(e).__name__, e))
        raise Refused("error: %s: %s" % (type(e).__name__, e))
    if op == "list":
        print(json.dumps(detail))
        return 0
    if op in COMMISSION_OPS:
        # The receipt IS the authorization (or its spending); it exists (h is
        # never None here) and is on disk before the board changed.
        result = dict(result, receipt=h)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Refused as e:
        sys.stderr.write("board_writer: REFUSED: %s\n" % e)
        sys.exit(2)
