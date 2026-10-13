#!/usr/bin/env python3
"""
Sun — Treebeard daily status agent (v0.3)

Collects server status indicators and emails a digest via Resend.
Reads RESEND_API_KEY, RESEND_FROM, DIGEST_TO from environment
(injected by systemd EnvironmentFile).

v0.3 changes from v0.2:
  - Reads the shared status board (/opt/treebeard/shared/lib/statusboard.py)
    and appends an "Agent Reports" table to the digest. This is the
    single-email design: Moon and future agents write a status card instead
    of sending their own email; Sun collects them all here.
  - Writes Sun's own status card too, so the board is uniform.
  - All board access is wrapped defensively: if the board is missing or
    misconfigured, Sun still sends its normal daily digest.

v0.2 changes from v0.1:
  - run() now invokes /bin/bash with `set -o pipefail` auto-prepended.
    Without this, piped commands like `journalctl | grep | wc -l`
    couldn't surface a journalctl failure — wc always succeeds and the
    digest would silently show "0" instead of the intended ERR tag.
  - Local timezone hoisted to a module-level LOCAL_TZ constant.
    Single source of truth; trivially relocatable.

v0.1 changes from initial cut:
  - HTML-escape all interpolated values
  - Replace fragile `grep -c | echo 0` pipeline with `grep | wc -l`
  - Surface command failures as ERR(...) tags instead of silently blanking
  - Use socket.gethostname() and shutil.disk_usage() instead of shelling out
  - Guard against double-wrapping a sender that already has a display name
"""

# ---------------------------------------------------------------------------
# IMPORTS
# All stdlib — no pip install required. Deliberate choice for an MVP on a
# hardened box: no new dependencies, no need to open the egress allowlist
# to PyPI.
# ---------------------------------------------------------------------------

import json                       # Build the JSON body for the Resend API call
import os                         # Read RESEND_* env vars injected by systemd
import shutil                     # disk_usage() — no df parsing needed
import socket                     # gethostname() — no subprocess overhead
import subprocess                 # Run shell commands for the remaining checks
import sys                        # exit() with codes systemd can read
import urllib.request             # Stdlib HTTPS client — replaces `requests`
from datetime import datetime, timezone
from html import escape           # Escape command output before HTML interpolation
from zoneinfo import ZoneInfo     # Stdlib timezone handling (Python 3.9+)
_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")  # S2 relocation convention


# ---------------------------------------------------------------------------
# SHARED STATUS BOARD (added in v0.3)
# Make the shared rulebook importable. Wrapped in try/except so that if the
# board is ever missing, Sun degrades to its plain digest instead of failing.
# ---------------------------------------------------------------------------

# Code is located from where THIS FILE lives; TREEBEARD_BASE relocates STATE
# (dreams, ledgers, board) and must not be used to find the shared library.
# Deriving the lib dir from the state root pointed a relocated or freshly
# installed system at a library directory that does not exist there.
_LIB_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "shared", "lib"))
sys.path.insert(0, _LIB_DIR)
try:
    import statusboard
except Exception:
    statusboard = None

try:
    import dream
except Exception:
    dream = None


# ---------------------------------------------------------------------------
# MODULE CONSTANTS
# Single source of truth for the local timezone the digest reports in.
# Update here only if the operator relocates or Treebeard moves regions.
# ---------------------------------------------------------------------------

LOCAL_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# SHELL COMMAND HELPER
# Two design goals:
#   1. ONE broken check should never crash the whole digest.
#   2. When a check DOES fail, the failure must be VISIBLE — not silently
#      replaced with a blank cell that hides infrastructure drift for months.
#
# To achieve (2) for pipelines, we invoke bash and prepend `set -o pipefail`
# to every command. Without it, a pipeline's exit code is whatever the last
# command returned — and for `... | wc -l` that's almost always 0, meaning
# an upstream `journalctl` failure would be silently lost. With pipefail,
# any non-zero exit anywhere in the pipe propagates and ERR(...) fires.
#
# We use bash (not /bin/sh) because Ubuntu's /bin/sh is dash, which does
# not implement pipefail.
# ---------------------------------------------------------------------------

def run(cmd):
    """Run a shell command and return stripped stdout.

    On any failure, return a short diagnostic string starting with 'ERR'
    so the failure is visible in the digest. Never raises.
    """
    try:
        out = subprocess.run(
            f"set -o pipefail; {cmd}",   # propagate failures through pipes
            shell=True,
            executable="/bin/bash",      # required for pipefail support
            capture_output=True,         # catch stdout and stderr
            text=True,                   # decode bytes -> str automatically
            timeout=10,                  # no single check should exceed 10s
            check=False,                 # we handle exit codes ourselves
        )
        if out.returncode == 0:
            return out.stdout.strip()
        # Non-zero exit: surface the failure with diagnostic info.
        # Cap stderr at 100 chars so a chatty command can't blow out the body.
        err = out.stderr.strip()[:100]
        return f"ERR({out.returncode}): {err}" if err else f"ERR({out.returncode})"
    except subprocess.TimeoutExpired:
        return "ERR(timeout)"
    except FileNotFoundError:
        return "ERR(not found)"
    except Exception as e:
        return f"ERR({type(e).__name__})"


# ---------------------------------------------------------------------------
# HUMAN-READABLE BYTE FORMATTING
# shutil.disk_usage() returns raw bytes. Format like `df -h` does
# (e.g. "15.2G") so the digest reads naturally.
# ---------------------------------------------------------------------------

def format_bytes(n):
    """Format an integer byte count as a short human-readable string."""
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


# ---------------------------------------------------------------------------
# STATUS COLLECTION
# Append new key/command pairs to the returned dict and they appear in the
# email automatically. Prefer stdlib calls over shelling out when stdlib
# can do the job — fewer moving parts, no fragile awk parsing.
# ---------------------------------------------------------------------------

def collect_status():
    """Return a dict of status indicators for the digest."""

    # Two timestamps: UTC for log correlation, local for human reading.
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)

    # Disk usage via stdlib — no df / awk parsing required.
    du = shutil.disk_usage("/")
    disk_pct = (du.used / du.total) * 100
    disk_str = f"{format_bytes(du.used)} of {format_bytes(du.total)} ({disk_pct:.1f}%)"

    return {
        # Server identity — socket call, no subprocess
        "hostname":          socket.gethostname(),
        "time_local":        now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "time_utc":          now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),

        # How long since last reboot — flags unexpected restarts
        "uptime":            run("uptime -p"),

        # 1m / 5m / 15m CPU load averages
        "load_average":      run("uptime | awk -F'load average: ' '{print $2}'"),

        # Root partition used / total / percent (stdlib, no df)
        "disk_root":         disk_str,

        # RAM and swap in human-readable units
        "memory":            run("free -h | awk '/^Mem:/ {print $3\" of \"$2}'"),
        "swap":              run("free -h | awk '/^Swap:/ {print $3\" of \"$2}'"),

        # P6: these count systemd UNIT states for treebeard-agent@*, which are
        # Type=oneshot -- so "active" means "a scheduled run happens to be
        # executing right now", not "the fleet is healthy", and cognitive
        # agents (treebeard/clark/han) have no unit and are never counted.
        # Named for what they actually measure.
        "agent_units_running":  run("systemctl list-units 'treebeard-agent@*' --state=active --no-legend | wc -l"),
        "agent_units_failed":   run("systemctl list-units 'treebeard-agent@*' --state=failed --no-legend | wc -l"),

        # Firewall posture — confirms UFW didn't get disabled
        "ufw_status":        run("systemctl is-active ufw"),

        # Brute-force defense — IPs Fail2ban banned in last 24h.
        # awk (not grep) because grep exits 1 when no matches found,
        # which under pipefail would surface as ERR(1) on quiet days.
        # awk always exits 0 and prints 0 if no matches. Real journalctl
        # failures still propagate through pipefail as intended.
        "fail2ban_bans_24h": run(
            "journalctl -u fail2ban --since '24 hours ago' --no-pager 2>/dev/null | awk '/Ban / {n++} END {print n+0}'"
        ),
    }


