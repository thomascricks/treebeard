#!/bin/bash
# provision-containment.sh -- Treebeard containment, Option C (operator-provisioned).
#   sudo bash install/provision-containment.sh --plan
#   sudo bash install/provision-containment.sh --apply --confirm=<token>
#   sudo bash install/provision-containment.sh --check
# install.sh NEVER calls this script. It provisions code execution for
# Meeseeks: ONE passwordless sudo transition, treebeard -> tbjail, for ONE
# fixed command; two login-less system accounts (tbctl, tbjail); the root-
# owned containment code under /opt/treebeard/shared/ctl; the root-only
# commissioning command /usr/local/sbin/tb-commission; the ptrace pin.
# Claim: "runtime-contained, not operator-authenticated" -- anyone who
# reaches root can commission; every receipt says "recorded".
#
# --plan    read-only: prints every change with its parameters, the host's
#           current-state facts, and the token (12 hex of the plan's sha256).
# --apply   recomputes the plan; refuses unless --confirm=<token> matches it.
#           Pre-flight P00-P20 run before any change (exit 2, "nothing
#           changed"); P21 is the first write (the before-snapshot). Steps
#           A1-A12 run in a fixed order, each verified before the next; any
#           failure stops the run (exit 1) and prints the rollback command.
# --check   read-only: NOT PROVISIONED (exit 3) | PROVISIONED (exit 0) |
#           INCONSISTENT: <check> (exit 1). It also verifies the service account's
#           `sudo -l` listing, the T2_MANIFEST pins, the PROVISIONED record and the
#           ptrace source. `sudo -l` invokes sudo and may therefore write a journal
#           entry; it does not execute the governed command. --check writes ONE thing:
#           after an entirely successful check it advances the board.receipts baseline
#           (root-only rollback state; RQ-1). A failed check writes nothing.
# Rollback: sudo bash install/rollback-containment.sh <receipt>
#
# It never: sets a password, creates a home or a shell, writes an SSH key or
# touches sshd/PAM/cron/systemd units/sockets, adds a group membership to an
# existing account, writes a sudo rule for tbctl, tbjail or root, runs
# `sysctl --system`, makes a network call, starts/stops/restarts a service,
# or changes anything under /home. No environment variable, flag or file can
# bypass a pre-flight check. No host-specific branch.
set -uo pipefail
umask 022
export LC_ALL=C PATH=/usr/sbin:/usr/bin:/sbin:/bin
SCRIPT=$(readlink -f "${BASH_SOURCE[0]}")
SRC=$(cd "$(dirname "$SCRIPT")/.." && pwd -P)

# ---- release constants (authored at freeze; P04 refuses any other bytes) ----
# path in the release tree | target | owner:group | mode | sha256
RELEASE=(
"shared/ctl/tbpolicy.py|/opt/treebeard/shared/ctl/tbpolicy.py|root:root|644|ae12526894831c8bb437909f42fa105a63412ec02cd3a023940a9a2bc043e9c3"
"shared/ctl/tbreceipt.py|/opt/treebeard/shared/ctl/tbreceipt.py|root:root|644|dd09224c3ebddd9c814608c2068de0088063b7eab1a6bd26ab80b3dba541fe5e"
"shared/ctl/mission_writer.py|/opt/treebeard/shared/ctl/mission_writer.py|root:root|644|5f046cddfcdd5da15a54ae5ccb98414afdeda38c919eb25f2bc97545cfb0a317"
"shared/ctl/lock_writer.py|/opt/treebeard/shared/ctl/lock_writer.py|root:root|644|ee1b8fd9afea7f428a82bb7a1c8b78a1a959a103e43373308b3e10c7050677df"
"shared/ctl/jail_launch.py|/opt/treebeard/shared/ctl/jail_launch.py|root:root|644|7e3c2eddeb814a0e5cfffd653d2515e42150fc0c0bc5496f5f615edb3573c3d0"
"shared/ctl/jail_prep.py|/opt/treebeard/shared/ctl/jail_prep.py|root:root|644|973d77bdb6c14021daf2a6802e2d58a42e903fdcc51b7655012812fe6ce0db43"
"shared/ctl/commission_op.py|/opt/treebeard/shared/ctl/commission_op.py|root:root|644|0df4bcb75c564ee57181169f85a11e9bc7b5943a4627a63069df8d7ddd45767c"
"shared/ctl/board_writer.py|/opt/treebeard/shared/ctl/board_writer.py|root:root|644|a48b627032dc45955e0147dacd3cb47f123c83f30a5cf081a388ca9a547d811e"
"install/containment/tb-commission|/usr/local/sbin/tb-commission|root:root|755|a5c853a9450ceef315abc9b176b1673a16dde668f33d097d5151fc061bb0b96e"
"install/containment/board_ops.conf|/etc/treebeard/board_ops.conf|root:root|644|e92a1b33d9eef695adb657351b15da2fed297f91a60f4b03d93d50fbd54ed419"
)
SUPPORTED_SUDO="sudo-rs >= 0.2.13; classic sudo >= 1.9.15"     # D14 / P06
SUDOERS_RULE='treebeard ALL=(tbjail) NOPASSWD:NOSETENV: /usr/bin/python3 -IB /opt/treebeard/shared/ctl/jail_prep.py request'
PIN_BODY='# Treebeard containment: the ptrace pin (provision-containment.sh A1).
# Named 99- so it is applied after every shipped sysctl file. ptrace_scope >= 1
# keeps other treebeard-uid processes from attaching to the root-started
# commissioning helpers. Removed only by rollback-containment.sh.
kernel.yama.ptrace_scope = 1'

