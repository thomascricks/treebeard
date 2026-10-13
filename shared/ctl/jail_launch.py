"""jail_launch.py -- fixed, root-owned, treebeard-unwritable jail launcher.

FROZEN privilege-transition mechanism (governor ruling):
    treebeard --(narrow NOPASSWD sudo: -u tbjail)--> THIS launcher --> tbjail exec
The invoking transition (sudo -u tbjail, or setpriv in the T1 test) performs the
uid/gid/group change; this launcher then ENFORCES the confinement invariants and
FAILS CLOSED on any violation. It never falls back to a treebeard-uid execution.

Invariants enforced here (each a hard abort, not a warning):
  * running identity MUST be uid=tbjail, gid=tbjail, supplementary groups EMPTY,
    and tbctl absent from every credential -- else abort (never proceed);
  * cwd is forced to the approved per-run scratch dir under JAIL_SCRATCH_ROOT,
    which must be owned by tbjail and lie inside the scratch root (no escape);
  * the child argv is constrained to a fixed shape: the pinned interpreter plus
    a whitelisted sub-command, every path arg resolved and proven INSIDE scratch;
  * environment is discarded and rebuilt to a minimal sanitized set;
  * anything unexpected aborts before exec.
"""
import os, sys, pwd, grp

PY = "/usr/bin/python3"                            # pinned real interpreter (T2 trust dep)
SCRATCH_ROOT = "/opt/treebeard/shared/toolbox/data/runs"   # tbjail-owned scratch tree
JAIL_USER = "tbjail"
FORBIDDEN_GROUP = "tbctl"

def _abort(msg):
    sys.stderr.write("jail_launch ABORT: %s\n" % msg)
    sys.exit(97)                                   # distinctive, never a fallback

def _resolve_uid_gid():
    try:
        pj = pwd.getpwnam(JAIL_USER)
    except KeyError:
        _abort("jail user %s does not exist" % JAIL_USER)
    return pj.pw_uid, pj.pw_gid

def _enforce_identity():
    want_uid, want_gid = _resolve_uid_gid()
    if os.getuid() != want_uid or os.geteuid() != want_uid:
        _abort("not running as %s (uid=%d euid=%d)" % (JAIL_USER, os.getuid(), os.geteuid()))
    if os.getgid() != want_gid or os.getegid() != want_gid:
        _abort("primary gid is not %s's gid (gid=%d)" % (JAIL_USER, os.getgid()))
    supp = os.getgroups()
    extra = [g for g in supp if g != want_gid]
    if extra:
        _abort("unexpected supplementary groups present: %r" % extra)
    try:
        forbidden = grp.getgrnam(FORBIDDEN_GROUP).gr_gid
        if forbidden in supp or forbidden == want_gid:
            _abort("forbidden group %s present in credential" % FORBIDDEN_GROUP)
    except KeyError:
        pass

def _sanitized_env(cwd):
    return {"PATH": "/usr/bin:/bin", "HOME": cwd, "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1"}

def _inside(root, path):
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)

def main(argv):
    if len(argv) < 2:
        _abort("usage: jail_launch <scratch_dir> <subcommand> [args]")
    scratch, sub, rest = argv[0], argv[1], argv[2:]
    _enforce_identity()
    if not _inside(SCRATCH_ROOT, scratch):
        _abort("scratch %r not inside %r" % (scratch, SCRATCH_ROOT))
    if not os.path.isdir(scratch):
        _abort("scratch %r is not a directory" % scratch)
    st = os.stat(scratch)
    want_uid, _ = _resolve_uid_gid()
    if st.st_uid != want_uid:
        _abort("scratch not owned by %s" % JAIL_USER)
    if sub == "py_compile":
        if len(rest) != 1 or not _inside(scratch, rest[0]):
            _abort("py_compile target must be one path inside scratch")
        child = [PY, "-m", "py_compile", rest[0]]
    elif sub == "run":
        if len(rest) != 1 or not _inside(scratch, rest[0]):
            _abort("run target must be one path inside scratch")
        child = [PY, rest[0]]
    else:
        _abort("subcommand %r not permitted" % sub)
    os.chdir(scratch)
    env = _sanitized_env(scratch)
    try:
        os.execve(PY, child, env)
    except OSError as e:
        _abort("exec failed: %s" % e)

if __name__ == "__main__":
    main(sys.argv[1:])
