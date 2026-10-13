#!/usr/bin/env bash
# Treebeard installer (Sprint 2 / H7). Idempotent. Run as root.
#   install.sh [phase ...]   phases: prereq users dirs code venv config initdb units linger preflight | all
# Install-time variables (env): TB_BASE TB_ETC TB_USER TB_GROUP TB_AGENT_USERS TB_PERSONA_USER
#   TB_AUDIT_GROUP TB_TZ TB_PYTHON TB_WITH_WILL TB_ENABLE_TIMERS TB_UNIT_DIR TB_BACKUP_USER
# Units are RENDERED here from units/*.tmpl into ordinary static files; no runtime substitution.
# Credentials are NEVER generated: providers.env.example is installed, preflight fails until populated.
set -euo pipefail
TB_BASE=${TB_BASE:-/opt/treebeard};   TB_ETC=${TB_ETC:-/etc/treebeard}
TB_USER=${TB_USER:-treebeard};        TB_GROUP=${TB_GROUP:-treebeard}
TB_AGENT_USERS=${TB_AGENT_USERS:-"sun moon clark han anvil"}
TB_PERSONA_USER=${TB_PERSONA_USER:-tbpersona}; TB_AUDIT_GROUP=${TB_AUDIT_GROUP:-tbaudit}
TB_TZ=${TB_TZ:-America/New_York};     TB_PYTHON=${TB_PYTHON:-python3}
TB_WITH_WILL=${TB_WITH_WILL:-0};      TB_ENABLE_TIMERS=${TB_ENABLE_TIMERS:-}
TB_BACKUP_USER=${TB_BACKUP_USER:-moon}   # owns $TB_BASE/backups; must be in TB_AGENT_USERS
TB_UNIT_DIR=${TB_UNIT_DIR:-/etc/systemd/system}
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)     # publication root: shared/ treebeard/ etc/treebeard/ examples/ install/
PHASES=("$@"); [ ${#PHASES[@]} -eq 0 ] && PHASES=(all)
has() { local p; for p in "${PHASES[@]}"; do [ "$p" = all ] || [ "$p" = "$1" ] && return 0; done; return 1; }
log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
# ---- per-path helpers: touch exactly one inode, never recurse -------------------
d() { mkdir -p "$1"; chown "$2:$3" "$1"; chmod "$4" "$1"; }          # d PATH OWNER GROUP MODE
j() { [ -e "$1" ] || printf '%s\n' "$5" > "$1"; chown "$2:$3" "$1"; chmod "$4" "$1"; }  # j PATH OWNER GROUP MODE JSON
       # A placeholder must be a VALID instance of its format: consumers parse these files,
       # and an empty file is not valid JSON (clean-room CR-6).
cpf() { install -m "$4" -o "$2" -g "$3" "$5" "$1"; }                  # cpf DEST OWNER GROUP MODE SRC
if [ "$TB_BASE" != /opt/treebeard ] || [ "$TB_ETC" != /etc/treebeard ]; then
  log "WARNING: non-default TB_BASE/TB_ETC. Relocation is honoured by shared/lib and treebeard/ (TREEBEARD_BASE/ETC),"
  log "         but NOT yet by moon/agent.py, persona/, shared/{anvil,meeseeks,toolbox}. Those still assume /opt/treebeard."
  log "         Also: the store initializers only enforce 2770/660 on the default path; this installer applies them explicitly."
fi
# ------------------------------------------------- prerequisites (READ-ONLY, before any mutation)
if has prereq; then
  pf=0; miss() { printf '[install] PREREQ FAIL: %s\n           -> %s\n' "$1" "$2" >&2; pf=1; }
  if command -v "$TB_PYTHON" >/dev/null 2>&1; then
    "$TB_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' \
      || miss "$TB_PYTHON is older than 3.9" "install a newer python3 and set TB_PYTHON to it"
    "$TB_PYTHON" -c 'import venv, ensurepip, sqlite3' 2>/dev/null \
      || miss "$TB_PYTHON lacks venv/ensurepip/sqlite3" "apt install python3-venv (Debian/Ubuntu), then re-run"
  else
    miss "interpreter not found: $TB_PYTHON" "install python3 (>= 3.9), or set TB_PYTHON to its path"
  fi
  for c in install sed ln readlink chown chmod useradd groupadd usermod getent; do
    command -v "$c" >/dev/null 2>&1 || miss "required command not found: $c" "install the package providing $c"
  done
  for r in install/requirements.txt shared/lib treebeard etc/treebeard/constitution.txt etc/treebeard/treebeard.charter.txt; do
    [ -e "$SRC/$r" ] || miss "candidate tree incomplete: $SRC/$r missing" "run install.sh from an intact release candidate"
  done
  for p in "$TB_BASE" "$TB_ETC" "$TB_UNIT_DIR"; do
    pp=$p; while [ ! -e "$pp" ] && [ "$pp" != / ]; do pp=$(dirname "$pp"); done
    [ -w "$pp" ] || miss "cannot create or write $p (nearest existing parent $pp not writable)" "run as root on a writable filesystem"
  done
  [ "$pf" -eq 0 ] || die "prerequisites unmet -- NOTHING has been modified. Fix the items above and re-run."
  log "prerequisites ok"
fi
# ---------------------------------------------------------------- users/groups
if has users; then
  getent group "$TB_GROUP" >/dev/null || groupadd "$TB_GROUP"
  getent group "$TB_AUDIT_GROUP" >/dev/null || groupadd "$TB_AUDIT_GROUP"
  id -u "$TB_USER" >/dev/null 2>&1 || useradd -m -g "$TB_GROUP" -s /bin/bash "$TB_USER"
  for a in $TB_AGENT_USERS; do
    getent group "$a" >/dev/null || groupadd -r "$a"
    id -u "$a" >/dev/null 2>&1 || useradd -r -g "$a" -d "$TB_BASE/$a" -M -s /usr/sbin/nologin "$a"
    usermod -aG "$TB_GROUP" "$a"
  done
  getent group "$TB_PERSONA_USER" >/dev/null || groupadd -r "$TB_PERSONA_USER"
  id -u "$TB_PERSONA_USER" >/dev/null 2>&1 || useradd -r -g "$TB_PERSONA_USER" -d "/home/$TB_PERSONA_USER" -M -s /usr/sbin/nologin "$TB_PERSONA_USER"
  usermod -aG "$TB_AUDIT_GROUP" "$TB_PERSONA_USER"; usermod -aG "$TB_AUDIT_GROUP" "$TB_USER"
  log "users/groups ok"
fi
# ---------------------------------------------------------------- directories (verified live table)
if has dirs; then
  U=$TB_USER; G=$TB_GROUP; B=$TB_BASE
  # Fail explicitly rather than dying mid-chown: these three agents own specific inodes below.
  for a in "$TB_BACKUP_USER" sun moon; do
    case " $TB_AGENT_USERS " in *" $a "*) ;; *) die "agent '$a' owns paths created here but is not in TB_AGENT_USERS ($TB_AGENT_USERS)";; esac
  done
  d "$B" root root 755
  d "$B/shared" root "$G" 750;          d "$B/shared/lib" root "$G" 750;      d "$B/shared/status" root "$G" 750
  d "$B/shared/board" "$U" "$G" 2775;   d "$B/shared/config" root root 755;   d "$B/shared/operator" root "$G" 750
  d "$B/shared/operator/job_lockbox" root "$G" 2770
  d "$B/shared/memory" root root 755
  for m in subconscious longterm dreams; do d "$B/shared/memory/$m" root "$G" 2770; done
  for m in shortterm meetings; do d "$B/shared/memory/$m" "$U" "$G" 2770; done
  for s in telemetry records drafts ltm_clark missions career career/reports career/status; do d "$B/shared/$s" root "$G" 2770; done
  # 2026-08-31 (RC4): stores for modules published since RC3. Without these the
  # directory is created lazily by whichever agent writes first, owned by that
  # agent and not group-writable, so a second agent cannot use it -- observed on
  # the production host, where dispatch.db came out treebeard:treebeard 644.
  # F35 (meeting messaging) needs the sms store to exist on a clean install.
  for s in sms meetmem dispatch; do d "$B/shared/$s" root "$G" 2770; done
  d "$B/shared/career_clark" "$U" "$G" 2770; d "$B/shared/career_clark/job_lockbox" "$U" "$G" 2770
  d "$B/shared/career_clark/status" "$U" "$G" 2750; d "$B/shared/career_clark/patrol" "$U" "$G" 2755
  d "$B/shared/inbound" "$U" "$G" 2750; d "$B/shared/inbound/clark" "$U" "$G" 2750
  for s in toolbox toolbox/data toolkit anvil meeseeks; do d "$B/shared/$s" "$U" "$G" 755; done
  d "$B/shared/toolbox/data/skills" "$U" "$G" 755
  # RC6 D-2 (operator ruling 2026-10-09 (b)): operator UI store (session secret, conversations).
  # Owner/group/mode as measured on the production host: treebeard:treebeard 0700 (stat 20261009T200122Z).
  d "$B/shared/ui" "$U" "$G" 700
  d "$B/backups" "$TB_BACKUP_USER" "$G" 750
  for a in $TB_AGENT_USERS; do d "$B/$a" "$a" "$G" 750; d "$B/$a/workspace" "$a" "$a" 750; done
  d "$B/persona" "$TB_PERSONA_USER" "$TB_AUDIT_GROUP" 750
  d "$B/treebeard" root root 755
  # statusboard contract: dir is root-owned 750; each writer's file pre-created <writer>:group 640
  j "$B/shared/status/sun.json" sun "$G" 640 '{}'; j "$B/shared/status/moon.json" moon "$G" 640 '{}'; j "$B/shared/status/career_hunter.json" "$U" "$G" 640 '{}'
  # Board state must EXIST on a fresh install: an absent board is not an empty board,
  # and the commissioning path reads it. Mode matches what board.save_board writes (0640).
  j "$B/shared/board/board.json" "$U" "$G" 640 '[]'
  d "$TB_ETC" root root 755; d "$TB_ETC/credentials" root "$G" 710; d "$TB_ETC/personality" root "$TB_PERSONA_USER" 2770
  log "directories ok"
