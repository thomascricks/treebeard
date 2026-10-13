"""commission_op.py -- create (or adopt) the meeseeks project for ONE card
whose commissioning a root-run tb-commission has just authorized and consumed
(CONTAINMENT T2 revision 4, ruling B).

It is step 2 of the single root-run command `sudo /usr/local/sbin/tb-commission
<card>` and runs only from it; tb-commission starts it as treebeard with
setpriv (no sudoers rule):
    setpriv --reuid=treebeard --regid=treebeard --init-groups --no-new-privs \
        /usr/bin/python3 -I -B /opt/treebeard/shared/ctl/commission_op.py <card> <consume>

It runs as treebeard (meeseeks data is treebeard's) and takes nothing from
the caller but the card id and the consume receipt hash. It refuses unless:
  * it descends from the root tb-commission run for this card (an ancestor
    with real uid 0 and the wrapper's exact command line). That is session
    binding, not operator identity (ruling P1: deferred to Router 2.0);
  * the receipt store is root-owned, not writable here, and its chain
    verifies;
  * the consume is the card's latest accepted authorize-commission, a
    root-gated writer receipt (authority "recorded"), bound to THIS
    tb-commission run, and no
    commission-link has spent it (board_writer.consume_problem: one check,
    shared with the writer);
  * the card is open, QUEUED, unlinked and at the consumed revision.
It then creates project card-<N> and stops. It writes NO board state: the
link and the start are the writer's commission-link, with its receipt.

ADOPTION (T2 rev-6 ruling 1). If card-<N> already exists, it is adopted
ONLY if it is an UNTOUCHED EXACT TWIN of what this program would create from
the card right now: a reference project is built by the same meeseeks
cmd_new in a scratch ledger, and every ledger table row for the pid (minus
timestamps/sequence numbers) and the project's whole directory tree (paths,
types, file bytes) must be identical. Any difference, extra row, recorded
call, unexpected state or file, symlink, or anything that cannot be read is
a REFUSAL: nothing is adopted, normalised or repaired, the authorization is
spent, and tb-commission tells the operator which check failed and the
recovery (README, section Containment, Recovery).

T2 claim (the maximum; rescoped by ruling P1): commissioning requires
one-time root-gated action (recorded, NOT operator-authenticated) and binds
the card to its own unmodified project at commission time; project content
after commissioning is not protected until W (backlog LEDGER-CONTENT-1).

It is not a general entry: no other operation, no path, no environment.
Root-owned; treebeard can neither edit nor replace it.
"""
import os
import pwd
import sys

PY = "/usr/bin/python3"
CTL = "/opt/treebeard/shared/ctl"
LIB = "/opt/treebeard/shared/lib"
MEESEEKS_DIR = "/opt/treebeard/shared/meeseeks"
BOARD = "/opt/treebeard/shared/board/board.json"
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C",
       "HOME": pwd.getpwnam("treebeard").pw_dir,
       "TREEBEARD_BASE": "/opt/treebeard", "TASKBOARD_PATH": BOARD,
       "MEESEEKS_ROOT": "/opt/treebeard/shared/meeseeks/data",
       "MEESEEKS_LIB": LIB}
COMMISSIONER = "operator (tb-commission)"


def _die(msg):
    sys.stderr.write("commission_op: REFUSED: %s\n" % msg)
    sys.exit(2)


def store_problem(path):
    """None if only root-owned code can have written `path`."""
    for p in (path, os.path.dirname(path)):
        try:
            st = os.stat(p)
        except OSError as e:
            return "receipt store unreadable (%s)" % (e.strerror or e)
        if st.st_uid != 0:
            return "%s is not root-owned" % p
        if os.access(p, os.W_OK):
            return "%s is writable by this process" % p
    return None