# ---------------------------------------------------------------------------
# STATUS BOARD RENDERING (added in v0.3)
# Sun reads every agent's status card via statusboard.read_all() and renders
# them as an "Agent Reports" table appended to the digest. This is why Moon
# (and future agents) don't send their own email — they write a card; Sun
# collects them all right here.
# ---------------------------------------------------------------------------

def _humanize_age(epoch):
    """Turn a unix timestamp into 'just now' / '5m ago' / '2d ago' / 'never'."""
    if not epoch:
        return "never"
    now = int(datetime.now(timezone.utc).timestamp())
    secs = max(0, now - int(epoch))
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 36:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"



# ---------------------------------------------------------------------------
# DAILY DREAM (added: Sun drives the daily dream pass for every carded agent)
# Wrapped like every other optional step: a failed or hung dream becomes an
# ERR line in the digest, never a reason the email fails to send.
# ---------------------------------------------------------------------------
import json as _json
import os as _os
import signal as _signal

DREAMS_DIR = f"{_TB_BASE}/shared/memory/dreams"
DREAM_TIMEOUT_S = 90  # a hung model call must not wedge the morning digest


class _DreamTimeout(Exception):
    pass


def run_dreams(agent_names):
    """Run dream.run(agent) for each agent, each under a hard timeout. Returns
    nothing; dream.py writes its own receipt, which the report reads."""
    if not dream:
        return
    for agent in agent_names:
        def _alarm(signum, frame):
            # Message, not a bare name. This exception can unwind through
            # library code (urlopen inside router's ollama backend), where a
            # caller's `except Exception as e` interpolates str(e). A bare
            # agent name there produced "ollama generate failed: clark" and
            # cost hours of misdirected diagnosis on 2026-08-09.
            raise _DreamTimeout(
                f"dream pass for {agent} exceeded DREAM_TIMEOUT_S="
                f"{DREAM_TIMEOUT_S}s (SIGALRM)")
        old = _signal.signal(_signal.SIGALRM, _alarm)
        _signal.alarm(DREAM_TIMEOUT_S)
        try:
            dream.run(agent)
        except _DreamTimeout:
            print(f"WARN: dream for {agent} timed out after {DREAM_TIMEOUT_S}s",
                  file=sys.stderr)
        except Exception as e:
            print(f"WARN: dream for {agent} failed: {e}", file=sys.stderr)
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old)


# The dream pass runs with Sun, daily. 26h mirrors the cadence Sun already
# declares on its own status card: one missed daily run is stale, a run a few
# hours late is not. Content and freshness are SEPARATE facts -- a stale dream
# is still shown, it is just labelled stale (P3).
DREAM_STALE_AFTER_H = 26


def _receipt_epoch(rec):
    """Epoch seconds from a receipt's ran_at, or 0 if absent/unparseable."""
    raw = (rec or {}).get("ran_at") or ""
    try:
        return int(datetime.fromisoformat(str(raw)).timestamp())
    except Exception:
        return 0


def read_dream_receipt(agent):
    """Read an agent's dream receipt.

    Returns (label, colour, epoch, stale). `epoch` powers the age column (P4);
    `stale` is computed from ran_at against the daily dream cadence (P3). A
    receipt with no readable timestamp is NOT assumed fresh -- it reports
    age 0 ('never'), which renders honestly as unknown."""
    path = _os.path.join(DREAMS_DIR, f"{agent}.receipt.json")
    try:
        with open(path, encoding="utf-8") as fh:
            r = _json.load(fh)
    except Exception:
        return ("", "#666666", 0, False)
    st = r.get("status", "")
    epoch = _receipt_epoch(r)
    now = int(datetime.now(timezone.utc).timestamp())
    stale = bool(epoch and (now - epoch) > DREAM_STALE_AFTER_H * 3600)

    if st == "dreamed":
        n = r.get("fragments_consolidated", 0)
        exc = (r.get("excerpt") or "").strip()
        exc = (exc[:60] + "...") if len(exc) > 60 else exc
        lab, col = f"ok ({n}) {escape(exc)}", "#1a7f37"
    elif st == "dreamless":
        lab, col = "dreamless", "#9a6700"
    elif st == "skipped":
        lab, col = "skipped", "#9a6700"
    elif st == "error":
        lab, col = "ERR", "#b00020"
    else:
        return ("", "#666666", epoch, stale)

    if stale:
        # Keep the content, mark the freshness. Never silently present old
        # dream output as current.
        lab = f"STALE &mdash; {lab}"
        col = "#9a6700"
    return (lab, col, epoch, stale)


_TH = ("text-align:left;padding:6px 12px;border-bottom:2px solid #ddd;"
       "color:#666;font-size:12px;text-transform:uppercase;")
_TD = "padding:6px 12px;border-bottom:1px solid #eee;"


def _dot(colour):
    return (f"<span style='display:inline-block;width:9px;height:9px;"
            f"border-radius:50%;background:{colour};margin-right:6px;"
            f"vertical-align:middle;'></span>")


def status_label(raw):
    """Map a card's status field to (label, colour).

    MISSING DATA IS NOT A WARNING (P2). A card with no status, or an
    unrecognised value, renders as UNKNOWN in neutral grey -- WARN is reserved
    for a component that actually reported a warning condition. The previous
    default turned every absent field into an amber WARN, which made missing
    data indistinguishable from a real fault."""
    colors = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#b00020"}
    if raw is None or str(raw).strip() == "":
        return ("UNKNOWN (no status reported)", "#666666")
    key = str(raw).strip().lower()
    if key not in colors:
        return (f"UNKNOWN ({escape(str(raw))})", "#666666")
    return (key.upper(), colors[key])


# BOARD-TRUTH S1: the board-staleness threshold is NOT defined here. It is
# statusboard.BOARD_STALE_AFTER_H, judged by statusboard.board_is_stale(),
# the same rule Treebeard's box.cards/box.card apply. One definition.


