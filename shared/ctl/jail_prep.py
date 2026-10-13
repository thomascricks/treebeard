#!/usr/bin/python3 -IB
"""jail_prep.py -- tbjail-side front end to the FROZEN jail launcher.

CONTAINMENT T2, invariant I1 (reviewer ruling 2026-10-02), revision 8 (operator
P2, 2026-10-04; rev 8.1 rule text approved by the operator): agent-generated
code executes as tbjail, not treebeard. The ONE supported invocation, and the
ONLY thing the one sudoers rule allows (token for token), is

    sudo -n -u tbjail /usr/bin/python3 -IB /opt/treebeard/shared/ctl/jail_prep.py request

with EVERY variable input on stdin: one JSON header line
    {"v": 1, "timeout": <s>, "mode": <mode>, "args": [<str>, ...]}
then a tar of the caller's cwd. Nothing variable reaches sudo, so the rule
has no wildcard; nothing here is parsed by a shell. Defence in depth: this
helper itself refuses unless it was started exactly as `/usr/bin/python3
-IB <this file> request` (the literal "-IB" token, not a venv).

P2 conditions, and where each is met:
  * fixed target tbjail: the sudoers Runas list is (tbjail) only, and this
    helper refuses any other real/effective/saved uid or gid;
  * env scrubbed: sudo-rs env_reset + NOSETENV, then -IB (no PYTHON*
    variable is read) and os.environ is cleared before anything else;
  * supplementary groups cleared: sudo sets tbjail's (none); verified empty
    here -- an unprivileged process cannot call setgroups, so it verifies;
  * capabilities dropped: a non-root uid holds none after sudo's switch;
    verified (CapInh/CapPrm/CapEff/CapAmb all zero), ambient cleared;
  * PR_SET_NO_NEW_PRIVS set, then verified (NoNewPrivs: 1);
  * the drop is irreversible: setuid(0)/setgid(0) must FAIL, else abort;
  * fail-closed, no fallback: every check aborts (rc 97) and runs nothing;
    there is no path to uid 1000 or root anywhere in this file;
  * optional extra isolation: if /usr/bin/bwrap is installed (root-owned),
    the launcher runs inside it with / read-only and only the run's scratch
    writable. It is additional, never a substitute, and a bwrap that cannot
    start fails the run. Absent bwrap, the uid boundary alone holds.

Why it exists (jail_launch.py stays byte-identical, governor-frozen):
  * the launcher needs a per-run scratch dir OWNED BY tbjail inside its
    SCRATCH_ROOT, and treebeard cannot create one;
  * the launcher accepts only `py_compile <file>` and `run <file>`, while
    the live callers use `-m unittest discover ...`, `-m unittest -v <mod>`
    and `<file> [args]`.
This helper, already running as tbjail, (1) verifies the credential and
locks it, (2) re-checks the identity with the launcher's own check, (3)
unpacks the tar into a fresh tbjail-owned scratch (regular files and
directories only; no links, no absolute or .. paths; size-capped), (4) maps
the mode onto a launcher sub-command, writing a fixed shim for the
unittest/args shapes, (5) execs the launcher in a child with the same
rlimits _jail_run always applied, enforces the wall clock ITSELF (a
treebeard parent cannot signal a tbjail process), (6) removes the scratch,
and exits with the child's status. stdout/stderr are the child's.
"""
import sys
import os

CTL = "/opt/treebeard/shared/ctl"
JAIL_LAUNCH = CTL + "/jail_launch.py"
PY = "/usr/bin/python3"
BWRAP = "/usr/bin/bwrap"
REQUEST_ARGV = ["request"]
HEADER_MAX = 8192
HEADER_KEYS = {"v", "timeout", "mode", "args"}
MAX_ARG_LEN = 256
MAX_TAR = 32 * 1024 * 1024
MAX_MEMBERS = 4000
MAX_FILE = 16 * 1024 * 1024
TIMEOUT_MAX = 900
STALE_S = 2 * 3600
MAX_ARGS = 16
SHIM_PREFIX = ".jail-shim-"
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 47, 4


def _abort(msg, code=97):
    sys.stderr.write("jail_prep ABORT: %s\n" % msg)
    sys.exit(code)


FLAGS_TOKEN = "-IB"          # the literal token the one jail rule names


