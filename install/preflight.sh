#!/usr/bin/env bash
# Treebeard preflight (Sprint 2 / H7). Read-only. Exit non-zero if the host is not ready.
# Each failure prints ONE precise operator instruction. Safe to run any time, as root.
set -uo pipefail
TB_BASE=${TB_BASE:-/opt/treebeard}; TB_ETC=${TB_ETC:-/etc/treebeard}
TB_USER=${TB_USER:-treebeard};      TB_GROUP=${TB_GROUP:-treebeard}
fail=0; ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n        -> %s\n' "$1" "$2"; fail=1; }
echo "[preflight] base=$TB_BASE etc=$TB_ETC user=$TB_USER"
# ---- credentials: must exist, be root:root 0600, and not be the example --------
C=$TB_ETC/credentials/providers.env
if [ ! -f "$C" ]; then bad "credentials file missing: $C" "cp $C.example $C && edit values && chown root:root $C && chmod 0600 $C"
else
  m=$(stat -c '%a %U:%G' "$C"); [ "$m" = "600 root:root" ] && ok "providers.env perms $m" || bad "providers.env perms are $m" "chown root:root $C && chmod 0600 $C"
  grep -qE '^[A-Z_]+=.+' "$C" && ok "providers.env has populated values" || bad "providers.env has no populated values" "edit $C: every KEY= you need must have a value (see $C.example for which are optional)"
fi
# ---- required configs + fail-closed governor -------------------------------------
for r in routing.conf governor; do [ -f "$TB_ETC/$r" ] && ok "$r present" || bad "$TB_ETC/$r missing (REQUIRED)" "cp $TB_ETC/$r.example $TB_ETC/$r (or install.sh config)"; done
for g in constitution.txt treebeard.charter.txt; do
  if [ -f "$TB_ETC/$g" ]; then
    om=$(stat -c "%U:%G %a" "$TB_ETC/$g")
    [ "$om" = "root:root 644" ] && ok "$g present, root:root 644" || bad "$TB_ETC/$g wrong owner/mode ($om)" "chown root:root $TB_ETC/$g && chmod 644 $TB_ETC/$g"
  else
    bad "$TB_ETC/$g missing (REQUIRED)" "re-run install.sh config from the release candidate"
  fi
done
if [ -f "$TB_ETC/governor" ]; then g=$(grep -vE '^\s*(#|$)' "$TB_ETC/governor" | wc -l); [ "$g" -eq 1 ] && ok "governor names exactly one agent" || bad "governor names $g agents (must be exactly 1)" "edit $TB_ETC/governor to one bare agent name"; fi
# ---- venv + imports ---------------------------------------------------------------
V=$TB_BASE/shared/venv/bin/python3
if [ -x "$V" ]; then "$V" -c 'import chromadb, reportlab, pypdf' 2>/dev/null && ok "venv imports chromadb/reportlab/pypdf" || bad "venv missing required packages" "$TB_BASE/shared/venv/bin/pip install -r <repo>/install/requirements.txt"
else bad "venv interpreter missing: $V" "run install.sh venv"; fi
# ---- frontier adapter seam (optional): loadability + contract only ----------------
# The public edition ships the seam, not an adapter. Unconfigured is VALID.
# Configured-but-unloadable is a failure. No adapter is imported or constructed
# here beyond the seam's own contract check.
if [ -x "$V" ]; then
  _sa=$("$V" -c "import sys; sys.path.insert(0, '$TB_BASE/shared/lib');
import router; s, d = router.check_seam_adapter(); print(s + '|' + d)" 2>/dev/null) || _sa="error|preflight could not query the seam"
  case "${_sa%%|*}" in
    ok)             ok "frontier adapter configured and loadable" ;;
    not_configured) ok "no frontier adapter configured (valid for the public edition)" ;;
    *)              bad "frontier adapter configured but unusable: ${_sa#*|}" "unset TREEBEARD_FRONTIER_ADAPTER, or install the private adapter and its dependency overlay" ;;
  esac