def render_plate_cell(owner):
    """Owner's plate, exactly board.plate() (L: open AND still the agent's
    to work -- review/blocked/approval-wait/closed cards are off it),
    ordered exactly as board.cmd_plate orders it. Ordering defers to
    board.source_rank so this can never drift from canonical precedence.
    Fail-open: never raises (caller interpolates raw)."""
    try:
        import board as _board
        with open(BOARD_PATH, encoding="utf-8") as fh:
            items = _json.load(fh)
        mtime = int(_os.path.getmtime(BOARD_PATH))
        rows = _board.plate(items, owner)
        rows.sort(key=lambda i: (_board.source_rank(i.get("source")),
                                 -(i.get("priority") or 0), i.get("id") or 0))
    except BaseException as e:
        return ("<span style='color:#666666;'>plate unreadable: "
                f"{escape(type(e).__name__)}</span>")
    if not rows:
        return "<span style='color:#666666;'>no open items</span>"
    out = []
    for i in rows:
        t = str(i.get("text") or "")
        t = (t[:60] + "...") if len(t) > 60 else t
        out.append(f"#{escape(str(i.get('id')))} {escape(t)} "
                   f"<span style='color:#777;'>&lt;{escape(str(i.get('source')))}"
                   "&gt;</span>")
    body = "<br>".join(out)
    if statusboard is None:
        body = ("<span style='color:#666666;'>board freshness UNKNOWN "
                "(status rulebook unavailable)</span><br>" + body)
    else:
        try:
            verdict = statusboard.board_freshness(BOARD_PATH)["verdict"]
        except Exception:
            verdict = ("STALE" if statusboard.board_is_stale(mtime)
                       else "FRESH")
        if verdict == "STALE":
            body = ("<span style='color:#9a6700;'>STALE &mdash; board last "
                    f"written {_humanize_age(mtime)}</span><br>" + body)
        elif verdict == "UNCHANGED":
            body = ("<span style='color:#666666;'>unchanged since "
                    f"{_humanize_age(mtime)} (board machinery reconciled "
                    "it)</span><br>" + body)
    return body


def render_cognitive_agents(carded_names):
    """Cognitive agents: minds with dream state and NO operational card.

    They are a different KIND of thing from Sun and Moon, so they get their
    own table rather than sharing one with infrastructure and being labelled
    'no card' for correctly lacking something they were never meant to have
    (P1). Authoritative source: the dream receipts written by dream.py."""
    rows = []
    try:
        for fn in sorted(_os.listdir(DREAMS_DIR)):
            # EXACT suffix only: retired/archived artifacts must never be
            # rediscovered by a broadening glob (P7).
            if not fn.endswith(".receipt.json"):
                continue
            name = fn[: -len(".receipt.json")]
            if name in carded_names:
                continue
            lab, col, epoch, _stale = read_dream_receipt(name)
            if not lab:
                continue
            rows.append(
                "<tr>"
                f"<td style='{_TD}font-weight:600;'>{escape(name)}</td>"
                f"<td style='{_TD}white-space:nowrap;color:#555;'>"
                f"{_humanize_age(epoch)}</td>"
                f"<td style='{_TD}'>{_dot(col)}{lab}</td>"
                f"<td style='{_TD}'>{render_plate_cell(name)}</td>"
                "</tr>")
    except Exception as e:
        print(f"WARN: cognitive agent rows failed: {e}", file=sys.stderr)
    if not rows:
        rows = [f"<tr><td colspan='4' style='padding:10px;color:#777;'>"
                f"No cognitive agents reported.</td></tr>"]
    return (
        "<h2 style='margin-top:1.5em;'>Cognitive Agents</h2>"
        "<p style='color:#777;font-size:0.85em;margin:2px 0 6px;'>"
        "Minds. State comes from dream receipts; these components hold no "
        "operational status card by design.</p>"
        "<table style='border-collapse:collapse;border:1px solid #ccc;'>"
        f"<tr><th style='{_TH}'>Agent</th><th style='{_TH}'>Dreamed</th>"
        f"<th style='{_TH}'>Dream</th><th style='{_TH}'>Open plate</th></tr>"
        + "".join(rows) + "</table>")


def render_agent_reports(reports):
    """Operational Services table: components that report health via a status
    card (Sun, Moon). Infrastructure, not minds -- cognitive agents render in
    their own section (P1)."""
    now = int(datetime.now(timezone.utc).timestamp())

    rows = []
    for r in reports:
        agent   = escape(str(r.get("agent", "?")))
        status  = r.get("status")
        summary = escape(str(r.get("summary", "")))
        epoch   = r.get("updated_epoch", 0) or 0
        stale_h = r.get("stale_after_hours")

        label, dot = status_label(status)
        # Cadence-aware staleness: only flag a service if it blew past the
        # cadence IT declared (Sun=26h, Moon=192h).
        stale = bool(stale_h and epoch and (now - int(epoch)) > float(stale_h) * 3600.0)
        if stale:
            label, dot = label + " / STALE", "#b00020"

        rows.append(
            "<tr>"
            f"<td style='{_TD}font-weight:600;'>{agent}</td>"
            f"<td style='{_TD}white-space:nowrap;'>{_dot(dot)}{label}</td>"
            f"<td style='{_TD}white-space:nowrap;color:#555;'>{_humanize_age(epoch)}</td>"
            f"<td style='{_TD}color:#333;'>{summary}</td>"
            "</tr>"
        )

    body = "".join(rows) if rows else (
        "<tr><td colspan='4' style='padding:10px;color:#777;'>"
        "No operational status cards found.</td></tr>"
    )
    carded = {str(r.get("agent", "")) for r in reports}
    return (
        "<h2 style='margin-top:1.5em;'>Operational Services</h2>"
        "<p style='color:#777;font-size:0.85em;margin:2px 0 6px;'>"
        "Infrastructure that reports health via a status card. These "
        "components do not think or dream.</p>"
        "<table style='border-collapse:collapse;border:1px solid #ccc;'>"
        f"<tr><th style='{_TH}'>Service</th><th style='{_TH}'>Status</th>"
        f"<th style='{_TH}'>Updated</th><th style='{_TH}'>Summary</th></tr>"
        f"{body}</table>"
        + render_cognitive_agents(carded)
    )


# ---------------------------------------------------------------------------
# EMAIL BODY FORMATTING
# Inline-styled HTML — email clients (especially ProtonMail) strip or ignore
# external and embedded stylesheets. Every style attribute lives on the
# element itself.
#
# ALL interpolated values are passed through html.escape() first. Our
# current commands are unlikely to produce <, >, or &, but any future check
# that does would otherwise corrupt the email layout silently.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# STABLE ACTIVITY SECTIONS (v0.4): Meeseeks lifecycle, task board, and
# per-tier token burn, all last-24h, all read-only, all fail-OPEN -- a
# broken ledger renders a gray note and never blocks the email.
# ---------------------------------------------------------------------------
import json as _json
import sqlite3 as _sq3
from datetime import timedelta as _timedelta

MEESEEKS_DB = os.environ.get("MEESEEKS_LEDGER", os.path.join(
    os.environ.get("MEESEEKS_ROOT", f"{_TB_BASE}/shared/meeseeks/data"), "ledger.db"))