def _flags_ok():
    """Started as `/usr/bin/python3 -IB <script> ...` -- the interpreter as
    invoked and the ONE literal flag token, exactly as the jail rule
    names them (no "-I -B", no extra flag) -- with -I and -B in force, and
    not a venv: the venv's python3 is a symlink to the same binary, but
    starting it runs the venv's site-packages .pth files first."""
    f = sys.flags
    orig = list(getattr(sys, "orig_argv", []))
    return all(getattr(f, n, False) for n in (
        "isolated", "ignore_environment", "no_user_site", "safe_path",
        "dont_write_bytecode")) and \
        sys.executable in (PY, os.path.realpath(PY)) and \
        sys.prefix == sys.base_prefix and \
        orig[:2] == [PY, FLAGS_TOKEN] and len(orig) == 2 + len(sys.argv)


def _launcher():
    sys.path[:] = [p for p in sys.path if "-packages" not in p] + [CTL]
    import jail_launch
    return jail_launch


# --- the credential: verify, lock, prove it cannot be undone -----------------

def proc_status(path="/proc/self/status"):
    out = {}
    with open(path) as fh:
        for line in fh:
            k, _, v = line.partition(":")
            out[k] = v.strip()
    return out


def credential_problems(want_uid, want_gid, status=None,
                        ids=None, groups=None):
    """[] if the process is exactly tbjail with no groups and no
    capabilities; else every problem found."""
    ruid, euid, suid = ids[0] if ids else os.getresuid()
    rgid, egid, sgid = ids[1] if ids else os.getresgid()
    out = []
    if (ruid, euid, suid) != (want_uid,) * 3:
        out.append("uids (real, effective, saved) = %r, not tbjail %d"
                   % ((ruid, euid, suid), want_uid))
    if (rgid, egid, sgid) != (want_gid,) * 3:
        out.append("gids (real, effective, saved) = %r, not tbjail %d"
                   % ((rgid, egid, sgid), want_gid))
    extra = [g for g in (os.getgroups() if groups is None else groups)
             if g != want_gid]
    if extra:
        out.append("supplementary groups present: %r" % extra)
    st = proc_status() if status is None else status
    for cap in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        v = st.get(cap)
        if v is None or int(v, 16) != 0:
            out.append("%s = %s, not 0" % (cap, v))
    return out