fi
# ---- systemd ReadWritePaths targets (unit refuses to start if absent) ----------
for p in shared/status/sun.json shared/status/moon.json shared/memory/dreams shared/memory/subconscious shared/memory/longterm shared/telemetry backups sun/workspace moon/workspace; do
  [ -e "$TB_BASE/$p" ] && ok "exists: $p" || bad "missing ReadWritePaths target: $TB_BASE/$p" "run install.sh dirs"; done
for pair in "shared/status/sun.json:sun" "shared/status/moon.json:moon" "shared/status/career_hunter.json:$TB_USER"; do p=${pair%%:*}; want=${pair##*:}
  [ -e "$TB_BASE/$p" ] && { o=$(stat -c %U "$TB_BASE/$p"); [ "$o" = "$want" ] && ok "status file owner $p=$o" || bad "status file $p owned by $o, writer is $want" "chown $want:$TB_GROUP $TB_BASE/$p"; }; done
# ---- stores: group-writable dirs (writers run as $TB_USER / supplementary group) -----
for s in telemetry records drafts ltm_clark missions; do dd=$TB_BASE/shared/$s
  [ -d "$dd" ] && { m=$(stat -c %a "$dd"); [ "$m" = 2770 ] && ok "store dir $s mode $m" || bad "store dir $s mode $m (want 2770)" "run install.sh initdb"; } || bad "store dir missing: $dd" "run install.sh dirs initdb"; done
# ---- local models (Ollama) ------------------------------------------------------------
if command -v ollama >/dev/null 2>&1 || curl -sf localhost:11434/api/tags >/dev/null 2>&1; then
  have=$(curl -sf localhost:11434/api/tags 2>/dev/null | tr ',' '\n' | grep -oE '"name":"[^"]+"' | cut -d'"' -f4 | tr '\n' ' ')
  for mdl in phi3:mini nomic-embed-text; do echo " $have " | grep -q " $mdl" && ok "ollama model $mdl" || bad "ollama model $mdl not pulled" "ollama pull $mdl"; done
else bad "ollama not reachable on localhost:11434" "install Ollama (https://ollama.com) and start it; then: ollama pull phi3:mini && ollama pull nomic-embed-text"; fi
# ---- rootless podman + toolroom image (career/toolbox path) -----------------------------
if command -v podman >/dev/null 2>&1; then
  if su -s /bin/sh "$TB_USER" -c 'podman image exists localhost/treebeard-toolroom:latest' 2>/dev/null; then ok "podman image localhost/treebeard-toolroom:latest (user $TB_USER)"
  elif [ -f "$TB_BASE/shared/toolbox/Containerfile" ]; then bad "podman image localhost/treebeard-toolroom:latest missing for user $TB_USER" "the career/toolbox path cannot run until this image exists. Build it: su - $TB_USER -c 'podman build -t localhost/treebeard-toolroom:latest -f $TB_BASE/shared/toolbox/Containerfile $TB_BASE/shared/toolbox'"
  else bad "podman image localhost/treebeard-toolroom:latest missing, and no Containerfile at $TB_BASE/shared/toolbox/Containerfile" "import a prebuilt image: su - $TB_USER -c 'podman load -i treebeard-toolroom.tar'"; fi
  [ -f "/var/lib/systemd/linger/$TB_USER" ] && ok "linger enabled for $TB_USER" || bad "linger not enabled for $TB_USER (rootless podman needs /run/user/<uid> without a login)" "loginctl enable-linger $TB_USER"
else bad "podman not installed (career/toolbox path requires rootless podman)" "apt install podman, then re-run preflight"; fi
# ---- inter-agent messaging (F35): store + fail-closed policy ---------------------------------
SMSDB=${TB_BASE:-/opt/treebeard}/shared/sms/sms.db
if [ -f "$SMSDB" ]; then ok "sms store present ($SMSDB)"
else bad "sms store missing: $SMSDB" "run: install.sh initdb   (meetings run without it, but participants cannot message each other)"; fi
SMSP=${TB_ETC:-/etc/treebeard}/sms_pairs.conf
if [ -f "$SMSP" ]; then ok "sms pairs policy present ($SMSP)"
else bad "sms pairs policy absent: $SMSP" "run: install.sh config   (installs the commented example; sms stays fail-closed until you add pairs)"; fi
MMDB=${TB_BASE:-/opt/treebeard}/shared/meetmem/meetmem.db
if [ -f "$MMDB" ]; then ok "meeting-memory store present ($MMDB)"
else bad "meeting-memory store missing: $MMDB" "run: install.sh initdb"; fi
DPDB=${TB_BASE:-/opt/treebeard}/shared/dispatch/dispatch.db
if [ -f "$DPDB" ]; then ok "dispatch store present ($DPDB)"
else bad "dispatch store missing: $DPDB" "run: install.sh initdb"; fi

# ---- bootstrap state: present AND a valid instance of its own format ------------------------
if [ -x "$V" ]; then
  BJ=$TB_BASE/shared/board/board.json
  if [ ! -e "$BJ" ]; then bad "board state missing: $BJ" "run install.sh dirs -- an absent board is not an empty board"
  elif "$V" -c 'import json,sys; sys.exit(0 if isinstance(json.load(open(sys.argv[1])), list) else 1)' "$BJ" 2>/dev/null
  then ok "board.json is a valid JSON list"
  else bad "board.json is not a valid JSON list: $BJ" "inspect it first -- card state may be recoverable; an empty board is: printf '[]\\n' > $BJ"; fi
  for sj in sun moon career_hunter; do sp=$TB_BASE/shared/status/$sj.json
    if [ ! -e "$sp" ]; then bad "status file missing: $sp" "run install.sh dirs"
    elif "$V" -c 'import json,sys; sys.exit(0 if isinstance(json.load(open(sys.argv[1])), dict) else 1)' "$sp" 2>/dev/null
    then ok "status $sj.json is a valid JSON object"
    else bad "status file is not valid JSON: $sp" "a placeholder must be a valid instance of its format: printf '{}\\n' > $sp"; fi; done
  TDB=$TB_BASE/shared/telemetry/telemetry.db
  if [ ! -e "$TDB" ]; then bad "telemetry store missing: $TDB" "run install.sh initdb"
  else for tbl in llm_calls routing_decisions; do
    "$V" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); n=c.execute("select count(*) from sqlite_master where type=? and name=?",("table",sys.argv[2])).fetchone()[0]; sys.exit(0 if n else 1)' "$TDB" "$tbl" 2>/dev/null \
      && ok "telemetry table present: $tbl" || bad "telemetry table missing: $tbl" "run install.sh initdb"; done; fi
  for lnk in "$TB_BASE"/shared/toolkit/*; do
    [ -L "$lnk" ] || continue
    [ -d "$lnk" ] && ok "toolkit link resolves: $(basename "$lnk")" || bad "toolkit link is broken: $lnk" "re-run install.sh code"; done
fi
# ---- units rendered? -----------------------------------------------------------------------
for u in treebeard-agent@.service treebeard-career.service treebeard-clark-intake.service treebeard-clark-patrol.service; do
  [ -f "/etc/systemd/system/$u" ] && { grep -q '@TB_' "/etc/systemd/system/$u" && bad "$u has unrendered tokens" "re-run install.sh units" || ok "unit present, no unrendered tokens: $u"; } || warn "unit not installed: $u (run install.sh units)"; done
grep -qE '^User=' /etc/systemd/system/treebeard-clark-patrol.service 2>/dev/null && ok "clark-patrol has User= (does not run as root)" || warn "clark-patrol unit missing or lacks User= -- do NOT enable it until rendered from the H7 template"
# ---- operator-supplied content (never in the repo) -------------------------------------------
for o in resume.txt career_profile.txt persona_tokens.txt; do [ -f "$TB_BASE/shared/operator/$o" ] && ok "operator file $o" || warn "operator file $TB_BASE/shared/operator/$o absent -- career/intake pipelines will fail visibly until supplied"; done
# ---- containment (Meeseeks code execution; optional) -- OPTION-C-SPEC C3.3 ------------------
# Read-only except: --check, when it fully succeeds, checkpoints the receipts baseline (RQ-1);
# and the probe row, which runs the ONE approved sudo argv with EMPTY stdin as
# $TB_USER: the helper refuses the missing request header (exit 97) and nothing runs.
echo "  ---- containment (Meeseeks code execution; optional) ----"
PCS=$(dirname "$(readlink -f "$0")")/provision-containment.sh
RCPTD=/var/lib/treebeard-containment
if ! ls "$RCPTD"/receipt-*.json >/dev/null 2>&1 && ! getent passwd tbjail >/dev/null && ! getent passwd tbctl >/dev/null \
   && [ ! -e /etc/sudoers.d/treebeard-containment ] && [ ! -e "$TB_BASE/shared/ctl" ]; then
  ok "containment: NOT PROVISIONED -- Meeseeks code execution fails closed (optional: README, section Containment)"
elif [ ! -f "$PCS" ]; then
  bad "containment: state present but $PCS is missing" "run preflight from the release tree (install/preflight.sh)"
else
  cko=$(bash "$PCS" --check 2>&1); ckr=$?
  while IFS= read -r l; do
    case $l in
      "  ok    "*)       ok "containment: ${l#  ok    }" ;;
      "  FAIL  "*|"  DRIFT "*) bad "containment: INCONSISTENT: ${l#  * }" "sudo bash $PCS --check; to remove: sudo bash ${PCS%/*}/rollback-containment.sh <receipt>" ;;
      "CHECK: PROVISIONED receipt "*) set -- ${l#CHECK: PROVISIONED receipt }; ok "containment: receipt $1, provisioned $2" ;;
      "CHECK: INCONSISTENT"*|"CHECK: NOT PROVISIONED"*) [ "$ckr" = 0 ] || bad "containment: ${l#CHECK: }" "sudo bash $PCS --check; to remove: sudo bash ${PCS%/*}/rollback-containment.sh <receipt>" ;;
    esac
  done <<< "$cko"
  if [ "$ckr" = 0 ]; then
    [ "$(stat -c '%U:%G %a' "$TB_BASE/shared/toolbox/data/runs/tbjail" 2>/dev/null)" = "tbjail:tbjail 700" ] && ok "containment: runs/tbjail tbjail:tbjail 700" || bad "containment: INCONSISTENT: runs/tbjail" "sudo bash $PCS --check"
    cs=$(cd / && su -s /bin/sh "$TB_USER" -c "$V -B -c \"import sys; sys.path.insert(0, '$TB_BASE/shared/meeseeks'); import meeseeks; print(meeseeks.containment_state())\"" 2>/dev/null | tail -1)
    [ "$cs" = PROVISIONED ] && ok "containment: C3 state as $TB_USER: PROVISIONED" || bad "containment: INCONSISTENT: C3 state as $TB_USER is '$cs'" "sudo bash $PCS --check"
    pr=$(cd / && su -s /bin/sh "$TB_USER" -c "/usr/bin/sudo -n -u tbjail /usr/bin/python3 -IB $TB_BASE/shared/ctl/jail_prep.py request </dev/null" >/dev/null 2>&1; echo $?)
    [ "$pr" = 97 ] && ok "containment: jail probe: PROVISIONED (helper reached, header refused, nothing run)" || bad "containment: INCONSISTENT: jail probe exit $pr (expected 97: helper reached, header refused)" "sudo bash $PCS --check"
  fi
fi
echo; if [ $fail -eq 0 ]; then echo "[preflight] PASS -- you may enable timers (e.g. systemctl enable --now treebeard-agent@sun.timer)"; else echo "[preflight] FAIL -- fix the items above; do not enable timers"; fi
exit $fail