fi
# ---------------------------------------------------------------- code (manifest trees only; mode bits preserved from source)
if has code; then
  G=$TB_GROUP
  copytree() { local from=$1 to=$2 owner=$3 grp=$4; local rel m
    (cd "$from" && find . -type f ! -name '*.bak*' ! -name '*.pre-*' ! -path '*/__pycache__/*' ! -path '*/data/*' ! -path '*/workspace/*') | while read -r rel; do
      m=$([ -x "$from/$rel" ] && echo 750 || echo 640); mkdir -p "$to/$(dirname "$rel")"; cpf "$to/$rel" "$owner" "$grp" "$m" "$from/$rel"; done; }
  copytree "$SRC/shared/lib" "$TB_BASE/shared/lib" root "$G"
  copytree "$SRC/treebeard" "$TB_BASE/treebeard" root "$G"
  for a in sun moon; do [ -d "$SRC/$a" ] && copytree "$SRC/$a" "$TB_BASE/$a" root "$G"; done
  for t in persona shared/anvil shared/meeseeks shared/toolbox shared/toolkit shared/config; do [ -d "$SRC/$t" ] && copytree "$SRC/$t" "$TB_BASE/$t" root "$G"; done
  [ -d "$SRC/shared/toolbox/data/skills" ] && copytree "$SRC/shared/toolbox/data/skills" "$TB_BASE/shared/toolbox/data/skills" root "$G"
  # Bare-name toolkit links: copytree selects with `find -type f`, which cannot express a
  # symlink, so without this a fresh install gets the hashed dirs and no bare names.
  if [ -d "$SRC/shared/toolkit" ]; then
    for lnk in "$SRC"/shared/toolkit/*; do
      [ -L "$lnk" ] || continue
      tgt=$(readlink "$lnk"); nm=$(basename "$lnk")
      case "$tgt" in /*|*/*) die "toolkit link $nm points outside its directory ($tgt)";; esac
      [ -d "$TB_BASE/shared/toolkit/$tgt" ] || die "toolkit link $nm targets $tgt, which was not installed"
      ln -sfn "$tgt" "$TB_BASE/shared/toolkit/$nm"; chown -h "$TB_USER:$TB_GROUP" "$TB_BASE/shared/toolkit/$nm"
    done
  fi
  log "code ok"