def lock_credential(want_uid, want_gid):
    """PR_SET_NO_NEW_PRIVS + clear ambient caps, then VERIFY; then prove
    root is out of reach. Aborts on any failure."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _abort("PR_SET_NO_NEW_PRIVS failed (errno %d)" % ctypes.get_errno())
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    st = proc_status()
    if st.get("NoNewPrivs") != "1":
        _abort("NoNewPrivs is %r after prctl, not 1" % st.get("NoNewPrivs"))
    bad = credential_problems(want_uid, want_gid, status=st)
    if bad:
        _abort("credential: " + "; ".join(bad))
    import pwd
    try:
        tb = pwd.getpwnam("treebeard").pw_uid
    except KeyError:
        tb = 1000
    for name, call in (("setuid(0)", lambda: os.setuid(0)),
                       ("setgid(0)", lambda: os.setgid(0)),
                       ("setresuid(treebeard)", lambda: os.setresuid(tb, tb, tb))):
        try:
            call()
        except PermissionError:
            continue
        _abort("%s SUCCEEDED: the drop is not irreversible" % name)


# --- the request: stdin only, strictly shaped --------------------------------

def read_header(stream):
    """The JSON header line -> (timeout, mode, args). ValueError if bad."""
    import json
    line = stream.readline(HEADER_MAX + 1)
    if not line.endswith(b"\n") or len(line) > HEADER_MAX:
        raise ValueError("request header missing, unterminated or over %d "
                         "bytes" % HEADER_MAX)
    try:
        h = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("request header is not ASCII JSON")
    if not isinstance(h, dict) or set(h) != HEADER_KEYS:
        raise ValueError("request header keys must be exactly %s"
                         % sorted(HEADER_KEYS))
    if h["v"] != 1 or isinstance(h["v"], bool):
        raise ValueError("request version %r unsupported" % (h["v"],))
    t = h["timeout"]
    if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= TIMEOUT_MAX:
        raise ValueError("timeout must be an integer 1..%d" % TIMEOUT_MAX)
    if not isinstance(h["mode"], str):
        raise ValueError("mode must be a string")
    a = h["args"]
    if (not isinstance(a, list) or len(a) > MAX_ARGS + 1
            or any(not isinstance(x, str) or not x.isascii() or "\x00" in x
                   or len(x) > MAX_ARG_LEN for x in a)):
        raise ValueError("args must be <= %d ASCII strings of <= %d chars"
                         % (MAX_ARGS + 1, MAX_ARG_LEN))
    return t, h["mode"], list(a)


def runs_dir(jl):
    return os.path.join(jl.SCRATCH_ROOT, "tbjail")


# --- runs/tbjail and the scratch: by FILE DESCRIPTOR, never by name (rev 8.2) --
# runs/ is treebeard's directory, so treebeard can rename or replace the
# runs/tbjail ENTRY at any moment. The directory is therefore opened ONCE
# (O_DIRECTORY|O_NOFOLLOW: a symlink or non-directory fails), fstat-checked
# (a directory, owned by tbjail, mode 0700), and from then on every scratch
# mkdir, directory open, file create, sweep and removal is relative to that
# fd (mkdirat/openat/unlinkat; O_CREAT|O_EXCL|O_NOFOLLOW for files). The
# name is never resolved again. The frozen launcher gets /proc/self/fd/<N>
# -- the held directory itself -- so its own path checks see the original
# directory, or fail closed if it was moved outside SCRATCH_ROOT.

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _dir_problem(st, want_uid):
    import stat
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != want_uid:
        return "not owned by tbjail"
    if stat.S_IMODE(st.st_mode) != 0o700:
        return "mode %o, not 700" % stat.S_IMODE(st.st_mode)
    return None


def open_runs_dir(path, want_uid):
    """(fd, None), or (None, why). The ONLY time the runs dir is named."""
    import errno
    try:
        fd = os.open(path, O_DIR)
    except FileNotFoundError as e:
        return None, "runs dir %s missing (%s)" % (path, e)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            return None, ("runs dir %s is not a directory (a symlink or other: %s)"
                          % (path, e.strerror))
        return None, "runs dir %s cannot be opened (%s)" % (path, e)
    why = _dir_problem(os.fstat(fd), want_uid)
    if why:
        os.close(fd)
        return None, "runs dir %s %s" % (path, why)
    return fd, None


def check_runs_dir(path, want_uid):
    """The same check, for callers that only want the verdict."""
    fd, why = open_runs_dir(path, want_uid)
    if fd is not None:
        os.close(fd)
    return why


def make_scratch(rfd, want_uid, rdir="runs dir"):
    """mkdirat a FRESH r-<random> in the held runs dir (never an existing
    entry), then open it by fd and fstat it. (name, scratch fd). `rdir` is
    for messages only: nothing here resolves it."""
    for _ in range(16):
        name = "r-" + os.urandom(8).hex()
        try:
            os.mkdir(name, 0o700, dir_fd=rfd)
        except FileExistsError:
            continue
        sfd = os.open(name, O_DIR, dir_fd=rfd)
        why = _dir_problem(os.fstat(sfd), want_uid)
        if why:
            os.close(sfd)
            raise OSError("new scratch %s/%s %s" % (rdir, name, why))
        return name, sfd
    raise OSError("no fresh scratch name in %s" % rdir)


def _open_dir_at(dfd, parts):
    """The directory dfd/parts..., each component created if missing and
    then opened O_NOFOLLOW relative to its parent fd. Caller closes."""
    fd = os.dup(dfd)
    try:
        for comp in parts:
            try:
                os.mkdir(comp, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(comp, O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_at(dfd, rel, data, mode=0o644):
    """Write a NEW file dfd/rel: O_CREAT|O_EXCL|O_NOFOLLOW, by fd."""
    parts = rel.split("/")
    pfd = _open_dir_at(dfd, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, mode, dir_fd=pfd)
    finally:
        os.close(pfd)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def _rm_at(dfd, name, depth=0):
    """Remove dfd/name, by fd: a non-directory (incl. a symlink) is
    unlinked; a directory is opened O_PATH|O_NOFOLLOW, made ours (0700),
    emptied through its fd, then rmdir'd. Best effort, never follows."""
    try:
        os.unlink(name, dir_fd=dfd)
        return
    except FileNotFoundError:
        return
    except (IsADirectoryError, PermissionError):
        pass
    if depth > 256:
        return
    pfd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        os.chmod("/proc/self/fd/%d" % pfd, 0o700)
        fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        for n in os.listdir(fd):
            try:
                _rm_at(fd, n, depth + 1)
            except OSError:
                pass
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dfd)