TELEMETRY_DB = os.environ.get("TELEMETRY_DB",
    f"{_TB_BASE}/shared/telemetry/telemetry.db")
BOARD_PATH = os.environ.get("BOARD_PATH",
    f"{_TB_BASE}/shared/board/board.json")

_ATH = ("text-align:left;padding:6px 12px;border-bottom:2px solid #ddd;"
        "color:#666;font-size:12px;text-transform:uppercase;")
_ATD = "padding:6px 12px;border-bottom:1px solid #eee;"


def _cutoff_24h():
    return (datetime.now(timezone.utc) - _timedelta(hours=24)).isoformat()


def _asection(title, inner):
    return f"<h2 style='margin-top:1.5em;'>{escape(title)}</h2>{inner}"


def _anote(msg):
    return f"<p style='color:#777;font-size:0.9em;'>{escape(msg)}</p>"


def render_meeseeks_activity():
    """Meeseeks lifecycle in the last 24h, from the box's events ledger."""
    try:
        con = _sq3.connect(f"file:{MEESEEKS_DB}?mode=ro", uri=True)
        try:
            cut = _cutoff_24h()
            spawns = con.execute(
                "SELECT COUNT(*) FROM events WHERE kind='spawn' AND ts>?",
                (cut,)).fetchone()[0]
            verdicts = dict(con.execute(
                "SELECT detail, COUNT(*) FROM ("
                " SELECT substr(detail, instr(detail,'-> ')+3) AS detail"
                " FROM events WHERE kind='verdict' AND ts>?)"
                " GROUP BY detail", (cut,)).fetchall())
            errors = con.execute(
                "SELECT COUNT(*) FROM events WHERE kind='meeseek_error'"
                " AND ts>?", (cut,)).fetchone()[0]
            projects = con.execute(
                "SELECT pid, COUNT(*) FROM events WHERE kind='spawn'"
                " AND ts>? GROUP BY pid ORDER BY 2 DESC", (cut,)).fetchall()
        finally:
            con.close()
    except Exception as e:
        return _asection("Meeseeks (24h)",
                         _anote(f"ledger unreadable: {type(e).__name__}"))
    if not spawns and not errors:
        return _asection("Meeseeks (24h)", _anote("no Meeseeks activity"))
    vparts = ", ".join(f"{escape(str(k))}: {v}"
                       for k, v in sorted(verdicts.items())) or "none"
    pparts = ", ".join(f"{escape(p)} ({n})" for p, n in projects) or "none"
    rows = (
        f"<tr><td style='{_ATD}'><b>Spawned</b></td><td style='{_ATD}'>{spawns}</td></tr>"
        f"<tr><td style='{_ATD}'><b>Verdicts</b></td><td style='{_ATD}'>{vparts}</td></tr>"
        f"<tr><td style='{_ATD}'><b>Errors</b></td><td style='{_ATD}'>{errors}</td></tr>"
        f"<tr><td style='{_ATD}'><b>Projects</b></td><td style='{_ATD}'>{pparts}</td></tr>")
    return _asection("Meeseeks (24h)",
        f"<table style='border-collapse:collapse;border:1px solid #ccc;'>{rows}</table>")


def render_board_activity():
    """Board state by source rank + completions in 24h (closed stamp)."""
    try:
        with open(BOARD_PATH, encoding="utf-8") as fh:
            items = _json.load(fh)
        if not isinstance(items, list):
            # Type gate on externally-supplied JSON. Explicit control
            # flow, not `assert`: under `python -O` assertions are
            # stripped, which would let a non-list board reach the loop
            # below and raise out of a function whose contract is to
            # degrade to a note. AssertionError is kept as the raised
            # type so the rendered diagnostic is unchanged.
            raise AssertionError("board is not a list")
    except Exception as e:
        return _asection("Task Board",
                         _anote(f"board unreadable: {type(e).__name__}"))
    cut = _cutoff_24h()

    def bucket(src):
        if src == "operator": return "operator"
        if src == "treebeard": return "treebeard"
        return "agents"

    # L (C2): `closed` is NOT done. Status buckets come from the board's
    # own vocabulary; anything else is counted as unknown, never as done.
    def sbucket(it):
        st = it.get("status")
        if st == "open":
            return "open"
        if st == "done":
            return "done"
        if st == "closed":
            return "failed" if it.get("outcome") == "failed" else "closed"
        return "unknown"

    stats = {}
    done24 = []
    for it in items:
        b = bucket(it.get("source", "operator"))
        s = stats.setdefault(b, {"open": 0, "done": 0, "failed": 0,
                                 "closed": 0, "unknown": 0})
        s[sbucket(it)] += 1
        if it.get("status") == "done" and it.get("closed", "") > cut:
            done24.append(it)
    order = ["operator", "treebeard", "agents"]
    rows = "".join(
        f"<tr><td style='{_ATD}'><b>{b.title()}</b></td>"
        + "".join(f"<td style='{_ATD}'>{stats[b][k]}</td>"
                  for k in ("open", "done", "failed", "closed", "unknown"))
        + "</tr>"
        for b in order if b in stats)
    if not rows:
        return _asection("Task Board", _anote("board is empty"))
    # P5: freshness. Items carry an authoritative `created` stamp; prefer it
    # to filesystem mtime. Rendered as plain age -- no product rule defines a
    # staleness threshold for the board, so this states the fact and judges
    # nothing.
    # L (M11): a transition is activity too -- newest of created / updated /
    # closed, not created alone.
    _stamps = [str(it.get(k) or "") for it in items
               for k in ("created", "updated", "closed") if it.get(k)]
    if _stamps:
        try:
            _newest = int(datetime.fromisoformat(max(_stamps)).timestamp())
            _age_src = "newest item"
        except Exception:
            _newest, _age_src = 0, "newest item"
    else:
        try:
            _newest, _age_src = int(_os.path.getmtime(BOARD_PATH)), "file mtime"
        except Exception:
            _newest, _age_src = 0, "unknown"
    _age_html = (f"<p style='color:#777;font-size:0.85em;margin:2px 0 6px;'>"
                 f"Last activity: {escape(_humanize_age(_newest))} "
                 f"({escape(_age_src)})</p>")

    head = (f"<tr><th style='{_ATH}'>Source</th><th style='{_ATH}'>Open</th>"
            f"<th style='{_ATH}'>Done</th><th style='{_ATH}'>Failed</th>"
            f"<th style='{_ATH}'>Closed</th><th style='{_ATH}'>Unknown</th>"
            "</tr>")
    # Per-state counts from THE resolver (uistate.resolve over opshell's
    # one execution-fact source) -- the same labels every surface shows.
    try:
        import opshell as _ops
        import uistate as _ui
        _by = {}
        for _c, _k in _ops.resolve_items(items):
            _by[_k] = _by.get(_k, 0) + 1
        _state_html = ("<p style='margin:4px 0;'><b>By state:</b> "
                       + escape(" \u00b7 ".join(
                           "%s %d" % (_ui.get(k)[0], _by[k])
                           for k in list(_ui.STATES) if k in _by))
                       + "</p>")
    except Exception as e:
        _state_html = _anote(f"card states unavailable: {type(e).__name__}")
    comp = "".join(
        f"<li>{escape(it.get('text','')[:120])} "
        f"<span style='color:#777'>({escape(it.get('source','?'))})</span></li>"
        for it in done24[:10])
    comp_html = (f"<p style='margin:6px 0 2px;'><b>Completed last 24h:"
                 f"</b> {len(done24)}</p><ul style='margin-top:2px;'>{comp}</ul>"
                 if done24 else _anote("no completions in the last 24h"))
    return _asection("Task Board",
        f"{_age_html}"
        f"<table style='border-collapse:collapse;border:1px solid #ccc;'>"
        f"{head}{rows}</table>{_state_html}{comp_html}")