fi
# ---------------------------------------------------------------- venv
if has venv; then
  V=$TB_BASE/shared/venv
  [ -x "$V/bin/python3" ] || "$TB_PYTHON" -m venv "$V"
  chown root:"$TB_GROUP" "$V"; chmod 755 "$V"
  "$V/bin/pip" install -q --upgrade pip
  "$V/bin/pip" install -q -r "$SRC/install/requirements.txt"
  log "venv ok ($("$V/bin/python3" -c 'import chromadb;print("chromadb",chromadb.__version__)'))"
fi
# ---------------------------------------------------------------- config (examples always; live only if absent; credentials example ONLY)
if has config; then
  G=$TB_GROUP
  for x in "$SRC"/examples/etc/*.example; do n=$(basename "$x" .example)
    cpf "$TB_ETC/$n.example" root "$G" 640 "$x"; [ -e "$TB_ETC/$n" ] || { cpf "$TB_ETC/$n" root "$G" 640 "$x"; log "installed example as live config: $n (edit before production)"; }; done
  for x in "$SRC"/examples/etc/personality/*.example; do n=$(basename "$x" .example)
    cpf "$TB_ETC/personality/$n.example" root "$G" 640 "$x"; [ -e "$TB_ETC/personality/$n" ] || cpf "$TB_ETC/personality/$n" root "$G" 640 "$x"; done
  for g in constitution.txt treebeard.charter.txt; do
    [ -f "$SRC/etc/treebeard/$g" ] || die "required policy file missing from candidate: etc/treebeard/$g -- not an intact release candidate"
    cpf "$TB_ETC/$g" root root 644 "$SRC/etc/treebeard/$g"
  done
  cpf "$TB_ETC/credentials/providers.env.example" root "$G" 640 "$SRC/examples/credentials/providers.env.example"
  [ -e "$TB_ETC/credentials/providers.env" ] || log "NOTE: $TB_ETC/credentials/providers.env not present -- copy the .example, populate, chmod 0600 root:root (preflight will enforce)"
  log "config ok"
fi
# ---------------------------------------------------------------- store initializers (schema) + explicit perms regardless of base
if has initdb; then
  V=$TB_BASE/shared/venv/bin/python3; G=$TB_GROUP
  # The installer may only initialize modules THIS EDITION SHIPS. Naming a module that is not
  # in the publication set aborts the phase mid-way and silently leaves every later store
  # uninitialized. Check first, and say which module is missing rather than raising ImportError.
  _shipped() { [ -f "$TB_BASE/shared/lib/$1.py" ] || die "initializer names a module this edition does not ship: shared/lib/$1.py -- fix the installer's module set, or add the module to the publication manifest"; }
  for m in record_log telemetry draft_store ltm_clark missions sms meetmem; do
    _shipped "$m"
    TREEBEARD_BASE=$TB_BASE TREEBEARD_ETC=$TB_ETC "$V" -c "import sys; sys.path.insert(0,'$TB_BASE/shared/lib'); import $m; $m.init_db()" >/dev/null
  done
  # telemetry.db also carries tables owned by modules that expose ensure_schema() instead of
  # init_db(). Those are created lazily at first write, so a fresh install lacks them until
  # something happens to write (routing_decisions absent after clean install -- CR-10).
  # dispatch.py exposes connect()+ensure_schema(con) rather than init_db().
  _shipped dispatch
  TREEBEARD_BASE=$TB_BASE TREEBEARD_ETC=$TB_ETC "$V" -c "import sys; sys.path.insert(0,'$TB_BASE/shared/lib'); import dispatch; c=dispatch.connect(); dispatch.ensure_schema(c); c.close()" >/dev/null
  for m in decisions hypotheses observations signals; do
    _shipped "$m"
    TREEBEARD_BASE=$TB_BASE TREEBEARD_ETC=$TB_ETC "$V" -c "import sys; sys.path.insert(0,'$TB_BASE/shared/lib'); import $m; $m.ensure_schema()" >/dev/null
  done
  for s in telemetry records drafts ltm_clark missions sms meetmem dispatch; do
    d "$TB_BASE/shared/$s" root "$G" 2770
    for fpath in "$TB_BASE/shared/$s"/*; do [ -f "$fpath" ] && { chown root:"$G" "$fpath"; chmod 660 "$fpath"; }; done
  done
  log "stores initialized (schema) + perms enforced"
fi
# ---------------------------------------------------------------- units (rendered, static)
if has units; then
  TB_UID=$(id -u "$TB_USER")
  for t in "$SRC"/install/units/*.tmpl; do
    rel=$(basename "$t" .tmpl); rel=${rel//__//}
    case "$rel" in treebeard-will*) [ "$TB_WITH_WILL" = 1 ] || continue;; esac
    mkdir -p "$TB_UNIT_DIR/$(dirname "$rel")"
    sed -e "s#@TB_BASE@#$TB_BASE#g" -e "s#@TB_ETC@#$TB_ETC#g" -e "s#@TB_USER@#$TB_USER#g" -e "s#@TB_GROUP@#$TB_GROUP#g" \
        -e "s#@TB_UID@#$TB_UID#g" -e "s#@TB_TZ@#$TB_TZ#g" "$t" > "$TB_UNIT_DIR/$rel.tmp"
    grep -q '@TB_[A-Z]*@' "$TB_UNIT_DIR/$rel.tmp" && die "unrendered token in $rel"
    install -m 644 -o root -g root "$TB_UNIT_DIR/$rel.tmp" "$TB_UNIT_DIR/$rel"; rm -f "$TB_UNIT_DIR/$rel.tmp"
  done
  if [ "$TB_UNIT_DIR" = /etc/systemd/system ] && command -v systemctl >/dev/null; then
    systemctl daemon-reload
    for tm in $TB_ENABLE_TIMERS; do systemctl enable --now "$tm"; log "enabled $tm"; done
    [ -z "$TB_ENABLE_TIMERS" ] && log "timers rendered but NOT enabled (set TB_ENABLE_TIMERS after preflight passes)"
  fi
  log "units rendered into $TB_UNIT_DIR"
fi
# ---------------------------------------------------------------- linger (rootless podman needs /run/user/<uid> without a login session)
if has linger; then
  command -v loginctl >/dev/null && loginctl enable-linger "$TB_USER" && log "linger enabled for $TB_USER" || log "loginctl unavailable; enable lingering for $TB_USER manually"
fi
# ---------------------------------------------------------------- preflight
if has preflight; then
  TB_BASE=$TB_BASE TB_ETC=$TB_ETC TB_USER=$TB_USER TB_GROUP=$TB_GROUP bash "$SRC/install/preflight.sh"
fi