def sweep_stale(rfd, now):
    for name in os.listdir(rfd):
        try:
            if name.startswith("r-") and \
                    now - os.stat(name, dir_fd=rfd, follow_symlinks=False).st_mtime > STALE_S:
                _rm_at(rfd, name)
        except OSError:
            pass


def _safe_rel(name):
    if not name or "\x00" in name or name.startswith("/"):
        return None
    parts = [x for x in name.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    return "/".join(parts)


def unpack(data, scratch_fd):
    """Regular files and directories only, created relative to the held
    scratch fd (never by name). Returns the member count."""
    import io, tarfile
    if len(data) > MAX_TAR:
        raise ValueError("input exceeds %d bytes" % MAX_TAR)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf:
            n += 1
            if n > MAX_MEMBERS:
                raise ValueError("more than %d members" % MAX_MEMBERS)
            rel = _safe_rel(m.name)
            if rel is None:
                raise ValueError("unsafe member name %r" % m.name)
            if m.isdir():
                os.close(_open_dir_at(scratch_fd, rel.split("/")))
                continue
            if not m.isreg():
                raise ValueError("member %r is not a regular file" % m.name)
            if m.size > MAX_FILE:
                raise ValueError("member %r exceeds %d bytes" % (m.name, MAX_FILE))
            create_at(scratch_fd, rel, tf.extractfile(m).read())
    return n


def plan(mode, args, scratch):
    """(launcher sub-command, absolute path, shim source or None)."""
    import json, re
    if mode == "py_compile":
        if len(args) != 1 or _safe_rel(args[0]) is None:
            raise ValueError("py_compile takes one relative path")
        return "py_compile", os.path.join(scratch, _safe_rel(args[0])), None
    if mode == "unittest-discover":
        if args:
            raise ValueError("unittest-discover takes no arguments")
        argv = ["discover", "-s", ".", "-p", "test_*.py", "-v"]
        src = ("import sys, unittest\n"
               "sys.argv = ['python -m unittest'] + %s\n"
               "unittest.main(module=None)\n" % json.dumps(argv))
        return "run", None, src
    if mode == "unittest":
        if len(args) != 1 or not re.match(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$", args[0]):
            raise ValueError("unittest takes one module name")
        src = ("import sys, unittest\n"
               "sys.argv = ['python -m unittest'] + %s\n"
               "unittest.main(module=None)\n" % json.dumps(["-v", args[0]]))
        return "run", None, src
    if mode == "run":
        if not args or _safe_rel(args[0]) is None:
            raise ValueError("run takes a relative path")
        extra = args[1:]
        if len(extra) > MAX_ARGS or any(not a.isascii() for a in extra):
            raise ValueError("run args must be <=%d ASCII tokens" % MAX_ARGS)
        target = _safe_rel(args[0])
        src = ("import os, runpy, sys\n"
               "sys.argv = %s\n"
               "sys.path[0] = os.path.dirname(sys.argv[0]) or '.'\n"
               "runpy.run_path(sys.argv[0], run_name='__main__')\n"
               % json.dumps([target] + list(extra)))
        return "run", None, src
    raise ValueError("unknown mode %r" % mode)


def _rlimits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))


def bwrap_available(path=BWRAP):
    """True only for a root-owned, non-group/world-writable regular file."""
    import stat
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_uid == 0
            and not st.st_mode & 0o022)