def render_clark_brief():
    """Clark's latest daily brief, pulled from the governance record store.
    Read-only, fail-OPEN: no brief (or an unreadable store) renders a gray
    note and never blocks the email. Mirrors the other activity sections."""
    try:
        import record_log
        recs = record_log.records_since(hours=24, kind="action", agent="clark")
    except Exception as e:
        return _asection("Clark Brief", _anote(f"brief unreadable: {type(e).__name__}"))

    brief = next((r for r in recs if r.get("subject") == "daily-brief"), None)
    if brief is None:
        return _asection("Clark Brief", _anote("Clark posted no brief in the last 24h."))

    payload = brief.get("payload")
    try:
        data = _json.loads(payload) if isinstance(payload, str) else (payload or {})
    except (ValueError, TypeError):
        data = {}

    if isinstance(data, dict) and data.get("html"):
        inner = f"<div style='color:#333;'>{data['html']}</div>"
    elif isinstance(data, dict) and data.get("text"):
        inner = f"<p style='color:#333;white-space:pre-wrap;'>{escape(str(data['text']))}</p>"
    else:
        inner = _anote("Clark's brief had no renderable content.")

    return _asection("Clark Brief", inner)


def render_token_burn():
    """Tokens and spend per tier, last 24h, from the telemetry ledger.
    Tier vocabulary is telemetry's own (local|free|cheap|premium)."""
    try:
        con = _sq3.connect(f"file:{TELEMETRY_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT tier, COUNT(*), COALESCE(SUM(tokens_in),0),"
                " COALESCE(SUM(tokens_out),0), COALESCE(SUM(cost_usd),0),"
                " SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END)"
                " FROM llm_calls WHERE ts_utc>? GROUP BY tier",
                (_cutoff_24h(),)).fetchall()
        finally:
            con.close()
    except Exception as e:
        return _asection("LLM Usage (24h)",
                         _anote(f"telemetry unreadable: {type(e).__name__}"))
    if not rows:
        return _asection("LLM Usage (24h)", _anote("no calls in the last 24h"))
    order = {"local": 0, "free": 1, "cheap": 2, "premium": 3}
    rows.sort(key=lambda r: order.get(r[0], 9))
    body = "".join(
        f"<tr><td style='{_ATD}'><b>{escape(t)}</b></td>"
        f"<td style='{_ATD}'>{c}</td><td style='{_ATD}'>{ti:,}</td>"
        f"<td style='{_ATD}'>{to:,}</td>"
        f"<td style='{_ATD}'>${cost:.4f}</td>"
        f"<td style='{_ATD}'>{err or 0}</td></tr>"
        for t, c, ti, to, cost, err in rows)
    tot = [sum(r[i] for r in rows) for i in (1, 2, 3, 4, 5)]
    body += (f"<tr><td style='{_ATD}'><b>Total</b></td>"
             f"<td style='{_ATD}'><b>{tot[0]}</b></td>"
             f"<td style='{_ATD}'><b>{tot[1]:,}</b></td>"
             f"<td style='{_ATD}'><b>{tot[2]:,}</b></td>"
             f"<td style='{_ATD}'><b>${tot[3]:.4f}</b></td>"
             f"<td style='{_ATD}'><b>{tot[4] or 0}</b></td></tr>")
    head = (f"<tr><th style='{_ATH}'>Tier</th><th style='{_ATH}'>Calls</th>"
            f"<th style='{_ATH}'>Tok In</th><th style='{_ATH}'>Tok Out</th>"
            f"<th style='{_ATH}'>Cost</th><th style='{_ATH}'>Errors</th></tr>")
    return _asection("LLM Usage (24h)",
        f"<table style='border-collapse:collapse;border:1px solid #ccc;'>"
        f"{head}{body}</table>")


def render_activity():
    """All three sections; each independently fail-open."""
    out = []
    for fn in (render_token_burn, render_meeseeks_activity,
               render_board_activity, render_clark_brief):
        try:
            out.append(fn())
        except Exception as e:
            out.append(_anote(f"section failed: {type(e).__name__}"))
    return "".join(out)