def check(card, digest, bw, tbreceipt, board, session):
    """None if commissioning `card` under `digest` is authorized here."""
    if session is None:
        return ("not launched by `sudo %s %s` (no ancestor run of it as "
                "root): %s" % (bw.WRAPPER, card, bw.NOT_IMPLEMENTED))
    why = store_problem(bw.RECEIPTS)
    if why:
        return why
    try:
        receipts = bw.read_receipts(tbreceipt)
    except bw.Refused as e:
        return str(e)
    why = bw.consume_problem(receipts, card, digest, session)
    if why:
        return why
    rec = next(r for r in receipts if r.get("hash") == digest)
    it = board.find(board.load_board(), card)
    if it is None or it.get("status") != "open":
        return "card %s is not an open card" % card
    if board.canonical_state(it) != "QUEUED" or board.card_exec_ref(it):
        return "card %s is not QUEUED and unlinked" % card
    if it.get("rev") != rec["result"]["rev"]:
        return ("card %s is at rev %s, the authorization is for rev %s"
                % (card, it.get("rev"), rec["result"]["rev"]))
    return None


def main(argv):
    import pwd
    f = sys.flags
    if not (f.isolated and f.dont_write_bytecode) or \
            os.path.realpath(sys.executable) != os.path.realpath(PY):
        _die("run as %s -I -B" % PY)
    if os.getuid() != pwd.getpwnam("treebeard").pw_uid:
        _die("commissioning runs as treebeard (uid %d here)" % os.getuid())
    if len(argv) != 2 or not argv[0].isdigit() or len(argv[1]) != 64 \
            or any(c not in "0123456789abcdef" for c in argv[1]):
        _die("usage: commission_op.py <card> <consume receipt hash>")
    card, digest = int(argv[0]), argv[1]
    os.environ.clear()
    os.environ.update(ENV)
    sys.path[:] = [p for p in sys.path if "-packages" not in p] + [
        CTL, LIB, MEESEEKS_DIR]
    import board_writer as bw
    import tbreceipt
    import board
    session = bw.commission_session(card)
    import meeseeks
    try:
        out = commission(card, digest, bw, tbreceipt, board, meeseeks, session)
    except Refused as e:
        _die(str(e))
    import json
    print(json.dumps(out, sort_keys=True))
    return 0


class Refused(Exception):
    pass


VOLATILE = ("ts", "created", "updated", "seq")   # differ in any twin