def launcher_argv(scratch, sub, path, bwrap=False, scratch_fd=None):
    """The child argv. With bwrap: / read-only, fresh /dev /proc /tmp, and
    ONLY the run's scratch writable; new pid/ipc/uts namespaces. Network is
    not unshared (T2 claims no network isolation). With scratch_fd, bwrap
    mounts the HELD scratch directory (--bind-fd) at `scratch`, which is
    then only a mount destination inside the sandbox."""
    inner = [PY, FLAGS_TOKEN, JAIL_LAUNCH, scratch, sub, path]
    if not bwrap:
        return inner
    bind = (["--bind-fd", str(scratch_fd), scratch] if scratch_fd is not None
            else ["--bind", scratch, scratch])
    return [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/dev/shm"] + bind + [
            "--unshare-ipc", "--unshare-pid",
            "--unshare-uts", "--unshare-cgroup-try", "--new-session",
            "--die-with-parent", "--cap-drop", "ALL", "--chdir", scratch,
            "--"] + inner


def run_child(scratch, sub, path, timeout, bwrap=False, scratch_fd=None):
    """Fork, exec the launcher in its own session, enforce the wall clock.
    The held scratch fd is passed on (inheritable in the child only).
    Returns ('exit', code) or ('signal', signum) or ('timeout', None)."""
    import signal, time
    argv = launcher_argv(scratch, sub, path, bwrap, scratch_fd)
    pid = os.fork()
    if pid == 0:
        try:
            if scratch_fd is not None:
                os.set_inheritable(scratch_fd, True)
            os.setsid()
            _rlimits()
            dn = os.open(os.devnull, os.O_RDONLY)
            os.dup2(dn, 0)
            os.execv(argv[0], argv)
        except BaseException as e:
            os.write(2, ("jail_prep ABORT: exec failed: %s\n" % e).encode())
        os._exit(97)
    deadline = time.monotonic() + timeout
    while True:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            break
        if time.monotonic() >= deadline:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            os.waitpid(pid, 0)
            return "timeout", None
        time.sleep(0.02)
    if os.WIFSIGNALED(status):
        return "signal", os.WTERMSIG(status)
    return "exit", os.WEXITSTATUS(status)


def main(argv):
    if not _flags_ok():
        _abort("must run as %s %s" % (PY, FLAGS_TOKEN))
    if argv != REQUEST_ARGV:
        _abort("the only invocation is `jail_prep.py request` (input on stdin)")
    os.environ.clear()
    os.environ.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    jl = _launcher()
    want_uid, want_gid = jl._resolve_uid_gid()
    lock_credential(want_uid, want_gid)
    jl._enforce_identity()                       # the launcher's own check
    try:
        timeout, mode, args = read_header(sys.stdin.buffer)
    except ValueError as e:
        _abort("refused: %s" % e)
    rdir = runs_dir(jl)
    rfd, bad = open_runs_dir(rdir, want_uid)     # the ONLY use of the name
    if bad:
        _abort(bad)
    import time
    sweep_stale(rfd, time.time())
    os.umask(0o022)
    try:
        name, sfd = make_scratch(rfd, want_uid, rdir)
    except OSError as e:
        _abort("scratch: %s" % e)
    use_bwrap = bwrap_available()
    # what the launcher is told: the held directory itself (no name), or,
    # under bwrap, the mount destination the held fd is bound onto
    base = (os.path.join(rdir, name) if use_bwrap else "/proc/self/fd/%d" % sfd)
    outcome = ("exit", 97)
    try:
        try:
            unpack(sys.stdin.buffer.read(MAX_TAR + 1), sfd)
            sub, path, shim = plan(mode, args, base)
        except (ValueError, OSError) as e:
            _abort("refused: %s" % e)
        if shim is not None:
            shim_name = SHIM_PREFIX + os.urandom(6).hex() + ".py"
            create_at(sfd, shim_name, shim.encode("ascii"))
            path = base + "/" + shim_name
        sys.stdout.flush()
        sys.stderr.flush()
        outcome = run_child(base, sub, path, timeout, use_bwrap, sfd)
    finally:
        os.close(sfd)
        try:
            _rm_at(rfd, name)
        except OSError:
            pass
        os.close(rfd)
    kind, val = outcome
    if kind == "timeout":
        sys.stderr.write("\nKILLED: wall timeout\n")
        sys.exit(124)
    if kind == "signal":
        import signal
        if val not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(val, signal.SIG_DFL)
        os.kill(os.getpid(), val)          # same death as the child, as before
        sys.exit(128 + val)
    sys.exit(val)


if __name__ == "__main__":
    main(sys.argv[1:])