def format_html(status, agent_reports_html=""):
    """Render the status dict as an HTML email body.

    agent_reports_html (v0.3): optional pre-rendered Agent Reports table,
    inserted after the system status table.
    """

    # One row per status field. snake_case keys become Title Case for display.
    # escape() defends against any value containing HTML metacharacters.
    rows = "\n".join(
        f"<tr><td style='padding:6px 12px;'><b>{escape(k.replace('_', ' ').title())}</b></td>"
        f"<td style='padding:6px 12px;'>{escape(v) if v else '—'}</td></tr>"
        for k, v in status.items()
    )

    # Header values are also escaped — defense in depth for future-proofing.
    return f"""<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;">
<h2>Treebeard Daily Digest</h2>
<p><i>{escape(status['hostname'])} — {escape(status['time_local'])}</i></p>
<table style="border-collapse:collapse;border:1px solid #ccc;">
{rows}
</table>
{agent_reports_html}
<p style="color:#888;font-size:0.85em;margin-top:1em;">Generated by Sun.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# RESEND API CALL
# One HTTPS POST to https://api.resend.com/emails with Bearer-token auth.
# 2xx = success (JSON body contains the message ID). Non-2xx raises
# urllib.error.HTTPError, which propagates up to main()'s catch block.
# ---------------------------------------------------------------------------

def _wire_html(html):
    """Encoding boundary for the outbound document (bug 5). Every non-ASCII
    character becomes a numeric entity so the HTML is 7-bit on the wire and
    renders identically whatever charset a mail client guesses. Storage
    upstream stays UTF-8; this is applied only at the emit boundary."""
    return html.encode("ascii", "xmlcharrefreplace").decode("ascii")
def send_email(api_key, sender, recipient, subject, html):
    """POST a single email to Resend. Returns (status_code, response_body)."""

    # Resend expects JSON: from, to (array), subject, html (or text).
    body = json.dumps({
        "from":    sender,
        "to":      [recipient],
        "subject": subject,
        "html":    _wire_html(html),
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",   # Resend's auth scheme
            "Content-Type":  "application/json",
            "User-Agent":    "Treebeard-Sun/0.2",
        },
        method="POST",
    )

    # 30s is conservative — Resend usually responds well under 1s.
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# SENDER NORMALIZATION
# RESEND_FROM might be either a bare address (onboarding@resend.dev) or
# already wrapped with a display name (e.g. "Ops <ops@example.com>").
# Detect which and only add the "Treebeard Sun <...>" wrapper for the
# bare-address case, to avoid nested brackets like
#   "Treebeard Sun <Ops <ops@example.com>>"
# which RFC 5322 parsers will reject.
# ---------------------------------------------------------------------------

def normalize_sender(raw):
    """Wrap a bare address with the Treebeard Sun display name.

    If the input already contains a '<' it's assumed to already be in
    display-name format and passes through unchanged.
    """
    if "<" in raw:
        return raw
    return f"Treebeard Sun <{raw}>"


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# Exit codes matter — systemd watches them.
#   0 = success (digest sent)
#   1 = config problem (missing env vars)
#   2 = send failure (Resend rejected, network issue, etc.)
# Trace failures with: journalctl -u treebeard-agent@sun.service
# ---------------------------------------------------------------------------

def write_own_card(status, summary, run_id, detail=None):
    """BOARD-TRUTH S3: Sun's own card states what this run DID. It is
    written only once the send has an outcome -- "sent"/ok only after the
    send completed, a truthful failure otherwise. Before S3 it was written
    before the send, so a failed send left "daily digest sent" on the card.
    A board problem must still never stop or fail the email itself."""
    if not statusboard:
        return
    try:
        statusboard.write_status(
            "sun", status, summary, detail=detail or {},
            stale_after_hours=26,   # daily agent: flag if silent > ~1 day
            run_id=run_id)
    except Exception as e:
        print(f"WARN: could not write Sun's status card: {e}", file=sys.stderr)


def main():
    run_id = None
    if statusboard:
        try:
            run_id = statusboard.make_run_id("sun")
        except Exception:
            run_id = None
    # Pull credentials/config from env (injected by EnvironmentFile).
    api_key   = os.environ.get("RESEND_API_KEY")
    sender    = os.environ.get("RESEND_FROM")
    recipient = os.environ.get("DIGEST_TO")

    # Fail fast and loudly if any are missing — clearer than a Resend 401.
    missing = [k for k, v in {
        "RESEND_API_KEY": api_key,
        "RESEND_FROM":    sender,
        "DIGEST_TO":      recipient,
    }.items() if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        write_own_card("fail", "daily digest NOT sent: missing config "
                       + ", ".join(missing), run_id)
        sys.exit(1)

    # Gather everything.
    status = collect_status()
    card_detail = {
        "load_average": status.get("load_average"),
        "disk_root":    status.get("disk_root"),
        "memory":       status.get("memory"),
    }

    # --- shared status board (v0.3): read every card for the digest ---
    # Sun's OWN card is written after the send (S3), so the digest shows
    # Sun's previous run. Wrapped: a board problem must never stop the email.
    agent_reports_html = ""
    if statusboard:
        try:
            _cards = statusboard.read_all()
            # Daily dream pass: Sun drives a dream for every carded agent, then
            # reports it. Guarded so a dream problem never blocks the digest.
            try:
                # Dream the COGNITIVE roster, not just carded infra.
                # Carded agents (sun/moon) have no day logs; the minds
                # (treebeard/clark/han) were never carded, so the dream
                # pass starved the memory loop for weeks (found Jul 17).
                _roster = [x.strip() for x in os.environ.get(
                    "DREAM_AGENTS", "treebeard,clark,han").split(",")
                    if x.strip()]
                # Carded components (sun/moon) are INFRASTRUCTURE: they hold
                # status cards because they report health, not because they
                # think. Unioning them in made them dream targets, producing
                # permanent "dreamless" receipts for things with no mind.
                # The dream roster is the cognitive list alone.
                run_dreams(sorted(set(_roster)))
            except Exception as e:
                print(f"WARN: dream pass failed: {e}", file=sys.stderr)
            # Re-read so freshly written dream receipts are reflected this run.
            agent_reports_html = render_agent_reports(statusboard.read_all())
        except Exception as e:
            print(f"WARN: could not build agent reports: {e}", file=sys.stderr)
            agent_reports_html = (
                f"<p style='color:#b00020'>Agent reports unavailable: {escape(str(e))}</p>"
            )

    # Stable activity sections (v0.4) -- guarded like everything else.
    try:
        agent_reports_html = agent_reports_html + render_activity()
    except Exception as e:
        print(f"WARN: activity sections failed: {e}", file=sys.stderr)

    # Render the email body (now including the Agent Reports table).
    html = format_html(status, agent_reports_html)

    # Subject includes the local date so threading works in your inbox.
    today       = datetime.now(LOCAL_TZ).strftime("%b %d, %Y")
    subject     = f"Treebeard Daily Digest — {today}"
    sender_full = normalize_sender(sender)

    # Send. On exception, log to stderr (journal), record the failure on
    # Sun's card, and exit 2. A send that returned is "sent"/ok on the card
    # only for a 2xx status; any other status is a failure ON THE CARD (Moe
    # G3 fix). The exit code for a returned send is unchanged (0).
    try:
        code, body = send_email(api_key, sender_full, recipient, subject, html)
    except Exception as e:
        print(f"ERROR sending: {e}", file=sys.stderr)
        write_own_card("fail", "daily digest NOT sent: %s: %s"
                       % (type(e).__name__, str(e)[:160]), run_id, card_detail)
        sys.exit(2)
    print(f"Sent: HTTP {code}")
    if isinstance(code, int) and 200 <= code < 300:
        write_own_card("ok", f"daily digest sent (HTTP {code})", run_id,
                       card_detail)
    else:
        write_own_card("fail", f"daily digest NOT sent: HTTP {code} "
                       "(non-2xx response)", run_id, card_detail)


def selftest():
    """Offline checks for the presentation semantics changed in U5.
    No network, no email, no writes outside a temp dir."""
    import tempfile as _tf, shutil as _sh
    global DREAMS_DIR
    failures = 0

    def check(label, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures += 1

    saved_dir = DREAMS_DIR

    # B (board type gate): a non-list board must DEGRADE to the section note,
    # not raise. The gate is explicit control flow, not `assert`, so this
    # holds under `python -O` too -- the property the 2026-09-02 hardening
    # established. render_activity would mask a raise as "section failed",
    # so these call render_board_activity directly.
    global BOARD_PATH
    _saved_board = BOARD_PATH
    _btmp = _tf.mkdtemp()
    try:
        for _label, _payload in (("dict", {"items": []}),
                                 ("string", "board"),
                                 ("int", 7)):
            _bp = _os.path.join(_btmp, "board_%s.json" % _label)
            with open(_bp, "w", encoding="utf-8") as _fh:
                _json.dump(_payload, _fh)
            BOARD_PATH = _bp
            try:
                _out = render_board_activity()
                _ok = "board unreadable" in _out
            except BaseException:
                _ok = False
            check("B%s non-list board (%s) degrades to a note, never raises"
                  % (_label[0].upper(), _label), _ok)
        _bp = _os.path.join(_btmp, "board_valid.json")
        with open(_bp, "w", encoding="utf-8") as _fh:
            _json.dump([{"id": 1, "status": "open", "source": "operator"}], _fh)
        BOARD_PATH = _bp
        try:
            _out = render_board_activity()
            _ok = "board unreadable" not in _out and "Operator" in _out
        except BaseException:
            _ok = False
        check("BV valid board still renders the table", _ok)
    finally:
        BOARD_PATH = _saved_board
        _sh.rmtree(_btmp, ignore_errors=True)
    tmp = _tf.mkdtemp(prefix="sun_selftest_")
    try:
        DREAMS_DIR = tmp

        # P2: missing / malformed status must NOT become WARN
        lab, col = status_label(None)
        check("P2 missing status -> UNKNOWN not WARN",
              "UNKNOWN" in lab and "WARN" not in lab)
        lab, _ = status_label("")
        check("P2 empty status -> UNKNOWN", "UNKNOWN" in lab)
        lab, _ = status_label("banana")
        check("P2 unrecognised status -> UNKNOWN", "UNKNOWN" in lab)
        lab, col = status_label("warn")
        check("P2 real warn still WARN", lab == "WARN" and col == "#9a6700")
        lab, _ = status_label("ok")
        check("P2 ok unaffected", lab == "OK")

        def _write(agent, ran_at, status="dreamed"):
            with open(_os.path.join(tmp, f"{agent}.receipt.json"), "w") as fh:
                _json.dump({"agent": agent, "ran_at": ran_at, "status": status,
                            "fragments_consolidated": 1,
                            "excerpt": "a dream"}, fh)

        _now = datetime.now(timezone.utc)
        _write("freshy", _now.isoformat())
        _write("staley", (_now - _timedelta(hours=100)).isoformat())

        lab, col, epoch, stale = read_dream_receipt("freshy")
        check("P3 fresh receipt not stale", (not stale) and epoch > 0)
        check("P4 fresh receipt exposes an age", _humanize_age(epoch) != "never")

        lab, col, epoch, stale = read_dream_receipt("staley")
        check("P3 old receipt marked stale", stale and "STALE" in lab)
        check("P3 stale keeps its content", "a dream" in lab)

        # P7: retired artifacts must not be discovered
        _sh.copy(_os.path.join(tmp, "freshy.receipt.json"),
                 _os.path.join(tmp, "retired.receipt.json.retired-20260101T000000Z"))
        html = render_cognitive_agents(set())
        check("P7 retired receipt not rendered", "retired" not in html)
        check("P1 cognitive table names its agents", "freshy" in html)
        check("P1 cognitive table has no 'no card' label", "no card" not in html)

        # P1: operational table is separate and carries no dream column
        ops = render_agent_reports([
            {"agent": "moon", "status": "ok", "summary": "backup ok",
             "updated_epoch": int(_now.timestamp()), "stale_after_hours": 192}])
        check("P1 operational section present", "Operational Services" in ops)
        check("P1 cognitive section present", "Cognitive Agents" in ops)
        check("P1 no combined 'Agent Reports' heading", "Agent Reports" not in ops)
        check("P1 ops table has no Dream column", ">Dream<" not in ops.split("Cognitive Agents")[0])

        # P2 end-to-end: a card with no status renders UNKNOWN, not WARN
        ops = render_agent_reports([{"agent": "ghost", "summary": "x"}])
        check("P2 cardless status renders UNKNOWN in table", "UNKNOWN" in ops)

        # escaping preserved
        ops = render_agent_reports([
            {"agent": "x", "status": "ok", "summary": "<script>bad</script>"}])
        check("HTML escaping preserved", "<script>" not in ops)
        # R1 (bug 3): relocation -- every STATE path must follow TREEBEARD_BASE;
        # the shared library is CODE and must be found beside this file instead.
        import subprocess as _sp
        _reloc = _os.path.join(tmp, "noncanon_root")
        _env = dict(_os.environ, TREEBEARD_BASE=_reloc)
        for _k in ("MEESEEKS_LEDGER", "MEESEEKS_ROOT", "TELEMETRY_DB", "BOARD_PATH"):
            _env.pop(_k, None)
        _probe = ("import sys; import importlib.util as u; "
                  f"s=u.spec_from_file_location('sunagent', {__file__!r}); "
                  "m=u.module_from_spec(s); s.loader.exec_module(m); "
                  "print(chr(10).join([m._LIB_DIR, m.DREAMS_DIR, m.MEESEEKS_DB, "
                  "m.TELEMETRY_DB, m.BOARD_PATH]))")
        _r = _sp.run([sys.executable, "-c", _probe], env=_env,
                     capture_output=True, text=True)
        _paths = _r.stdout.split()
        if _r.returncode != 0:
            print("  R1 probe stderr:", _r.stderr.strip()[-400:])
        check("R1 relocated import succeeds", _r.returncode == 0 and len(_paths) == 5)
        _state = _paths[1:]
        check("R1 all root paths follow TREEBEARD_BASE",
              bool(_state) and all(p.startswith(_reloc) for p in _state))
        check("R1 no /opt/treebeard literal survives relocation",
              bool(_state) and not any(p.startswith("/opt/treebeard") for p in _state))
        _lib_here = _os.path.normpath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                                    "..", "shared", "lib"))
        check("R1 shared lib located beside the agent, not via the state root",
              bool(_paths) and _paths[0] == _lib_here and not _paths[0].startswith(_reloc))
        # M (bug 5): fixture = the exact Moon summary that rendered as mojibake
        _fix = "weekly backup OK \u2014 274 files, 9.86 MB"
        _doc = format_html({"hostname": "example-host", "time_local": "now"},
                           render_agent_reports([{"agent": "moon", "status": "ok",
                                                  "summary": _fix}]))
        check("M1 document declares utf-8 charset", '<meta charset="utf-8">' in _doc)
        check("M2 em-dash survives generation as U+2014", "\u2014" in _doc)
        _wire = _wire_html(_doc)
        check("M3 wire document is pure ASCII", _wire.isascii())
        check("M4 em-dash carried as numeric entity", "&#8212;" in _wire and "\u2014" not in _wire)
        check("M5 no Windows-1252 mojibake possible on wire",
              "\u00e2\u20ac" not in _wire.encode("utf-8").decode("cp1252", "replace"))
        check("M6 markup untouched by wire encoding", _wire.count("<tr>") == _doc.count("<tr>"))
    finally:
        DREAMS_DIR = saved_dir
        _sh.rmtree(tmp, ignore_errors=True)

    _s3_fixture_parent(check)

    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# BOARD-TRUTH S3 fixtures. The parent builds a throwaway TREEBEARD_BASE and
# re-runs THIS file in a child, so statusboard's store and boxread's sensor
# are derived exactly as in production -- from TREEBEARD_BASE -- and no real
# card is written. main() runs with the network, shell checks and dreams
# stubbed; the real statusboard writer and the real box.status reader run.
# Failure counters, not asserts: this suite is declared SUPPORTED under -O.
# ---------------------------------------------------------------------------

def _s3_fixture_parent(check):
    import tempfile as _tf, shutil as _sh, subprocess as _sp
    base = _tf.mkdtemp(prefix="sun_s3_")
    try:
        _os.makedirs(_os.path.join(base, "shared", "status"))
        _os.makedirs(_os.path.join(base, "shared", "board"))
        env = dict(_os.environ, TREEBEARD_BASE=base)
        for k in ("BOARD_PATH", "MEESEEKS_LEDGER", "MEESEEKS_ROOT",
                  "TELEMETRY_DB", "RESEND_API_KEY", "RESEND_FROM",
                  "DIGEST_TO"):
            env.pop(k, None)
        r = _sp.run([sys.executable, _os.path.abspath(__file__),
                     "--s3-fixtures"], env=env, capture_output=True,
                    text=True, timeout=120)
        for line in r.stdout.splitlines():
            if line.startswith("S3 "):
                verdict, _, label = line[3:].partition(" ")
                check(label, verdict == "PASS")
        check("S3 fixture child completed (rc=0, all checks reported)",
              r.returncode == 0 and "S3-DONE" in r.stdout)
        if r.returncode != 0:
            print("  S3 child stderr:", r.stderr.strip()[-600:])
    finally:
        _sh.rmtree(base, ignore_errors=True)


def _s3_fixture_child():
    global send_email, collect_status, run_dreams, render_activity
    out = []

    def check(label, cond):
        out.append(cond)
        print(f"S3 {'PASS' if cond else 'FAIL'} {label}")

    base = _os.environ["TREEBEARD_BASE"]
    store = _os.path.join(base, "shared", "status")
    check("S3 statusboard store derives from TREEBEARD_BASE",
          statusboard is not None
          and _os.path.realpath(statusboard.STATUS_DIR)
          == _os.path.realpath(store))
    sys.path.insert(0, _LIB_DIR)
    import boxread
    check("S3 box.status sensor derives from TREEBEARD_BASE",
          _os.path.realpath(boxread.STATUS_DIR) == _os.path.realpath(store))

    events = []
    real_ws = statusboard.write_status

    def spy_ws(agent, status, *a, **k):
        events.append("write:%s" % status)
        return real_ws(agent, status, *a, **k)
    statusboard.write_status = spy_ws
    collect_status = lambda: {"hostname": "fixture", "time_local": "now",
                              "load_average": "0.00"}
    run_dreams = lambda names: None
    render_activity = lambda: ""

    def card():
        with open(_os.path.join(store, "sun.json"), encoding="utf-8") as fh:
            return _json.load(fh)

    def run_main(sender):
        global send_email
        send_email = sender
        del events[:]
        try:
            main()
            return 0
        except SystemExit as e:
            return e.code

    _os.environ.update(RESEND_API_KEY="fixture-key",
                       RESEND_FROM="sun@example.invalid",
                       DIGEST_TO="op@example.invalid")

    # 1. the send FAILS: the card says so, and nothing ever said "sent"
    def failing(*a, **k):
        events.append("send")
        raise urllib.error.URLError("fixture: connection refused")
    rc = run_main(failing)
    c = card()
    check("S3 failed send exits 2 (unchanged)", rc == 2)
    check("S3 failed send: the card is written AFTER the send attempt",
          events == ["send", "write:fail"])
    check("S3 failed send: no ok/sent card at any point",
          "write:ok" not in events)
    check("S3 failed send: card status is fail", c.get("status") == "fail")
    check("S3 failed send: card summary says NOT sent, with the cause",
          c.get("summary", "").startswith("daily digest NOT sent: URLError")
          and "sent" not in c.get("summary", "").replace("NOT sent", ""))
    check("S3 failed send: card carries a run_id",
          str(c.get("run_id", "")).startswith("sun-"))
    r = boxread._status_one("sun")
    txt = boxread.render(r)
    check("S3 Treebeard's box.status shows the failure as FRESH state=fail",
          r.get("status") == "OK" and r.get("state") == "fail"
          and "state: fail" in txt and "NOT sent" in txt)
    check("S3 box.status shows the same run_id",
          r.get("run_id") == c.get("run_id"))

    # 2. the send SUCCEEDS: ok/sent only after it returned
    def sending(*a, **k):
        events.append("send")
        return 200, "{}"
    rc = run_main(sending)
    c = card()
    check("S3 good send exits 0", rc == 0)
    check("S3 good send: card written only after the send returned",
          events == ["send", "write:ok"])
    check("S3 good send: card says sent",
          c.get("status") == "ok"
          and c.get("summary") == "daily digest sent (HTTP 200)")

    # 2b. the send RETURNS non-2xx: the card says NOT sent; exit unchanged
    def non2xx(*a, **k):
        events.append("send")
        return 302, "{}"
    rc = run_main(non2xx)
    c = card()
    check("S3 non-2xx send: exit code unchanged (0)", rc == 0)
    check("S3 non-2xx send: card written after the send, never ok",
          events == ["send", "write:fail"])
    check("S3 non-2xx send: card fail with the HTTP status as reason",
          c.get("status") == "fail" and c.get("summary")
          == "daily digest NOT sent: HTTP 302 (non-2xx response)")
    r = boxread._status_one("sun")
    check("S3 non-2xx send: box.status shows state=fail",
          r.get("status") == "OK" and r.get("state") == "fail"
          and "HTTP 302" in boxread.render(r))

    # 3. config missing: no send, a truthful failure card
    for k in ("RESEND_API_KEY", "RESEND_FROM", "DIGEST_TO"):
        _os.environ.pop(k, None)
    rc = run_main(sending)
    c = card()
    check("S3 missing config exits 1 (unchanged) and never sends",
          rc == 1 and "send" not in events)
    check("S3 missing config: card fail, names the vars, no values",
          c.get("status") == "fail"
          and "missing config RESEND_API_KEY" in c.get("summary", "")
          and "fixture-key" not in _json.dumps(c))
    statusboard.write_status = real_ws

    # 4. S1: the board threshold has ONE definition (statusboard's)
    with open(_os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    import re as _re
    check("S1 Sun defines no board threshold of its own",
          not _re.search(r"(?m)^\s*BOARD_STALE_AFTER_H\s*=(?!=)", src)
          and statusboard.BOARD_STALE_AFTER_H == 48)
    global BOARD_PATH
    BOARD_PATH = _os.path.join(base, "shared", "board", "board.json")
    with open(BOARD_PATH, "w", encoding="utf-8") as fh:
        _json.dump([{"id": 1, "status": "open", "owner": "han",
                     "source": "operator", "text": "fixture"}], fh)
    win = statusboard.BOARD_STALE_AFTER_H * 3600
    now = int(datetime.now(timezone.utc).timestamp())
    _os.utime(BOARD_PATH, (now - win + 120, now - win + 120))
    check("S1 Sun plate: board inside the shared window is not STALE",
          "STALE" not in render_plate_cell("han"))
    _os.utime(BOARD_PATH, (now - win - 120, now - win - 120))
    check("S1 Sun plate: board past the shared window is STALE",
          "STALE" in render_plate_cell("han"))
    print("S3-DONE" if all(out) else "S3-INCOMPLETE")
    return 0 if all(out) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    if sys.argv[1:] == ["--s3-fixtures"]:
        raise SystemExit(_s3_fixture_child())
    main()