def _task_file(text):
    """The card text exactly as commissioning writes it for cmd_new."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="commission-task-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="ascii", errors="replace") as fh:
        fh.write(text)
    return tmp


def _create(meeseeks, pid, text):
    import argparse
    import contextlib
    tmp = _task_file(text)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            meeseeks.cmd_new(argparse.Namespace(
                name=pid, by=COMMISSIONER, task=None, task_file=tmp))
    finally:
        os.unlink(tmp)


def ledger_rows(con, pid):
    """{table: sorted row reprs} for every ledger table with a pid column,
    timestamps and sequence numbers removed."""
    out = {}
    tables = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    for t in tables:
        cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
        if "pid" not in cols:
            continue
        keep = [c for c in cols if c not in VOLATILE]
        rows = con.execute('SELECT %s FROM "%s" WHERE pid=?' % (
            ", ".join('"%s"' % c for c in keep), t), (pid,)).fetchall()
        out[t] = sorted(repr(dict(zip(keep, tuple(r)))) for r in rows)
    return out


def project_tree(top):
    """{relative path: 'dir' | sha256} under `top`; a symlink, special file
    or unreadable entry raises (unverifiable)."""
    import hashlib
    import stat as st_
    st = os.lstat(top)
    if not st_.S_ISDIR(st.st_mode):
        raise ValueError("%s is not a directory" % top)
    def unreadable(e):
        raise e
    out = {}
    for dp, dns, fns in os.walk(top, onerror=unreadable):
        for n in sorted(dns + fns):
            full = os.path.join(dp, n)
            rel = os.path.relpath(full, top)
            m = os.lstat(full).st_mode
            if st_.S_ISLNK(m):
                raise ValueError("symlink %s in the project" % rel)
            if st_.S_ISDIR(m):
                out[rel] = "dir"
            elif st_.S_ISREG(m):
                with open(full, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
            else:
                raise ValueError("special file %s in the project" % rel)
    return out


def _first_difference(have, want):
    for k in sorted(set(have) | set(want)):
        if have.get(k) != want.get(k):
            return k, have.get(k), want.get(k)
    return None


def twin_problem(meeseeks, pid, text):
    """None if the existing project `pid` is an untouched exact twin of what
    _create(meeseeks, pid, text) makes now; else which check failed. Every
    error is a refusal ("unverifiable"). Writes nothing to the real ledger."""
    import shutil
    import tempfile
    scratch = tempfile.mkdtemp(prefix="commission-twin-")
    saved = os.environ.get("MEESEEKS_ROOT")
    try:
        try:
            real_rows = ledger_rows(meeseeks.connect(), pid)
            real_tree = project_tree(meeseeks.proj_dir(pid))
        except BaseException as e:
            return "unverifiable: the existing project could not be read (%s: %s)" % (
                type(e).__name__, e)
        try:
            os.environ["MEESEEKS_ROOT"] = os.path.join(scratch, "data")
            _create(meeseeks, pid, text)
            twin_rows = ledger_rows(meeseeks.connect(), pid)
            twin_tree = project_tree(meeseeks.proj_dir(pid))
        except BaseException as e:
            return "unverifiable: the reference twin could not be built (%s: %s)" % (
                type(e).__name__, e)
    finally:
        if saved is None:
            os.environ.pop("MEESEEKS_ROOT", None)
        else:
            os.environ["MEESEEKS_ROOT"] = saved
        shutil.rmtree(scratch, ignore_errors=True)
    first = ("projects", "sections")          # the most telling checks first
    for t in sorted(set(real_rows) | set(twin_rows),
                    key=lambda t: (first.index(t) if t in first else 9, t)):
        h, w = real_rows.get(t, []), twin_rows.get(t, [])
        if h == w:
            continue
        if len(h) != len(w):
            return ("ledger table %s has %d row(s) for %s; an untouched twin has %d"
                    % (t, len(h), pid, len(w)))
        import ast
        for hr, wr in zip(h, w):
            if hr != wr:
                d = _first_difference(ast.literal_eval(hr), ast.literal_eval(wr))
                return ("ledger table %s: %s is %r; an untouched twin has %r"
                        % (t, d[0], d[1], d[2]) if d else
                        "ledger table %s differs" % t)
    d = _first_difference(real_tree, twin_tree)
    if d:
        rel, have, want = d
        if want is None:
            return "unexpected %s in the project directory: %s" % (
                "directory" if have == "dir" else "file", rel)
        if have is None:
            return "missing from the project directory: %s" % rel
        return "project file %s differs from an untouched twin" % rel
    return None


def commission(card, digest, bw, tbreceipt, board, meeseeks, session):
    """check(), then create card-<N>, or adopt it ONLY as an untouched exact
    twin. No board write; a refusal writes nothing."""
    why = check(card, digest, bw, tbreceipt, board, session)
    if why:
        raise Refused(why)
    it = board.find(board.load_board(), card)
    pid = "card-%d" % card
    adopted = meeseeks.get_project(meeseeks.connect(), pid) is not None
    if adopted:
        why = twin_problem(meeseeks, pid, it["text"])
        if why:
            raise Refused(
                "NOT ADOPTED: project %s already exists and is NOT an untouched "
                "exact twin of card %d's authorized work -- %s. Nothing was "
                "adopted, changed or repaired; authorization %s is SPENT."
                % (pid, card, why, digest[:12]))
    else:
        _create(meeseeks, pid, it["text"])
    return {"card": card, "project": pid, "adopted": adopted,
            "consume": digest}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