# ---- fixed paths (C1 finding 2: the containment code hard-codes them) ------
TB=/opt/treebeard
SH=$TB/shared
CTL=$SH/ctl
ETC=/etc/treebeard
SBIN=/usr/local/sbin
SUDOERS_D=/etc/sudoers.d
SUDOERS_T=$SUDOERS_D/treebeard-containment
RCPT_D=/var/lib/treebeard-containment
RUNS=$SH/toolbox/data/runs
JAILDIR=$RUNS/tbjail
MS=$SH/meeseeks
# RQ-4 (final ruling 2026-10-09): meeseeks/data, created ONLY if absent, with the owner, group
# and mode of the working reference installation (recorded stat, packet
# RQ-4 meeseeks/data stat record: treebeard:treebeard 2775, no extended ACL)
MS_DATA=$MS/data
MS_DATA_OWNER=treebeard; MS_DATA_GROUP=treebeard; MS_DATA_MODE=2775
VENV=$SH/venv
TBUSER=treebeard
# >>> VERBATIM t2-deploy.sh lines 123-128 (sysctl bindings) sha256 81e3f3281409eae7ffa9341113cdfe44f2a6440a8bb0661ec6e3fdff189a3a2e
SYSCTLD=/etc/sysctl.d
SYSCTL_DIRS=(/etc/sysctl.d /run/sysctl.d /usr/local/lib/sysctl.d /usr/lib/sysctl.d /lib/sysctl.d)
SYSCTL_CONF=/etc/sysctl.conf
sysctl_ptrace() { cat /proc/sys/kernel/yama/ptrace_scope; }
sysctl_dump() { /usr/sbin/sysctl -a 2>/dev/null; }
sysctl_apply_file() { /usr/sbin/sysctl -p "$1"; }   # rev 8.8: ONE file, never --system
# <<< VERBATIM t2-deploy.sh lines 123-128
# >>> VERBATIM t2-deploy.sh lines 139-139 (inst) sha256 04f98cd97894cfd3629e2220fce2d5481d30da280e145d03c2de59622f2e1bfb
inst() { install -o "${1%%:*}" -g "${1##*:}" -m "$2" "$3" "$4"; }
# <<< VERBATIM t2-deploy.sh lines 139-139
# >>> VERBATIM t2-deploy.sh lines 142-142 (sudo_rules_text) sha256 54c01657b45a771f35d2cdeadef566f30e3dc1ff2f43b80e1a70889e141ca4ee
sudo_rules_text() { cat /etc/sudoers "$SUDOERS_D"/* 2>/dev/null; }
# <<< VERBATIM t2-deploy.sh lines 142-142
# >>> VERBATIM t2-deploy.sh lines 151-164 (acct_problems) sha256 f611152c546e4dd68dbd6552bb4c385f5f51c5bfe6b34fd1aa9baa6c77847b99
acct_problems() {
    local u=$1 e sh home st ak
    e=$(getent passwd "$u") || { echo "$u: no such account"; return 0; }
    sh=$(cut -d: -f7 <<< "$e"); home=$(cut -d: -f6 <<< "$e")
    [ "$sh" = /usr/sbin/nologin ] || echo "$u: login shell $sh (must be /usr/sbin/nologin)"
    st=$(passwd -S "$u" 2>/dev/null | awk '{print $2}')
    [ "$st" = L ] || echo "$u: password status '$st' (must be L: locked, no password)"
    [ "$(id -nG "$u")" = "$u" ] || echo "$u: supplementary groups: $(id -nG "$u")"
    [ ! -e "$home/.ssh" ] || echo "$u: $home/.ssh exists"
    ak=$(sshd -T -C "user=$u,host=localhost,addr=127.0.0.1" 2>/dev/null | awk '$1=="authorizedkeyscommand"{print $2}')
    [ -z "$ak" ] || [ "$ak" = none ] || echo "$u: sshd AuthorizedKeysCommand $ak"
    grep -qE "^[[:space:]]*$u[[:space:]]" <<< "$(sudo_rules_text)" && echo "$u: has a sudoers rule of its own"
    return 0
}
# <<< VERBATIM t2-deploy.sh lines 151-164
# >>> VERBATIM t2-deploy.sh lines 322-411 (T2-SYSCTL block) sha256 aa8795538091bc668b0044dba21f2512ef23f8aea825696c92111e42e8129815
# >>> T2-SYSCTL (rev 8.8) -- byte-identical in t2-deploy.sh, t2-rollback.sh and
# t2-g3-proof.sh (tests/test_t2_rev88.py). Nothing in this block applies a
# setting: the ONE apply is t2-deploy.sh's sysctl_apply_file binding
# (`sysctl -p <pin>`), and no T2 script runs `sysctl --system`.
# Precedence, as systemd-sysctl (boot) and procps `sysctl --system` apply it:
# every *.conf in SYSCTL_DIRS, one per basename (first dir wins), applied in
# basename order, then SYSCTL_CONF if it exists; for one key the LAST
# setting applied takes effect.
SYSCTL_KEY=kernel.yama.ptrace_scope
SYSCTL_PY=$(cat <<'PY'
import os, sys
mode, conf, dirs = sys.argv[1], sys.argv[2], sys.argv[3:]
chosen = {}
for d in dirs:
    try:
        names = os.listdir(d)
    except OSError:
        continue
    for n in names:
        if n.endswith(".conf") and n not in chosen:
            chosen[n] = os.path.join(d, n)
files = [chosen[n] for n in sorted(chosen)]
if os.path.isfile(conf):
    files.append(conf)
settings = []                      # (key pattern, value, file), applied order
for f in files:
    with open(f, errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s or s[0] in "#;" or "=" not in s:
                continue
            k, v = [x.strip() for x in s.lstrip("-").split("=", 1)]
            settings.append((k.replace("/", "."), v, f))
if mode == "key":                  # every setting of one key: "<value> <file>"
    for k, v, f in settings:
        if k == os.environ["SYSCTL_KEY"]:
            print(v, f)
elif mode == "after":              # settings of the key applied AFTER the pin
    pin = os.environ["PIN_NAME"]       # (codepoint order, as the tools sort)
    for k, v, f in settings:
        if k == os.environ["SYSCTL_KEY"] and (f == conf or os.path.basename(f) > pin):
            print(v, f)
PY
)
ptrace_cfg() { SYSCTL_KEY=$SYSCTL_KEY python3 -I -c "$SYSCTL_PY" key "$SYSCTL_CONF" "${SYSCTL_DIRS[@]}"; }
PIN_NAME=99-t2-ptrace.conf
ptrace_after_pin() { SYSCTL_KEY=$SYSCTL_KEY PIN_NAME=$PIN_NAME python3 -I -c "$SYSCTL_PY" after "$SYSCTL_CONF" "${SYSCTL_DIRS[@]}"; }
ptrace_effective() { ptrace_cfg | tail -1 | cut -d' ' -f2-; }
ptrace_cfg_without() { ptrace_cfg | grep -v " $1\$" || true; }
ptrace_ok() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ]; }
# rev 8.8: the LIVE kernel, EVERY key (`sysctl -a`), compared before/after.
# VOLATILE_KEYS change by themselves and are never compared (listed in the
# evidence): all are read-only (0444) kernel counters except ns_last_pid
# (the last pid handed out). Sampled on the box 2026-10-04 (5 x sysctl -a,
# 1 s apart); step 1 refuses if any OTHER key changes by itself. A key whose
# value changed, appeared or disappeared is a change; multi-line keys (e.g.
# dev.cdrom.info) are compared line for line.
VOLATILE_KEYS="fs.aio-nr fs.dentry-state fs.file-nr fs.inode-nr fs.inode-state fs.quota.allocated_dquots fs.quota.cache_hits fs.quota.drops fs.quota.free_dquots fs.quota.lookups fs.quota.reads fs.quota.syncs fs.quota.writes kernel.ns_last_pid kernel.pty.nr kernel.random.entropy_avail kernel.random.uuid net.netfilter.nf_conntrack_count"
LIVE_PY=$(cat <<'PY'
import sys
before, after, allowed, volatile = sys.argv[1], sys.argv[2], sys.argv[3], set(sys.argv[4].split())
def load(path):
    d = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            k, _, v = line.rstrip("\n").partition(" = ")
            d.setdefault(k.strip(), []).append(v)
    return d
b, a = load(before), load(after)
for k in sorted(set(b) | set(a)):
    if k in volatile or k == allowed or b.get(k) == a.get(k):
        continue
    show = lambda x: "ABSENT" if x is None else " | ".join(x)
    print("%s: %s -> %s" % (k, show(b.get(k)), show(a.get(k))))
PY
)
# a snapshot without the ptrace_scope line is not a snapshot: an empty or
# failed `sysctl -a` must never compare as "no change"
live_snapshot() { sysctl_dump > "$1" || true; grep -q "^$SYSCTL_KEY = " "$1"; }
live_changes() { python3 -I -c "$LIVE_PY" "$1" "$2" "${3-}" "$VOLATILE_KEYS"; }   # <before> <after> [allowed key]
# evidence lines for one comparison; returns 1 on any change but [allowed key]
live_report() {
    local chg
    chg=$(live_changes "$1" "$2" "${3-}") || { echo "  LIVE COMPARISON FAILED (fail closed)"; return 1; }
    echo "  live keys compared (sysctl -a): $(wc -l < "$1") lines before, $(wc -l < "$2") after${3:+; allowed to change: $3}"
    echo "  excluded as volatile (never compared): $VOLATILE_KEYS"
    [ -z "$chg" ] && { echo "  no other live value changed"; return 0; }
    echo "  LIVE CHANGE (fail closed):"; sed 's/^/    /' <<< "$chg"; return 1
}
# <<< T2-SYSCTL
# <<< VERBATIM t2-deploy.sh lines 322-411
# >>> VERBATIM t2-deploy.sh lines 414-434 (ptrace_is1, ptrace_pin_apply) sha256 d072fbb4540069a6aa45cb77890e77fec182d0825bac3dcd2adc6132199eb54a
ptrace_is1() { [ "$1" = 1 ]; }
# rev 8.8 (deploy only): apply the ONE pin file alone and compare every live
# key; prints the evidence; returns 1 if sysctl -p failed, any live key other
# than ptrace_scope changed, or the live value is < 1
ptrace_pin_apply() {   # <pin> <before-snapshot> <after-snapshot>
    local ar r=0 pv
    live_snapshot "$2" || { echo "  no usable live snapshot before the apply: NOT applied"; return 1; }
    pv=$(sed -n "s/^$SYSCTL_KEY = //p" "$2")
    ptrace_is1 "$pv" || { echo "  live $SYSCTL_KEY '$pv' before the apply is not exactly 1: NOT applied (the pin would change it)"; return 1; }
    sysctl_apply_file "$1" 2>&1 | sed 's/^/  sysctl -p: /'; ar=${PIPESTATUS[0]}
    live_snapshot "$3" || { echo "  no usable live snapshot after the apply"; r=1; }
    pv=$(sysctl_ptrace 2>/dev/null)
    echo "  applied: sysctl -p $1 (this file ONLY; rc=$ar)"
    live_report "$2" "$3" "$SYSCTL_KEY" || r=1
    echo "  $SYSCTL_KEY live: $(sed -n "s/^$SYSCTL_KEY = //p" "$2") before, $pv after"
    echo "  settings of $SYSCTL_KEY in application order (the LAST takes effect at boot):"
    ptrace_cfg | sed 's/^/    /'
    [ "$ar" = 0 ] || { echo "  sysctl -p failed"; r=1; }
    ptrace_is1 "$pv" || { echo "  live $SYSCTL_KEY '$pv' after the apply is not exactly 1"; r=1; }
    return $r
}
# <<< VERBATIM t2-deploy.sh lines 414-434
# matrix rows: t2-deploy.sh lines 210-254, 45 rows, sha256 af9de818ae25d8d46acd6eacec87b7a2da1e8aa79adaa9c9428edf2e9b7893dc

# ---- this script's own helpers ------------------------------------------------
PIN=$SYSCTLD/99-treebeard-ptrace.conf
PIN_NAME=99-treebeard-ptrace.conf            # (overrides the T2 block's name: this edition's pin)
SETID_ROOTS="/"
# DC-14 (ruling 2026-10-08 23:15): the SNAPSHOT-ONLY exclusion list for reboot-volatile
# values -- exactly the three ruled items, a constant distinct from the frozen 18
# VOLATILE_KEYS (which A1/R-2 use, byte-unchanged). Any addition needs a new ruling.
SNAPSHOT_REBOOT_VOLATILE="listener-pids kernel.random.boot_id net.ipv4.tcp_fastopen_key"
h() { sha256sum "$1" | cut -d' ' -f1; }
hs() { sha256sum | cut -d' ' -f1; }
refuse() { echo "REFUSED: $*"; echo "Nothing changed."; exit 2; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
ACT=""        # actions journal of this run (JSON lines), set at P21
fail() {
    echo "FAIL: $*"
    echo "Provisioning stopped. Completed steps are recorded in $ACT."
    [ -n "$ACT" ] && echo "To undo them: sudo bash $SRC/install/rollback-containment.sh --partial $ACT"
    exit 1
}
is_hex64() { [[ "$1" =~ ^[0-9a-f]{64}$ ]]; }
# one-line state of a path: absent | type owner group mode sha acl
pstate() {
    local p=$1 t s a
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then echo absent; return; fi
    t=$(stat -c %F "$p"); s=-
    [ "$t" = "regular file" ] || [ "$t" = "regular empty file" ] && s=$(h "$p")
    # -E: no "#effective:" annotations (a tab plus a comment) in the recorded ACL text
    a=$( { getfacl -cpPE "$p" 2>/dev/null || true; } | grep -E '^(user|group|mask|other|default):' | cut -f1 | tr '\n' ',')
    echo "type=${t// /_} owner=$(stat -c %U "$p") group=$(stat -c %G "$p") mode=$(stat -c %a "$p") sha=$s acl=$a"
}
# DC-2 (ruling 2026-10-08 23:15): the ONLY paths whose drift/rollback comparison is
# limited to type, owner, group, mode and EFFECTIVE permissions after the ACL mask --
# board.json (rewritten by the board writer) and the jail scratch. Emitted into the
# receipt. Every other recorded path: any difference is a refusal.
EXEMPT_PATHS=("/opt/treebeard/shared/board/board.json" "/opt/treebeard/shared/toolbox/data/runs/tbjail")
is_exempt() { local e; for e in "${EXEMPT_PATHS[@]}"; do [ "$1" = "$e" ] && return 0; done; return 1; }
# RQ-2 (final ruling 2026-10-09): the ONE owner exception -- board.json may be owned by
# treebeard OR tbctl (the writer replaces it as tbctl; D1 debt, documented). Group, mode and
# effective permissions stay exact; no third owner is accepted; no other path has one.
OWNER_EXCEPTIONS_PATH="/opt/treebeard/shared/board/board.json"
OWNER_EXCEPTIONS_OWNERS="treebeard tbctl"
# board.receipts (ruling 2026-10-09 08:05, option ii): APPEND-ONLY, a distinct compare kind,
# NOT part of EXEMPT_PATHS. At A12 the receipt records its content (length, sha256, bytes) and
# its identity (inode, device, birth time). --check/rollback accept it only if the current
# file is the SAME file (inode+device+birth), at least that long, and its first recorded-length
# bytes hash to the recorded sha256; owner, group, mode and ACL stay exact. Appended receipts
# are permitted. Limit, stated: content hashing cannot tell a replacement holding identical
# bytes from the original; the identity comparison does, except where the filesystem reports
# no birth time (0) AND a recreated file reuses the same inode number on the same device.
APPEND_ONLY_PATHS=("/opt/treebeard/shared/ctl/receipts/board.receipts")
# RQ-1 (final ruling 2026-10-09): the protected prefix is a BASELINE kept in root-only rollback
# state, $RCPT_D/receipts-baseline.json (root 0600 in the 0700 store). Initial baseline = the
# real content at provisioning (normally 0 bytes). A --check first verifies the current file
# against the latest baseline; only when the WHOLE check succeeds does the baseline advance to
# the current content. A failed check never advances it, so a checkpoint cannot legitimise
# tampering it detected. Rollback verifies against the latest committed baseline before any
# change. STATED LIMIT: content appended after the last successful checkpoint is not protected
# against truncation back to that baseline; with a 0-byte initial baseline, truncation before
# the first successful checkpoint is not detectable.
BASELINE_F=receipts-baseline.json
is_append_only() { local e; for e in "${APPEND_ONLY_PATHS[@]}"; do [ "$1" = "$e" ] && return 0; done; return 1; }
append_record() {   # <path> -> one JSON object: content + identity at this moment
    python3 -I -c 'import base64, hashlib, json, sys
p, ino, dev, birth = sys.argv[1:5]
d = open(p, "rb").read()
print(json.dumps({"path": p, "length": len(d), "sha256": hashlib.sha256(d).hexdigest(),
                  "bytes_b64": base64.b64encode(d).decode(), "inode": int(ino), "dev": int(dev), "birth": int(birth)}))' \
        "$1" "$(stat -c %i "$1")" "$(stat -c %d "$1")" "$(stat -c %W "$1")"
}
EFFECTIVE_PY=$(cat <<'PY'
def effective(state):
    """type owner group mode + effective ACL permissions (named and group-class entries
    AND the mask); the ACL text itself (which the writer's replace changes) is not compared"""
    if state == "absent":
        return state
    f = dict(x.split("=", 1) for x in state.split(" ") if "=" in x)
    ents = [e.split()[0] for e in f.get("acl", "").split(",") if e.strip()]   # tolerate annotations
    mask = next((e.split(":")[2] for e in ents if e.startswith("mask:")), None)
    out = []
    for e in ents:
        tag, q, perm = e.split(":")[-3:]
        if e.startswith("default:") or tag == "mask":
            continue
        if mask and (tag == "group" or q):
            perm = "".join(c if m != "-" else "-" for c, m in zip(perm, mask))
        out.append("%s:%s:%s" % (tag, q, perm))
    return "type=%s owner=%s group=%s mode=%s eff=%s" % (f.get("type"), f.get("owner"), f.get("group"), f.get("mode"), ",".join(sorted(out)))
PY
)
# DC-2 applied to whole-snapshot comparisons (rollback's final check, the proof): for an
# exempt path the stat line is reduced to type/owner/group/mode/effective permissions and its
# facl lines are not compared separately; every other line must be identical. Prints the
# differences; returns 1 if there are any.
snap_compare() {   # <before> <after>
    python3 -I - "$1" "$2" "$EFFECTIVE_PY" "${EXEMPT_PATHS[@]}" <<'PY'
import sys
exec(sys.argv[3])
exempt = set("path:" + x for x in sys.argv[4:])
def load(f):
    out = set()
    for l in open(f).read().splitlines():
        subj, kind, val = l.split("\t", 2)
        if subj in exempt and kind == "facl":
            continue
        if subj in exempt and kind == "stat":
            val = effective(val)
        out.add("%s\t%s\t%s" % (subj, kind, val))
    return out
b, a = load(sys.argv[1]), load(sys.argv[2])
for l in sorted(b - a):
    print("< " + l)
for l in sorted(a - b):
    print("> " + l)
sys.exit(1 if a != b else 0)
PY
}
ustate() {
    local u=$1 p g s
    p=$(getent passwd "$u") || { echo absent; return; }
    g=$(getent group "$u" || echo none); s=$(passwd -S "$u" 2>/dev/null | awk '{print $2}')
    echo "passwd=$p group=$g pwstatus=$s"
}
json_act() {   # step kind subject before after compare claims-json
    python3 -I -c 'import json, sys
a = sys.argv[1:]
print(json.dumps({"step": a[0], "kind": a[1], "subject": a[2], "before": a[3], "after": a[4],
                  "compare": a[5], "claims": json.loads(a[6]), "result": "ok"}, sort_keys=True))' "$@" >> "$ACT"
}
# DC-4 (ruling 2026-10-08 23:15): every grant-proof claim names the path (or principal),
# the change KIND, and the exact BEFORE and AFTER state of that kind.
#   paths:      create | delete | type | owner | group | mode | content | acl
#   principals: passwd | pwstat | sudo-l | group
CLAIMS_PY='import json, sys
FIELDS = (("type", "type"), ("owner", "owner"), ("group", "group"), ("mode", "mode"), ("content", "sha"), ("acl", "acl"))
def fields(st):
    return dict(x.split("=", 1) for x in st.split(" ") if "=" in x)
subj, b, a = sys.argv[1:4]
out = []
if b == a:
    pass
elif b == "absent":
    out.append({"subject": subj, "kind": "create", "before": "absent", "after": a})
elif a == "absent":
    out.append({"subject": subj, "kind": "delete", "before": b, "after": "absent"})
else:
    fb, fa = fields(b), fields(a)
    for kind, f in FIELDS:
        if fb.get(f) != fa.get(f):
            out.append({"subject": subj, "kind": kind, "before": fb.get(f), "after": fa.get(f)})
for extra in sys.argv[4:]:
    out.append(json.loads(extra))
print(json.dumps(out))'
claim() { python3 -I -c 'import json, sys; print(json.dumps({"subject": sys.argv[1], "kind": sys.argv[2], "before": sys.argv[3], "after": sys.argv[4]}))' "$@"; }
rec_path() {   # step kind path - before [extra claim json...]
    local s=$1 k=$2 p=$3 c b=$5 af; shift 5
    if is_append_only "$p"; then c=append-only; elif is_exempt "$p"; then c=exempt; else c=full; fi
    af=$(pstate "$p")
    json_act "$s" "$k" "path:$p" "$b" "$af" "$c" "$(python3 -I -c "$CLAIMS_PY" "path:$p" "$b" "$af" "$@")"
}
# the principal values exactly as snapshot() records them ("absent" when there is no line)
pv_passwd() { getent passwd "$1" || echo absent; }
pv_group()  { getent group "$1" || echo absent; }
pv_pwstat() { getent passwd "$1" >/dev/null && passwd -S "$1" 2>/dev/null | awk '{print $2}' || echo absent; }
pv_sudol()  { getent passwd "$1" >/dev/null && /usr/bin/sudo -n -l -U "$1" 2>&1 | hs || echo absent; }

as_tb() { setpriv --reuid=$TBUSER --regid=$TBUSER --init-groups env -i PATH=/usr/bin:/bin HOME=/tmp LC_ALL=C "$@"; }

sudo_version_ok() {   # D14 / P06: prints the version line; refuses anything unverified
    local v impl n
    v=$(/usr/bin/sudo --version 2>/dev/null | head -1)
    echo "$v"
    if [[ "$v" =~ ^sudo-rs[[:space:]]+([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
        n=$(printf '%d%04d%04d' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}")
        [ "$n" -ge "$(printf '%d%04d%04d' 0 2 13)" ]
    elif [[ "$v" =~ ^Sudo\ version\ ([0-9]+)\.([0-9]+)\.([0-9]+)(p[0-9]+)?$ ]]; then
        n=$(printf '%d%04d%04d' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}")
        [ "$n" -ge "$(printf '%d%04d%04d' 1 9 15)" ]
    else
        return 1
    fi
}

# ---- the K-style enumeration (C5.4): one line per fact, "<subject>\t<kind>\t<value>" --
PLAN_PATHS() {
    local r
    for r in "${RELEASE[@]}"; do IFS='|' read -r _ t _ <<< "$r"; echo "$t"; done
    printf '%s\n' "$CTL" "$CTL/receipts" "$CTL/receipts/board.receipts" "$CTL/T2_MANIFEST" "$CTL/PROVISIONED" \
        "$SUDOERS_T" "$PIN" "$RUNS" "$JAILDIR" "$SH" "$SH/lib" "$SH/lib/board.py" "$SH/board" \
        "$SH/board/board.json" "$SH/board/.board.lock" "$MS" "$MS_DATA" \
        "$SH/anvil" "$SH/toolkit" "$SH/toolbox" "$SH/toolbox/data" "$SH/toolbox/data/manifests"
    find "$MS" -mindepth 1 -maxdepth 1 2>/dev/null
}
snapshot() {   # <file>
    {
    getent passwd | while IFS= read -r l; do printf 'principal:%s\tpasswd\t%s\n' "${l%%:*}" "$l"; done
    getent group | while IFS= read -r l; do printf 'group:%s\tgroup\t%s\n' "${l%%:*}" "$l"; done
    passwd -S -a 2>/dev/null | while read -r u s _; do printf 'principal:%s\tpwstat\t%s\n' "$u" "$s"; done
    for f in /etc/sudoers "$SUDOERS_D"/*; do [ -f "$f" ] && printf 'path:%s\tsudoers\t%s %s\n' "$f" "$(h "$f")" "$(wc -l < "$f")"; done
    getent passwd | cut -d: -f1 | while read -r u; do
        printf 'principal:%s\tsudo-l\t%s\n' "$u" "$(/usr/bin/sudo -n -l -U "$u" 2>&1 | hs)"; done
    getfacl -R -P -p "$TB" "$ETC" "$SBIN" 2>/dev/null | awk '
        /^# file: /{p=substr($0,9); next} /^$/{next}
        /^# /{sub(/^# /,""); printf "path:%s\tfacl\t%s\n", p, $0; next}
        {printf "path:%s\tfacl\t%s\n", p, $0}'
    find $SETID_ROOTS -xdev -perm /6000 -type f -printf 'path:%p\tsetid\t%m %u %g\n' 2>/dev/null
    PLAN_PATHS | sort -u | while read -r p; do printf 'path:%s\tstat\t%s\n' "$p" "$(pstate "$p")"; done
    sysctl_dump | python3 -I -c 'import sys
vol = set(sys.argv[1].split()) | set(sys.argv[2].split())
for line in sys.stdin:
    k, _, v = line.rstrip("\n").partition(" = ")
    if k.strip() not in vol:
        print("sysctl:%s\tsysctl\t%s" % (k.strip(), v))' "$VOLATILE_KEYS" "$SNAPSHOT_REBOOT_VOLATILE"
    ss -ltnupH 2>/dev/null | { case " $SNAPSHOT_REBOOT_VOLATILE " in *" listener-pids "*) sed -E 's/pid=[0-9]+,//g' ;; *) cat ;; esac; } \
        | awk '{printf "listen:%s %s\tlisten\t%s\n", $1, $5, $0}'
    getent passwd | cut -d: -f6 | sort -u | while read -r d; do
        [ -e "$d/.ssh/authorized_keys" ] && printf 'path:%s\tauthkeys\t%s\n' "$d/.ssh/authorized_keys" "$(h "$d/.ssh/authorized_keys" 2>/dev/null || echo unreadable)"
    done
    systemctl list-unit-files --state=enabled --no-legend 2>/dev/null | awk '{printf "unit:%s\tunit\tenabled\n", $1}'
    } | LC_ALL=C sort -u > "$1"
}
# DC-4: the grant proof is a REPLAY. Starting from the before-snapshot, every action's
# claims (subject, kind, before, after) are applied in order; each claim's "before" must be
# the current value, and the result must be EXACTLY the after-snapshot. Any observed change
# no claim produces is UNRECORDED; any claim that does not apply is a MISMATCH; any action
# (other than a recorded no-op) without a claim has NO EFFECT. facl/sudoers lines are views
# of a path's state: they may differ only where that path's stat line (the full state) did.
grant_proof() {   # <before> <after> <actions.jsonl>
    python3 -I - "$@" <<'PY'
import json, sys
def load(f):
    d = {}
    for l in open(f).read().splitlines():
        subj, kind, val = l.split("\t", 2)
        d.setdefault((subj, kind), []).append(val)
    return {k: tuple(sorted(v)) for k, v in d.items()}
B, A = load(sys.argv[1]), load(sys.argv[2])
acts = [json.loads(l) for l in open(sys.argv[3]) if l.strip()]
ORDER = ("type", "owner", "group", "mode", "sha", "acl")
FIELD = {"type": "type", "owner": "owner", "group": "group", "mode": "mode", "content": "sha", "acl": "acl"}
PRINCIPAL = ("passwd", "pwstat", "sudo-l", "group")
def one(d, k):
    v = d.get(k)
    return "absent" if v is None else v[0]
bad, n_claims = [], 0
state = {}              # (subject, kind) -> current value, kind "stat" for paths
for i, x in enumerate(acts):
    if x.get("compare") != "noop" and not x["claims"]:
        bad.append("NO EFFECT %s %s %s" % (x["step"], x["kind"], x["subject"]))
    for c in x["claims"]:
        n_claims += 1
        subj, kind, cb, ca = c["subject"], c["kind"], c["before"], c["after"]
        if kind in PRINCIPAL:
            key = (subj, kind)
            cur = state.get(key, one(B, key))
            if cur != cb:
                bad.append("MISMATCH %s %s %s: claimed before [%s], current [%s]" % (x["step"], subj, kind, cb, cur)); continue
            state[key] = ca
            continue
        key = (subj, "stat")
        cur = state.get(key, one(B, key))
        if kind == "create":
            ok = cur == "absent"; new = ca
        elif kind == "delete":
            ok = cur == cb; new = "absent"
        elif kind in FIELD and cur != "absent":
            f = dict(y.split("=", 1) for y in cur.split(" ") if "=" in y)
            ok = f.get(FIELD[kind]) == cb
            f[FIELD[kind]] = ca
            new = " ".join("%s=%s" % (k, f[k]) for k in ORDER if k in f)
        else:
            ok, new = False, cur
        if not ok:
            bad.append("MISMATCH %s %s %s: claimed before [%s], current [%s]" % (x["step"], subj, kind, cb, cur)); continue
        state[key] = new
changed = {k for k in set(B) | set(A) if B.get(k) != A.get(k)}
stat_changed = {k[0] for k in changed if k[1] == "stat"}
for key in sorted(changed):
    subj, kind = key
    if kind == "stat" or kind in PRINCIPAL:
        if key not in state:
            bad.append("UNRECORDED %s %s: [%s] -> [%s]" % (subj, kind, one(B, key), one(A, key)))
    elif kind in ("facl", "sudoers") and subj in stat_changed:
        continue
    else:
        bad.append("UNRECORDED %s %s: %s -> %s" % (subj, kind, list(B.get(key, ())), list(A.get(key, ()))))
for key, val in sorted(state.items()):
    if val != one(A, key):
        bad.append("REPLAY MISMATCH %s %s: replayed [%s], after-snapshot [%s]" % (key[0], key[1], val, one(A, key)))
print("grant proof (replay): %d changed facts, %d actions, %d claims, %d unrecorded, %d mismatches, %d actions without effect"
      % (len(changed), len(acts), n_claims, sum(b.startswith("UNRECORDED") for b in bad),
         sum("MISMATCH" in b for b in bad), sum(b.startswith("NO EFFECT") for b in bad)))
for b in bad:
    print("  " + b)
sys.exit(1 if bad else 0)
PY
}

# ---- facts and the plan --------------------------------------------------------
sysctl_digest() { sysctl_dump | python3 -I -c 'import sys
vol = set(sys.argv[1].split())
for line in sys.stdin:
    k = line.partition(" = ")[0].strip()
    if k not in vol:
        sys.stdout.write(line)' "$VOLATILE_KEYS" | hs; }
plan_text() {
    local r rel t own mode sha
    echo "TREEBEARD CONTAINMENT PLAN (provision-containment.sh sha256 $(h "$SCRIPT"))"
    echo "host facts (the token binds this plan to this state):"
    echo "  accounts: passwd $(getent passwd | hs | cut -c1-16)  group $(getent group | hs | cut -c1-16)"
    echo "  sudoers text $(sudo_rules_text | hs | cut -c1-16); treebeard listing $(/usr/bin/sudo -n -l -U $TBUSER 2>&1 | hs | cut -c1-16)"
    echo "  sudo: $(/usr/bin/sudo --version 2>/dev/null | head -1) (supported: $SUPPORTED_SUDO)"
    echo "  kernel.yama.ptrace_scope live $(sysctl_ptrace 2>/dev/null); configured: $(ptrace_cfg | tr '\n' ';')"
    echo "  sysctl -a (minus the 18 frozen VOLATILE_KEYS) $(sysctl_digest | cut -c1-16)"
    echo "  receipt store $RCPT_D: $([ -e "$RCPT_D" ] && ls -1 "$RCPT_D" | tr '\n' ' ' || echo absent)"
    PLAN_PATHS | sort -u | while read -r p; do echo "  state $p: $(pstate "$p")"; done
    echo "changes, in this order (each verified before the next):"
    echo "  A1  ptrace pin ALONE: $PIN root:root 644 sha256 $(printf '%s\n' "$PIN_BODY" | hs); sysctl -p $PIN (never --system); every live key compared"
    echo "  A2  accounts: useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin tbctl; usermod -L tbctl; the same for tbjail"
    echo "  A3  install -d -o root -g root -m 755 $CTL; 8 files root:root 644:"
    for r in "${RELEASE[@]}"; do IFS='|' read -r rel t own mode sha <<< "$r"
        case $t in "$CTL"/*) echo "        $t  $sha";; esac; done
    echo "  A4  install -d -o root -g tbaudit -m 2750 $CTL/receipts; $CTL/receipts/board.receipts root:tbaudit 0640 (empty)"
    for r in "${RELEASE[@]}"; do IFS='|' read -r rel t own mode sha <<< "$r"
        case $t in "$CTL"/*) ;; *) echo "  A5  install -o ${own%%:*} -g ${own##*:} -m $mode $rel -> $t  $sha";; esac; done
    echo "  A6  jail scratch: $RUNS treebeard:treebeard 755 (only if absent); $JAILDIR tbjail:tbjail 700"
    echo "  A7  ACLs (10 named entries, 1 of them default): u:tbctl:--x,u:tbjail:--x $SH; u:tbctl:--x $SH/lib;"
    echo "        u:tbctl:r-- $SH/lib/board.py; u:tbctl:rwx + default u:tbctl:rw- $SH/board; u:tbctl:r-- $SH/board/board.json;"
    echo "        $SH/board/.board.lock treebeard:treebeard 0660 (only if absent) + u:tbctl:rw-; u:tbctl:--x $CTL/receipts;"
    echo "        u:tbctl:rw- $CTL/receipts/board.receipts"
    echo "  A8  none: $MS and its existing top-level entries are not re-owned or re-moded (operator ruling 2026-10-09 U4/U5)"
    echo "  A8b $MS_DATA $MS_DATA_OWNER:$MS_DATA_GROUP $MS_DATA_MODE, ONLY if absent (an existing data/ is never modified; meeseeks/ itself is not changed by A8b)"
    echo "  A9  $CTL/T2_MANIFEST root:root 644: sha256 of the 8 ctl files, lib/board.py, board_ops.conf, tb-commission; recorded $SUDOERS_T"
    echo "  A10 sudoers LAST: $SUDOERS_T root:root 0440, visudo -c -f then visudo -c; the ONE rule:"
    echo "        $SUDOERS_RULE"
    echo "  A11 live verification: the 45-row J19 argv matrix (sudo's verdict from its own journal); C3 checks (a)-(c) hold as treebeard; board_writer selfcheck as tbctl"
    echo "  A12 after-snapshot; its difference from the before-snapshot must equal this run's recorded actions; receipt; $CTL/PROVISIONED root:root 644; then the C3 state as treebeard must be PROVISIONED"
    echo "D7: $SH/anvil, $SH/toolkit, $SH/toolbox, $SH/toolbox/data and $SH/toolbox/data/manifests are NOT re-owned or re-moded."
    echo "    The only paths created under toolbox/data are $RUNS (if absent) and $JAILDIR (A6)."
    echo "Grants exactly: 2 system accounts (no login, password, home, shell or SSH); one NOPASSWD rule treebeard -> tbjail for one argv;"
    echo "  no rule for tbctl, tbjail or root; no group membership change; no sysctl other than ptrace_scope (pinned at 1, its live value)."
}

# ---- pre-flight (all before any change) ------------------------------------------
PF=()
pf() { PF+=("$1: $2"); }
preflight() {
    local r rel t own mode sha got v e
    # P00 root
    { [ "$(id -u)" = 0 ] && [ "$(id -ru)" = 0 ]; } || refuse "P00: run as root (sudo bash $SCRIPT ...)"
    # P01 serialised (the same lock as the T2 scripts)
    exec 9</opt; flock -n 9 || refuse "P01: another containment/T2 run holds the lock on /opt"
    # P03 fixed base and user: no override is honoured, any other value refuses
    if [ -n "${TB_BASE+x}" ] && [ "$TB_BASE" != /opt/treebeard ]; then pf P03 "TB_BASE=$TB_BASE: the containment code hard-codes /opt/treebeard (no relocation)"; fi
    if [ -n "${TB_USER+x}" ] && [ "$TB_USER" != treebeard ]; then pf P03 "TB_USER=$TB_USER: the containment code hard-codes the service account treebeard"; fi
    [ -d "$SH" ] || pf P03 "$SH missing: run install.sh first"
    getent passwd $TBUSER >/dev/null || pf P03 "no account $TBUSER: run install.sh first"
    # P04 release files intact
    for r in "${RELEASE[@]}"; do IFS='|' read -r rel t own mode sha <<< "$r"
        is_hex64 "$sha" || { pf P04 "embedded constant for $rel is not a sha256 (unfrozen script)"; continue; }
        if [ ! -f "$SRC/$rel" ] || [ -L "$SRC/$rel" ]; then pf P04 "$SRC/$rel missing or not a regular file"; continue; fi
        got=$(h "$SRC/$rel"); [ "$got" = "$sha" ] || pf P04 "$rel is ${got:0:16}, release constant ${sha:0:16}"
    done
    # P05 interpreter
    [ "$(stat -c '%U:%G %a' "$(readlink -f /usr/bin/python3)")" = "root:root 755" ] || pf P05 "/usr/bin/python3 target is not root:root 755"
    /usr/bin/python3 -IB -c 'import sys; assert sys.flags.isolated' 2>/dev/null || pf P05 "/usr/bin/python3 -IB is not isolated"
    # P06 tools + the verified sudo implementations (D14: refusal only)
    for t in setpriv setfacl getfacl visudo useradd usermod userdel passwd sysctl sshd sudo flock journalctl ss; do
        command -v "$t" >/dev/null 2>&1 || pf P06 "required tool not found: $t"; done
    v=$(sudo_version_ok) || pf P06 "sudo '$v' is not a verified implementation/version ($SUPPORTED_SUDO)"
    # P07 sudoers parses and includes the drop-in directory
    visudo -c >/dev/null 2>&1 || pf P07 "visudo -c fails on the current sudoers"
    grep -qE '^[@#]includedir /etc/sudoers.d' /etc/sudoers || pf P07 "/etc/sudoers does not include /etc/sudoers.d"
    # P08 install layout as install.sh left it
    [ "$(stat -c '%U:%G %a' "$SH" 2>/dev/null)" = "root:$TBUSER 750" ] || pf P08 "$SH is not root:$TBUSER 750"
    [ "$(stat -c '%U:%G %a' "$SH/lib" 2>/dev/null)" = "root:$TBUSER 750" ] || pf P08 "$SH/lib is not root:$TBUSER 750"
    [ "$(stat -c %U "$SH/lib/board.py" 2>/dev/null)" = root ] || pf P08 "$SH/lib/board.py missing or not root-owned"
    [ "$(stat -c '%U:%G %a' "$SH/board" 2>/dev/null)" = "$TBUSER:$TBUSER 2775" ] || pf P08 "$SH/board is not $TBUSER:$TBUSER 2775"
    [ -f "$SH/board/board.json" ] || pf P08 "$SH/board/board.json missing"
    [ -d "$MS" ] || pf P08 "$MS missing"
    [ -z "$(find "$SH/lib" ! -user root -o -perm /022 2>/dev/null | head -1)" ] || pf P08 "$SH/lib has a non-root-owned or group/other-writable entry"
    # P09 venv not writable before the jail boundary (verify only)
    [ -d "$VENV" ] || pf P09 "$VENV missing: run install.sh venv first"
    [ -z "$(find "$VENV" ! -type l \( ! -user root -o -perm /022 \) 2>/dev/null | head -1)" ] || pf P09 "$VENV has a non-root-owned or group/other-writable entry"
    # P10 tbaudit holds treebeard
    getent group tbaudit | cut -d: -f4 | tr ',' '\n' | grep -qx $TBUSER || pf P10 "group tbaudit missing or does not list $TBUSER"
    # P11 no principal conflict
    for e in tbjail tbctl; do
        getent passwd "$e" >/dev/null && pf P11 "account $e already exists"
        getent group "$e" >/dev/null && pf P11 "group $e already exists"
    done
    # P12 no sudoers conflict
    [ ! -e "$SUDOERS_T" ] || pf P12 "$SUDOERS_T already exists"
    [ -z "$(grep -rlE 'tbjail|tbctl|jail_prep' /etc/sudoers "$SUDOERS_D" 2>/dev/null)" ] || pf P12 "a sudoers file already names tbjail, tbctl or jail_prep"
    # P13 the service account has no sudo right
    /usr/bin/sudo -n -l -U $TBUSER 2>&1 | grep -q "may run the following" && pf P13 "$TBUSER already has a sudo rule (sudo -l -U $TBUSER)"
    for g in $(id -nG $TBUSER 2>/dev/null); do case $g in sudo|admin|wheel|root) pf P13 "$TBUSER is in group $g";; esac; done
    # P14 no file/dir conflict
    for e in "$CTL" "$SBIN/tb-commission" "$ETC/board_ops.conf" "$JAILDIR" "$PIN"; do
        { [ -e "$e" ] || [ -L "$e" ]; } && pf P14 "$e already exists"; done
    # P15 no foreign ACL on the targets
    for e in "$SH" "$SH/lib" "$SH/lib/board.py" "$SH/board" "$SH/board/board.json"; do
        getfacl -cp "$e" 2>/dev/null | grep -qE '^(user|group):[^:]+:|^default:' && pf P15 "$e already has a named or default ACL entry"; done
    # P16 Yama present, ptrace exactly 1
    v=$(sysctl_ptrace 2>/dev/null || echo absent); ptrace_is1 "$v" || pf P16 "kernel.yama.ptrace_scope is '$v', not exactly 1 (provisioning never changes it)"
    # P17 every configured setting exactly 1, none applied after the pin
    v=$(ptrace_cfg | awk '$1 != "1"'); [ -z "$v" ] || pf P17 "a configured kernel.yama.ptrace_scope is not 1: $(tr '\n' ';' <<< "$v")"
    v=$(ptrace_after_pin); [ -z "$v" ] || pf P17 "a setting is applied after $PIN_NAME: $(tr '\n' ';' <<< "$v")"
    # P18 live keys stable (U6: snapshots in a root-only mktemp directory, umask 077, removed on exit)
    if PF_TMP=$(umask 077; mktemp -d /dev/shm/tbc-pf.XXXXXX) && [ "$(stat -c '%u %a %F' "$PF_TMP")" = "0 700 directory" ]; then
        trap 'rm -rf -- "$PF_TMP"' EXIT
        (umask 077; live_snapshot "$PF_TMP/s1") && sleep 1 && (umask 077; live_snapshot "$PF_TMP/s2") || pf P18 "sysctl -a gave no usable snapshot"
        v=$(live_changes "$PF_TMP/s1" "$PF_TMP/s2" "" 2>&1); rm -rf -- "$PF_TMP"
        [ -z "$v" ] || pf P18 "live keys change by themselves (outside the 18 frozen VOLATILE_KEYS): $(tr '\n' ';' <<< "$v")"
    else pf P18 "no root-only temporary directory (mktemp -d /dev/shm/tbc-pf.XXXXXX)"; fi
    # P19 nothing running that the change affects
    for e in meeseeks.py s3_verify_harness.py reflector.py anvil.py; do
        pgrep -f "$e" | grep -vx "$$" | grep -q . && pf P19 "a process running $e is active"; done
}

# P02: anything in the receipt store except completed rollbacks (archived-*/,
# rollback-*.json) and receipts is the trace of an unfinished run
stray_state() {
    local f out=""
    [ -d "$RCPT_D" ] || return 1
    for f in "$RCPT_D"/*; do [ -e "$f" ] || continue
        case $(basename "$f") in archived-*|rollback-*.json|receipt-*.json|noop-*.json) ;; *) out="$out $(basename "$f")";; esac; done
    [ "${1:-}" = list ] && { echo "${out# }"; return 0; }
    [ -n "$out" ]
}
# ---- --check: read-only consistency of a provisioned box --------------------------
latest_receipt() { ls -1 "$RCPT_D"/receipt-*.json 2>/dev/null | sort | tail -1; }
receipt_ok() { python3 -I -c 'import hashlib, json, sys
r = json.load(open(sys.argv[1])); s = r.pop("self_sha256", "")
sys.exit(0 if hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest() == s else 1)' "$1"; }
# live vs recorded "after" for every action of a receipt; prints one line per difference
receipt_drift() { python3 -I - "$1" "$2" "$EFFECTIVE_PY" <<'PY'
import json, subprocess, sys
r = json.load(open(sys.argv[1]))
SELF = sys.argv[2]
EFF_SRC = sys.argv[3]
def sh(fn, arg):
    return subprocess.run(["bash", "-c", 'source "$0" --source-only; %s "$1"' % fn, SELF, arg],
                          capture_output=True, text=True).stdout.strip()
# a subject touched by several steps (A4 creates receipts/, A7 then adds its ACL) is
# compared with its LAST recorded state only
last = {}
for a in r["actions"]:
    if a["compare"] != "noop":
        last[a["subject"]] = a
exempt = set(r.get("exempt_paths", []))
exec(EFF_SRC)
for a in last.values():
    subj, c = a["subject"], a["compare"]
    kind, _, name = subj.partition(":")
    if c == "exempt" and name not in exempt:
        print("%s %s: recorded as exempt but not in the receipt's exempt_paths" % (a["step"], subj)); continue
    if kind == "path" and name in exempt and c != "exempt":
        print("%s %s: in exempt_paths but recorded with compare=%s" % (a["step"], subj, c)); continue
    if c == "append-only":
        rec = (r.get("append_only") or {}).get(name)
        import os as _os
        _bf = _os.path.join(_os.path.dirname(_os.path.abspath(sys.argv[1])), "receipts-baseline.json")
        if _os.path.isfile(_bf):
            rec = json.load(open(_bf)).get(name, rec)       # the latest committed baseline
        f = lambda st: dict(x.split("=", 1) for x in st.split(" ") if "=" in x) if st != "absent" else {}
        meta = lambda d: (d.get("owner"), d.get("group"), d.get("mode"), d.get("acl"), (d.get("type") or "").replace("regular_empty_file", "regular_file"))
        if rec is None:      # no A12 record (a --partial run): only an untouched empty file is accepted
            emp = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            rec = {"length": 0, "sha256": emp, "inode": None} if f(a["after"]).get("sha") == emp else None
        live = sh("pstate", name)
        if live == "absent":
            print("%s %s: append-only: MISSING (recorded %s bytes)" % (a["step"], subj, rec and rec["length"])); continue
        if rec is None:
            print("%s %s: append-only: no content record; recorded state [%s] != live [%s]" % (a["step"], subj, a["after"], live)); continue
        if meta(f(live)) != meta(f(a["after"])):
            print("%s %s: append-only: owner/group/mode/ACL differ: live [%s] != recorded [%s]" % (a["step"], subj, live, a["after"])); continue
        import hashlib, os
        st = os.stat(name)
        birth = subprocess.run(["stat", "-c", "%W", name], capture_output=True, text=True).stdout.strip()
        if rec.get("inode") is not None and (st.st_ino, st.st_dev, int(birth or 0)) != (rec["inode"], rec["dev"], rec["birth"]):
            print("%s %s: append-only: REPLACED: inode/dev/birth %s/%s/%s != recorded %s/%s/%s" % (a["step"], subj, st.st_ino, st.st_dev, birth, rec["inode"], rec["dev"], rec["birth"])); continue
        data = open(name, "rb").read()
        if len(data) < rec["length"]:
            print("%s %s: append-only: TRUNCATED: %d bytes < recorded %d" % (a["step"], subj, len(data), rec["length"])); continue
        if hashlib.sha256(data[:rec["length"]]).hexdigest() != rec["sha256"]:
            print("%s %s: append-only: RECORDED PREFIX ALTERED (first %d bytes)" % (a["step"], subj, rec["length"])); continue
        continue
    if kind == "principal":
        live, want = sh("ustate", name), a["after"]
    elif c == "exempt":
        live, want = effective(sh("pstate", name)), effective(a["after"])
        oe = (r.get("owner_exceptions") or {}).get(name, [])
        lo = dict(x.split("=", 1) for x in live.split(" ") if "=" in x).get("owner")
        wo = dict(x.split("=", 1) for x in want.split(" ") if "=" in x).get("owner")
        if oe and lo in oe and wo in oe:       # RQ-2: treebeard <-> tbctl only, nothing else differs
            live = live.replace("owner=%s " % lo, "owner=%s " % wo, 1)
    else:
        live, want = sh("pstate", name), a["after"]
    if live != want:
        print("%s %s: live [%s] != recorded [%s]" % (a["step"], subj, live, want))
PY
}
# write the baseline: <receipt> [init]. Atomic (temp in the store + rename), root 0600. On
# advance, the identity (inode/dev/birth) was already verified equal by the check.
baseline_advance() {
    local r=$1 mode=${2:-advance} e recs="[" tmp
    for e in "${APPEND_ONLY_PATHS[@]}"; do [ -e "$e" ] || return 1
        recs="$recs$( [ "$recs" = "[" ] || echo ,)$(append_record "$e")"; done; recs="$recs]"
    tmp=$(mktemp "$RCPT_D/.baseline.XXXXXX") || return 1
    python3 -I - "$RCPT_D/$BASELINE_F" "$recs" "$mode" "$(date -u +%Y%m%dT%H%M%SZ)" "$(basename "$r")" > "$tmp" <<'PY' || { rm -f "$tmp"; return 1; }
import json, os, sys
bf, recs, mode, ts, rname = sys.argv[1:6]
old = json.load(open(bf)) if os.path.isfile(bf) else {}
new = {}
for x in json.loads(recs):
    prev = old.get(x["path"])
    x.pop("bytes_b64", None)
    x.update({"set_at": ts, "set_by": mode, "receipt": rname,
              "previous": {"length": prev["length"], "sha256": prev["sha256"], "set_at": prev.get("set_at")} if prev else None})
    new[x["path"]] = x
print(json.dumps(new, sort_keys=True, indent=1))
PY
    chmod 600 "$tmp" && mv -f "$tmp" "$RCPT_D/$BASELINE_F" || return 1
    for e in "${APPEND_ONLY_PATHS[@]}"; do
        echo "  ok    $(basename "$e") baseline ($mode): $(python3 -I -c 'import json,sys; x=json.load(open(sys.argv[1]))[sys.argv[2]]; p=x.get("previous") or {}; print("%s -> %s bytes" % (p.get("length", "-"), x["length"]))' "$RCPT_D/$BASELINE_F" "$e")"
    done
}
check() {
    local r rc=0 e tbl
    r=$(latest_receipt)
    if [ -z "$r" ]; then
        tbl=""
        for e in tbjail tbctl; do getent passwd "$e" >/dev/null && tbl="$tbl account $e;"; done
        for e in "$SUDOERS_T" "$CTL" "$SBIN/tb-commission" "$PIN" "$JAILDIR"; do [ -e "$e" ] && tbl="$tbl $e;"; done
        if [ -z "$tbl" ]; then echo "CHECK: NOT PROVISIONED"; return 3; fi
        echo "CHECK: INCONSISTENT: containment state present without a receipt:$tbl"; return 1
    fi
    receipt_ok "$r" || { echo "CHECK: INCONSISTENT: receipt $r fails its self_sha256"; return 1; }
    local rh; rh=$(h "$r")
    echo "  receipt $(basename "$r") ${rh:0:12}"
    local d; d=$(receipt_drift "$r" "$SCRIPT")
    if [ -n "$d" ]; then echo "$d" | sed 's/^/  DRIFT /'; rc=1; fi
    for e in tbjail tbctl; do
        local p; p=$(acct_problems "$e"); [ -z "$p" ] && echo "  ok    $e uid $(id -u $e): nologin, locked, own group only" || { echo "  FAIL  $p"; rc=1; }
    done
    local want; want=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["sudo_l_treebeard_after"])' "$r")
    [ "$(/usr/bin/sudo -n -l -U $TBUSER 2>&1 | hs)" = "$want" ] && echo "  ok    sudoers: $TBUSER's listing is the provisioned one (exactly the jail rule)" || { echo "  FAIL  sudoers: $TBUSER's listing changed since provisioning"; rc=1; }
    [ -f "$SUDOERS_T" ] || { echo "  FAIL  $SUDOERS_T absent (INCONSISTENT: sudoers rule absent)"; rc=1; }
    local bad=0; while read -r k p x _; do case $k in sha256|recorded) [ "$(h "$p" 2>/dev/null)" = "$x" ] || { echo "  DRIFT T2_MANIFEST $p"; bad=1; };; esac; done < "$CTL/T2_MANIFEST"
    [ "$bad" = 0 ] && echo "  ok    $CTL/T2_MANIFEST pins match" || rc=1
    grep -qx "receipt $(basename "$r") $rh" "$CTL/PROVISIONED" 2>/dev/null && grep -qx "recorded $SUDOERS_T $(h "$SUDOERS_T" 2>/dev/null)" "$CTL/PROVISIONED" \
        && echo "  ok    $CTL/PROVISIONED names this receipt and the installed sudoers fragment" || { echo "  FAIL  $CTL/PROVISIONED does not match the receipt / fragment"; rc=1; }
    local pv; pv=$(sysctl_ptrace 2>/dev/null)
    { ptrace_is1 "$pv" && [ "$(ptrace_effective)" = "$PIN" ]; } && echo "  ok    kernel.yama.ptrace_scope live 1, pin present and last-applied" || { echo "  FAIL  ptrace live '$pv', effective source $(ptrace_effective)"; rc=1; }
    if [ "$rc" = 0 ]; then
        baseline_advance "$r" || { echo "  FAIL  receipts baseline could not be advanced (nothing else changed)"; rc=1; }
    fi
    if [ "$rc" = 0 ]; then echo "CHECK: PROVISIONED receipt ${rh:0:12} $(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["ts"])' "$r")"; return 0; fi
    echo "CHECK: INCONSISTENT: see the lines above. To remove: sudo bash $SRC/install/rollback-containment.sh $r"; return 1
}

# ---- the 45-row matrix runner, implementation-neutral (R-1 b, R-1a, R-1c) ---------
# sudo's verdict comes from its OWN journal for that invocation (the entries
# between two cursors). R-1c (2026-10-09): ACCEPTED only when BOTH are present:
# (1) the PAM session-opened record, and (2) sudo's own audit entry
# "<user> : PWD=... ; ... COMMAND=..." whose reason field (the text between
# " : " and "PWD=") is EMPTY. A session record alone is not authorization:
# sudo-rs 0.2.13 opens and closes a PAM session before refusing VAR=value.
# REFUSED: no qualifying audit entry and no helper execution (a non-zero exit
# that is not the helper's 97). Anything else is UNCLASSIFIED and fails. The
# helper's exit is then checked against the row. Message text is recorded,
# never matched.
sudo_verdict() {   # <journal entries of ONE invocation> <exit status> -> "VERDICT sessions audit"
    local jl=$1 r=$2 sess aud got
    sess=$(grep -c "session opened for user" <<< "$jl")
    aud=$(grep -cE '^ *[^ :]+ : +PWD=[^;]*;.*COMMAND=' <<< "$jl")   # audit entry, empty reason field
    if [ "$sess" -gt 0 ] && [ "$aud" -gt 0 ]; then got=ACCEPTED
    elif [ "$aud" = 0 ] && [ "$r" -ne 0 ] && [ "$r" -ne 97 ]; then got=REFUSED
    else got=UNCLASSIFIED; fi
    echo "$got $sess $aud"
}
matrix_run() {
    local J=/opt/treebeard/shared/ctl/jail_prep.py
    local P=/usr/bin/python3 W=/opt/treebeard/shared/ctl/board_writer.py
    local C=/opt/treebeard/shared/ctl/commission_op.py D
    D=$(dirname "$J")
    local bad=0 n=0 u t exp cmd out r got tu tg why cur jl sess aud ok
    while IFS='|' read -r exp u t cmd why; do
        [ -n "$exp" ] || continue
        n=$((n+1))
        tu=${t%%:*}; tg=; [ "$t" != "$tu" ] && tg="-g ${t#*:}"
        journalctl --sync 2>/dev/null; cur=$(journalctl -n 1 --show-cursor -q -o cat 2>/dev/null | sed -n 's/^-- cursor: //p')
        out=$(setpriv --reuid="$u" --regid="$u" --init-groups env -i PATH=/usr/bin:/bin \
              /usr/bin/sudo -n -k -u "$tu" $tg $cmd </dev/null 2>&1); r=$?
        journalctl --sync 2>/dev/null
        jl=$(journalctl -q -o cat --after-cursor="$cur" _COMM=sudo + SYSLOG_IDENTIFIER=sudo 2>/dev/null)
        if [ -z "$cur" ]; then got=UNCLASSIFIED; sess=-; aud=-
        else read -r got sess aud <<< "$(sudo_verdict "$jl" "$r")"; fi
        case $exp in
            ACCEPTED) [ "$got" = ACCEPTED ] && [ "$r" = 97 ] ;;
            REFUSED)  [ "$got" = REFUSED ] ;;
            EITHER)   [ "$got" != UNCLASSIFIED ] ;;
            SELF-JP)  [ "$got" = REFUSED ] || { [ "$got" = ACCEPTED ] && [ "$r" = 97 ]; } ;;
            SELF-CO)  [ "$got" = REFUSED ] || { [ "$got" = ACCEPTED ] && [ "$r" != 0 ]; } ;;
            *) false ;;
        esac && ok=ok || { ok=MISMATCH; bad=1; }
        printf '%-8s expected=%-8s sudo=%-12s rc=%-3s sessions=%s audit=%s %-9s -> %-12s %s  [%s] | %s | journal: %s\n' \
            "$ok" "$exp" "$got" "$r" "$sess" "$aud" "$u" "$t" "$cmd" "$why" "$(head -1 <<< "$out" | cut -c1-80)" \
            "$(grep -m1 -E 'COMMAND=|session opened' <<< "$jl" | cut -c1-90)"
    done <<EOF
ACCEPTED|treebeard|tbjail|$P -IB $J request|the exact approved argv
REFUSED|treebeard|tbjail|$P -I -B $J request|flags split into two tokens
REFUSED|treebeard|tbjail|$P -IB -O $J request|extra flag -O
REFUSED|treebeard|tbjail|$P -IBO $J request|extra flag folded into the token
REFUSED|treebeard|tbjail|$P -O -IB $J request|extra flag before -IB
REFUSED|treebeard|tbjail|$P -IB -E $J request|extra flag -E
REFUSED|treebeard|tbjail|$P -IB -S $J request|extra flag -S
REFUSED|treebeard|tbjail|$P -I $J request|-B missing
REFUSED|treebeard|tbjail|$P $J request|no flags
REFUSED|treebeard|tbjail|$P -IB -c print(1)|-c instead of the script
REFUSED|treebeard|tbjail|$P -IB -c print(1) request|-c with the argument
REFUSED|treebeard|tbjail|$P -c print(1)|-c, no flags
REFUSED|treebeard|tbjail|$P -IB /tmp/jail_prep.py request|substituted script path
REFUSED|treebeard|tbjail|$P -IB $D/jail_launch.py request|substituted script: the launcher
REFUSED|treebeard|tbjail|$P -IB $D/../ctl/jail_prep.py request|same file, different spelling
REFUSED|treebeard|tbjail|$P -IB $J request x|second positional argument
REFUSED|treebeard|tbjail|$P -IB $J request request|argument repeated
REFUSED|treebeard|tbjail|$P -IB $J|no argument
REFUSED|treebeard|tbjail|$P -IB $J request;id|shell metacharacter (one token)
REFUSED|treebeard|tbjail|$P -IB $J \$(id)|command substitution text (one token)
REFUSED|treebeard|tbjail|FOO=1 $P -IB $J request|env var via sudo VAR=x
REFUSED|treebeard|tbjail|PYTHONPATH=/tmp $P -IB $J request|env var via sudo VAR=x (PYTHONPATH)
REFUSED|treebeard|tbjail|LD_PRELOAD=/tmp/x.so $P -IB $J request|env var via sudo VAR=x (LD_PRELOAD)
REFUSED|treebeard|tbjail|/usr/bin/env FOO=1 $P -IB $J request|env var via an env wrapper
REFUSED|treebeard|tbjail|/usr/bin/env $P -IB $J request|env wrapper, no var
REFUSED|treebeard|tbjail|/opt/treebeard/shared/venv/bin/python3 -IB $J request|venv interpreter
EITHER|treebeard|tbjail|/usr/bin/python3.14 -IB $J request|symlink target spelling: same root-owned binary; records whether sudo-rs canonicalises
REFUSED|treebeard|tbjail|$J request|the script exec'd directly (no interpreter)
REFUSED|treebeard|tbjail|/bin/sh -c id|a shell
REFUSED|treebeard|tbjail|/usr/bin/id|any other command
REFUSED|treebeard|root|$P -IB $J request|different target user: root
REFUSED|treebeard|#0|$P -IB $J request|different target user: uid 0
REFUSED|treebeard|tbctl|$P -IB $J request|different target user: tbctl
SELF-JP|treebeard|treebeard|$P -IB $J request|self-target: sudo-rs runs it as treebeard; the helper must ABORT rc 97 at its credential lock
REFUSED|treebeard|tbjail:root|$P -IB $J request|target group root
REFUSED|treebeard|tbjail:sudo|$P -IB $J request|target group sudo
REFUSED|treebeard|tbctl|$P -I -B $W selfcheck|treebeard to the writer
REFUSED|treebeard|root|/usr/local/sbin/tb-commission 1|treebeard to tb-commission
SELF-CO|treebeard|treebeard|$P -I -B $C 1 0000000000000000000000000000000000000000000000000000000000000000|commission_op via sudo self-target: unreachable, or refuses outside tb-commission
REFUSED|treebeard|root|/usr/bin/id|treebeard to root
REFUSED|tbjail|tbjail|$P -IB $J request|tbjail has no rule
REFUSED|tbjail|root|/usr/bin/id|tbjail has no rule
REFUSED|tbjail|treebeard|/usr/bin/id|tbjail has no rule
REFUSED|tbctl|tbjail|$P -IB $J request|tbctl has no rule
REFUSED|tbctl|root|/usr/bin/id|tbctl has no rule
EOF
    echo "rows: $n"
    return $bad
}

# ---- apply ---------------------------------------------------------------------------
apply() {
    local tok=$1 r rel t own mode sha b want
    preflight
    local rcpt; rcpt=$(latest_receipt)
    if [ -n "$rcpt" ]; then   # P20 idempotency
        if out=$(check) ; then
            mkdir -p "$RCPT_D"
            python3 -I -c 'import json, sys, time; json.dump({"v": 1, "ts": sys.argv[1], "noop": True, "plan_sha256": sys.argv[2], "uid": 0, "receipt": sys.argv[3]}, open(sys.argv[4], "w"), sort_keys=True)' \
                "$TS" "$(plan_text | hs)" "$(basename "$rcpt")" "$RCPT_D/noop-$TS.json"
            chmod 600 "$RCPT_D/noop-$TS.json"
            echo "already provisioned (receipt $(h "$rcpt" | cut -c1-12)); nothing to do. No-op record: $RCPT_D/noop-$TS.json"; exit 0
        fi
        echo "$out"; refuse "P20: INCONSISTENT: a receipt exists but --check fails. To remove: sudo bash $SRC/install/rollback-containment.sh $rcpt"
    fi
    stray_state && pf P02 "$RCPT_D holds records of an unfinished run ($(stray_state list)): roll it back with --partial; nothing is repaired"
    if [ ${#PF[@]} -gt 0 ]; then printf '  %s\n' "${PF[@]}"; refuse "pre-flight failed (${#PF[@]} item(s))"; fi
    local plan; plan=$(plan_text); local ptok; ptok=$(printf '%s\n' "$plan" | hs | cut -c1-12)
    [ "$tok" = "$ptok" ] || refuse "the box changed since you read the plan, or the token is wrong"
    # P21: the first write
    install -d -o root -g root -m 0700 "$RCPT_D"
    ACT=$RCPT_D/actions-$TS.jsonl; : > "$ACT"; chmod 600 "$ACT"
    printf '%s\n' "$plan" > "$RCPT_D/plan-$TS.txt"; chmod 600 "$RCPT_D/plan-$TS.txt"
    snapshot "$RCPT_D/before-$TS.txt"; chmod 600 "$RCPT_D/before-$TS.txt"
    echo "P21 before-snapshot: $RCPT_D/before-$TS.txt ($(wc -l < "$RCPT_D/before-$TS.txt") facts)"

    echo "== A1 ptrace pin ALONE"
    printf '%s\n' "$PIN_BODY" > "$RCPT_D/pin-$TS.conf"
    b=$(pstate "$PIN")
    inst root:root 644 "$RCPT_D/pin-$TS.conf" "$SYSCTLD/.$PIN_NAME.new" && mv "$SYSCTLD/.$PIN_NAME.new" "$PIN" || fail "A1 install"
    rec_path A1 file "$PIN" - "$b"      # recorded the moment it exists, verified next (a failed step is still undoable)
    [ "$(h "$PIN")" = "$(printf '%s\n' "$PIN_BODY" | hs)" ] || fail "A1 pin hash"
    ptrace_pin_apply "$PIN" "$RCPT_D/sysctl-before-$TS.txt" "$RCPT_D/sysctl-after-$TS.txt" > "$RCPT_D/a1-$TS.txt" 2>&1; local pr=$?
    sed 's/^/  /' "$RCPT_D/a1-$TS.txt"
    [ "$pr" = 0 ] && [ "$(ptrace_effective)" = "$PIN" ] || fail "A1 FAIL CLOSED: a live key other than ptrace_scope changed, ptrace is not exactly 1, sysctl -p failed, or the pin is not the effective source. Only the pin is in place."

    echo "== A2 accounts"
    for e in tbctl tbjail; do
        b=$(ustate $e); local gb; gb=$(pv_group "$e")      # the group's own prior state (DC-11a)
        useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$e"; local ur=$? ur2=1
        [ "$ur" = 0 ] && { usermod -L "$e"; ur2=$?; }
        getent passwd "$e" >/dev/null && json_act A2 account "principal:$e" "$b" "$(ustate $e)" full "[$(claim "principal:$e" passwd absent "$(pv_passwd "$e")"), $(claim "principal:$e" pwstat absent "$(pv_pwstat "$e")"), $(claim "principal:$e" sudo-l absent "$(pv_sudol "$e")"), $(claim "group:$e" group "$gb" "$(pv_group "$e")")]"
        [ "$ur" = 0 ] && [ "$ur2" = 0 ] || fail "A2 useradd/usermod $e"
        local p; p=$(acct_problems "$e"); [ -z "$p" ] || fail "A2 $e: $p"
        echo "  $e: $(getent passwd $e) groups=$(id -nG $e) password=$(passwd -S $e | awk '{print $2}')"
    done

    echo "== A3 containment code"
    b=$(pstate "$CTL"); install -d -o root -g root -m 755 "$CTL" || fail "A3 mkdir"; rec_path A3 dir "$CTL" - "$b"
    for r in "${RELEASE[@]}"; do IFS='|' read -r rel t own mode sha <<< "$r"
        case $t in "$CTL"/*) ;; *) continue;; esac
        b=$(pstate "$t"); inst "$own" "$mode" "$SRC/$rel" "$t" || fail "A3 install $t"
        rec_path A3 file "$t" - "$b"; [ "$(h "$t")" = "$sha" ] || fail "A3 hash $t"
    done

    echo "== A4 receipt store"
    b=$(pstate "$CTL/receipts"); install -d -o root -g tbaudit -m 2750 "$CTL/receipts" || fail "A4 mkdir"; rec_path A4 dir "$CTL/receipts" - "$b"
    b=$(pstate "$CTL/receipts/board.receipts"); inst root:tbaudit 0640 /dev/null "$CTL/receipts/board.receipts" || fail "A4 file"
    rec_path A4 file "$CTL/receipts/board.receipts" - "$b"

    echo "== A5 tb-commission and board_ops.conf"
    for r in "${RELEASE[@]}"; do IFS='|' read -r rel t own mode sha <<< "$r"
        case $t in "$CTL"/*) continue;; esac
        b=$(pstate "$t"); inst "$own" "$mode" "$SRC/$rel" "$t" || fail "A5 install $t"
        rec_path A5 file "$t" - "$b"; [ "$(h "$t")" = "$sha" ] || fail "A5 hash $t"
    done

    echo "== A6 jail scratch"
    if [ ! -e "$RUNS" ]; then b=$(pstate "$RUNS"); install -d -o $TBUSER -g $TBUSER -m 755 "$RUNS" || fail "A6 runs"; rec_path A6 dir "$RUNS" - "$b"; fi
    b=$(pstate "$JAILDIR"); install -d -o tbjail -g tbjail -m 700 "$JAILDIR" || fail "A6 runs/tbjail"; rec_path A6 dir "$JAILDIR" - "$b"
    [ "$(stat -c '%U:%G %a %F' "$JAILDIR")" = "tbjail:tbjail 700 directory" ] || fail "A6 verify"

    echo "== A7 ACLs (the live T2 shape)"
    acl7() {   # compare path setfacl-args...
        local c=$1 p=$2; shift 2; local b; b=$(pstate "$p")
        setfacl "$@" "$p" || fail "A7 setfacl $* $p"
        rec_path A7 acl "$p" "$c" "$b"
    }
    acl7 - "$SH" -m u:tbctl:--x,u:tbjail:--x
    acl7 - "$SH/lib" -m u:tbctl:--x
    acl7 - "$SH/lib/board.py" -m u:tbctl:r--
    acl7 - "$SH/board" -m u:tbctl:rwx
    acl7 - "$SH/board" -d -m u:tbctl:rw-
    acl7 - "$SH/board/board.json" -m u:tbctl:r--
    if [ ! -e "$SH/board/.board.lock" ]; then b=$(pstate "$SH/board/.board.lock"); inst $TBUSER:$TBUSER 0660 /dev/null "$SH/board/.board.lock" || fail "A7 lock"; rec_path A7 file "$SH/board/.board.lock" - "$b"; fi
    acl7 - "$SH/board/.board.lock" -m u:tbctl:rw-
    acl7 - "$CTL/receipts" -m u:tbctl:--x
    acl7 - "$CTL/receipts/board.receipts" -m u:tbctl:rw-
    for p in "$SH" "$SH/lib" "$SH/lib/board.py" "$SH/board" "$SH/board/board.json" "$SH/board/.board.lock" "$CTL/receipts" "$CTL/receipts/board.receipts"; do
        echo "  $(getfacl -cp "$p" 2>/dev/null | grep -E '^(user|default:user):[^:]+:' | tr '\n' ' ') $p"; done

    echo "== A8b meeseeks/data (RQ-4: only if absent)"
    if [ ! -e "$MS_DATA" ] && [ ! -L "$MS_DATA" ]; then
        b=$(pstate "$MS_DATA")
        install -d -o $MS_DATA_OWNER -g $MS_DATA_GROUP -m $MS_DATA_MODE "$MS_DATA" || fail "A8b create $MS_DATA"
        rec_path A8b dir "$MS_DATA" - "$b"
        [ "$(stat -c '%U:%G %a %F' "$MS_DATA")" = "$MS_DATA_OWNER:$MS_DATA_GROUP $MS_DATA_MODE directory" ] || fail "A8b verify $MS_DATA"
        echo "  created $MS_DATA $(stat -c '%U:%G %a' "$MS_DATA")"
    else
        echo "  $MS_DATA exists ($(stat -c '%U:%G %a %F' "$MS_DATA")): not modified, not recorded"
    fi

    echo "== A9 T2_MANIFEST"
    printf '%s\n%s\n' "# Treebeard containment (provision-containment.sh); receipt receipt-$TS.json" "$SUDOERS_RULE" > "$RCPT_D/sudoers-$TS"
    chmod 0440 "$RCPT_D/sudoers-$TS"
    { echo "installed $TS by ${SUDO_USER:-root} (recorded, not authenticated)"
      for r in "${RELEASE[@]}"; do IFS='|' read -r rel t _ <<< "$r"; echo "sha256 $t $(h "$t")"; done
      echo "sha256 $SH/lib/board.py $(h "$SH/lib/board.py")"
      echo "recorded $SUDOERS_T $(h "$RCPT_D/sudoers-$TS")"; } > "$RCPT_D/T2_MANIFEST-$TS"
    b=$(pstate "$CTL/T2_MANIFEST"); inst root:root 644 "$RCPT_D/T2_MANIFEST-$TS" "$CTL/T2_MANIFEST" || fail "A9"; rec_path A9 file "$CTL/T2_MANIFEST" - "$b"
    sed 's/^/  /' "$CTL/T2_MANIFEST"

    echo "== A10 sudoers LAST"
    visudo -c -f "$RCPT_D/sudoers-$TS" >/dev/null || fail "A10 the fragment does not parse (not installed)"
    b=$(pstate "$SUDOERS_T"); local slb; slb=$(pv_sudol $TBUSER)
    inst root:root 0440 "$RCPT_D/sudoers-$TS" "$SUDOERS_T" || fail "A10 install"
    if ! visudo -c >/dev/null 2>&1; then mv "$SUDOERS_T" "$RCPT_D/sudoers-REJECTED-$TS"; fail "A10 visudo -c failed with the fragment; it was moved out"; fi
    rec_path A10 file "$SUDOERS_T" - "$b" "$(claim "principal:$TBUSER" sudo-l "$slb" "$(pv_sudol $TBUSER)")"
    echo "  ok: $SUDOERS_T $(h "$SUDOERS_T")"

    echo "== A11 live verification"
    local mx; mx=$(matrix_run 2>&1); local mrc=$?
    printf '%s\n' "$mx" > "$RCPT_D/a11-matrix-$TS.txt"; sed 's/^/  /' <<< "$mx"
    [ "$mrc" = 0 ] || fail "A11 (a) the J19 matrix does not match (see above)"
    # (b) the C3 check as the service account. Its (d) record, ctl/PROVISIONED, names the
    # receipt, so it is written at A12: here (a)-(c) must hold and (d) must be the ONLY gap;
    # A12 then requires PROVISIONED once the record exists.
    local cs; cs=$(cd / && as_tb "$VENV/bin/python3" -B -c "import os, pwd, sys; sys.path.insert(0, '$MS'); import meeseeks; v, why = meeseeks._containment_check(pwd.getpwnam, os.lstat, meeseeks._read_bytes); print(why or v)" 2>&1 | tail -1)
    echo "  (b) C3 check as $TBUSER before the record: $cs"
    [ "$cs" = "(d) no root-written sudoers record" ] || fail "A11 (b) the C3 check reports '$cs' (expected only the missing (d) record)"
    local sc; sc=$(cd / && setpriv --reuid=tbctl --regid=tbctl --clear-groups --no-new-privs env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/python3 -I -B "$CTL/board_writer.py" selfcheck 2>&1); local scr=$?
    echo "  (c) board_writer selfcheck as tbctl: rc=$scr"; [ "$scr" = 0 ] || { sed 's/^/    /' <<< "$sc"; fail "A11 (c) board_writer selfcheck"; }

    echo "== A12 after-snapshot, grant proof, receipt"
    snapshot "$RCPT_D/after-$TS.txt"; chmod 600 "$RCPT_D/after-$TS.txt"
    grant_proof "$RCPT_D/before-$TS.txt" "$RCPT_D/after-$TS.txt" "$ACT" > "$RCPT_D/grant-proof-$TS.txt" 2>&1; local gp=$?
    sed 's/^/  /' "$RCPT_D/grant-proof-$TS.txt"
    [ "$gp" = 0 ] || fail "provisioning changed something it did not record (or recorded something without effect)"
    local R=$RCPT_D/receipt-$TS.json
    local APPEND_JSON="[" e; for e in "${APPEND_ONLY_PATHS[@]}"; do APPEND_JSON="$APPEND_JSON$( [ "$APPEND_JSON" = "[" ] || echo ,)$(append_record "$e")"; done; APPEND_JSON="$APPEND_JSON]"
    python3 -I - "$R" "$TS" "$(h "$SCRIPT")" "$(printf '%s\n' "$plan" | hs)" "$tok" "${SUDO_UID:-}" "${SUDO_USER:-}" \
        "$(h "$RCPT_D/before-$TS.txt")" "$(h "$RCPT_D/after-$TS.txt")" "$ACT" "$(/usr/bin/sudo -n -l -U $TBUSER 2>&1 | hs)" \
        "$(sed -n "s/^kernel.yama.ptrace_scope = //p" "$RCPT_D/sysctl-before-$TS.txt")" "$(sysctl_ptrace)" "$PIN" "$SCRIPT" "$(/usr/bin/sudo --version | head -1)" "$APPEND_JSON" "$OWNER_EXCEPTIONS_PATH" "$OWNER_EXCEPTIONS_OWNERS" "${EXEMPT_PATHS[@]}" <<'PY'
import hashlib, json, sys
(out, ts, ssha, psha, tok, suid, suser, bsha, asha, act, sudol, pb, pa, pin, script, sudov, appj, oe_path, oe_owners) = sys.argv[1:20]
exempt_paths = sys.argv[20:]          # the script's EXEMPT_PATHS constant, as is
append_only = {x["path"]: x for x in json.loads(appj)}
r = {"v": 1, "ts": ts, "script_sha256": ssha, "plan_sha256": psha, "token": tok,
     "invoker": {"uid": 0, "sudo_uid": suid, "sudo_user": suser, "basis": "recorded, not authenticated"},
     "before_snapshot_sha256": bsha, "after_snapshot_sha256": asha,
     "actions": [json.loads(l) for l in open(act) if l.strip()],
     "sysctl": {"key": "kernel.yama.ptrace_scope", "live_before": pb, "live_after": pa, "pin_file": pin},
     "sudo_version": sudov, "sudo_l_treebeard_after": sudol,
     "exempt_paths": exempt_paths,
     "append_only": append_only,
     "owner_exceptions": {oe_path: oe_owners.split()},
     "provisioned_record": "/opt/treebeard/shared/ctl/PROVISIONED (written after this receipt; names its sha256)",
     "_lib": script}
r["self_sha256"] = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
with open(out, "w") as f:
    json.dump(r, f, sort_keys=True, indent=1)
PY
    chmod 600 "$R"
    local bo; bo=$(baseline_advance "$R" init) || fail "A12 the initial receipts baseline could not be written"
    sed 's/^/  /' <<< "$bo"
    local rh; rh=$(h "$R")
    { echo "receipt $(basename "$R") $rh"; echo "recorded $SUDOERS_T $(h "$SUDOERS_T")"; } > "$RCPT_D/PROVISIONED-$TS"
    inst root:root 644 "$RCPT_D/PROVISIONED-$TS" "$CTL/PROVISIONED" || fail "A12 PROVISIONED"
    local cs2; cs2=$(cd / && as_tb "$VENV/bin/python3" -B -c "import sys; sys.path.insert(0, '$MS'); import meeseeks; print(meeseeks.containment_state())" 2>&1 | tail -1)
    echo "  C3 state as $TBUSER with the record: $cs2"
    [ "$cs2" = PROVISIONED ] || fail "A12 the C3 state is '$cs2' with the record in place, not PROVISIONED"
    snapshot "$RCPT_D/after2-$TS.txt"; chmod 600 "$RCPT_D/after2-$TS.txt"
    local extra; extra=$(diff <(cut -f1 "$RCPT_D/after-$TS.txt" | sort -u) <(cut -f1 "$RCPT_D/after2-$TS.txt" | sort -u) | grep '^[<>]' ; \
                         diff "$RCPT_D/after-$TS.txt" "$RCPT_D/after2-$TS.txt" | grep '^[<>]' | cut -f1 | sed 's/^[<>] //' | sort -u | grep -vx "path:$CTL/PROVISIONED")
    [ -z "$extra" ] || fail "after the receipt, something other than $CTL/PROVISIONED changed: $extra"
    echo "  receipt: $R"
    echo "  receipt sha256: $rh"
    echo "  $CTL/PROVISIONED root:root 644 written"
    echo "PROVISIONED. Keep the receipt hash. To remove: sudo bash $SRC/install/rollback-containment.sh $R"
}

# ---- main ------------------------------------------------------------------------------
case "${1:-}" in
    --plan)
        preflight
        rc=$(latest_receipt); [ -n "$rc" ] && { check; echo "(a receipt exists: --apply would be a no-op if --check passes)"; exit 0; }
        stray_state && pf P02 "$RCPT_D holds records of an unfinished run ($(stray_state list)): roll it back with --partial"
        plan=$(plan_text); printf '%s\n' "$plan"
        if [ ${#PF[@]} -gt 0 ]; then echo "PRE-FLIGHT: ${#PF[@]} item(s) would refuse --apply:"; printf '  %s\n' "${PF[@]}"; echo "NO TOKEN (fix the items above; nothing changed)"; exit 2; fi
        echo "PRE-FLIGHT: P00-P20 pass"
        echo "TOKEN: $(printf '%s\n' "$plan" | hs | cut -c1-12)"
        echo "To apply exactly this plan: sudo bash $SCRIPT --apply --confirm=<the token above>" ;;
    --apply)
        case "${2:-}" in --confirm=*) apply "${2#--confirm=}" ;; *) refuse "--apply needs --confirm=<token from --plan>" ;; esac ;;
    --check)
        [ "$(id -u)" = 0 ] || refuse "--check runs as root (it reads the receipt store and sudoers)"
        check; exit $? ;;
    --source-only) return 0 2>/dev/null || exit 0 ;;
    *) echo "usage: sudo bash $SCRIPT --plan | --apply --confirm=<token> | --check"
       echo "  --check verifies the receipt, accounts, the service account's sudo -l listing, the T2_MANIFEST pins,"
       echo "  the PROVISIONED record and the ptrace source. sudo -l invokes sudo and may write a journal entry;"
       echo "  it does not execute the governed command."; exit 2 ;;
esac
