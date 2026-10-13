#!/usr/bin/env python3
"""
Treebeard shared status board -- the rulebook Sun and Moon both load.

It is a LIBRARY, not an agent: importing it does nothing on its own.
It exposes two actions:
  write_status() -> an agent records ITS OWN status into its own file
  read_all()     -> Sun reads EVERY agent's status to build the digest

BOARD-TRUTH S (2026-10-01) adds two shared definitions, so no reader keeps
its own copy: the optional generic run_id on a card (make_run_id builds
one), and the ONE board-staleness rule (BOARD_STALE_AFTER_H, board_is_stale,
board_freshness) that Sun's digest and Treebeard's box.cards/box.card both
judge the task board by.

BOARD-TRUTH L adds:
  - L-R7: board_freshness() also reads the board.meta.json sidecar (written
    by the reflector's reconcile, freshness/reconcile metadata ONLY) so a
    reader can tell "board UNCHANGED, machinery reconciled" apart from
    "board machinery has not reconciled" (STALE). Still ONE rule.
  - L-R8: a status-level sticky `last_failure` on every card: set by a
    `fail` write, carried forward across later ok/warn writes, replaced by a
    newer failure, cleared ONLY by an explicit clear_last_failure=True write
    (the status contract; no producer passes it). It is HISTORY, never a
    current-state or authority signal.
"""

import os         # for file paths
import re         # for validating agent names
import glob       # for "find all the *.json files"
import json       # for reading/writing the status files (which are JSON)
import datetime   # for timestamps
import hashlib    # board sha256 vs the reconcile stamp (L-R7)


# Where the status files live, e.g. /opt/treebeard/shared/status/moon.json
# (TREEBEARD_BASE is only used for off-server testing; on the box it's unset,
#  so this is just /opt/treebeard.)
BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
STATUS_DIR = os.path.join(BASE, "shared", "status")

# Allowed agent names: a plain ASCII word -- letters, digits, underscore,
# starting with a letter or underscore. e.g. sun, moon, backup_agent.
# (Stricter than isidentifier(), which would also allow Unicode like "Delta".)
_AGENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A run id names ONE producer run, so a reader can match the card to that
# run's own record (journal line, log entry, report). Same character class
# and bound as the box.status reader (boxread._RUN_RE): a run id this writer
# accepts is always one the sensor will show.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# BOARD-TRUTH S1: the ONE board-staleness threshold. The task board is
# STALE when its file was last written more than this many hours ago. Sun's
# digest (plate cell) and Treebeard's box.cards/box.card both judge the
# board through board_is_stale(); neither may define a threshold of its own.
BOARD_STALE_AFTER_H = 48


def _utcnow():
    # Current time, in UTC. Used to timestamp every status update.
    return datetime.datetime.now(datetime.timezone.utc)


def make_run_id(prefix, started_utc=None):
    """A run id: '<prefix>-<UTC start, compact>', e.g.
    'sun-20261002T100000Z'. started_utc is the run's own start stamp in the
    card format ("%Y-%m-%dT%H:%M:%SZ"), so the id matches the run's record;
    None means now."""
    if started_utc is None:
        started_utc = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = "%s-%s" % (prefix, started_utc.replace("-", "").replace(":", ""))
    if not _RUN_ID_RE.match(rid):
        raise ValueError("invalid run id: %r" % (rid,))
    return rid


def board_is_stale(mtime_epoch, now_epoch=None):
    """True when a board last written at mtime_epoch is past
    BOARD_STALE_AFTER_H. Exactly the rule Sun applied before S1."""
    if now_epoch is None:
        now_epoch = int(_utcnow().timestamp())
    return (int(now_epoch) - int(mtime_epoch)) > BOARD_STALE_AFTER_H * 3600


def board_meta_path(path):
    """The reconcile sidecar beside a board file (board.py meta_path rule)."""
    return os.path.splitext(path)[0] + ".meta.json"


def _read_board_meta(path):
    try:
        with open(board_meta_path(path), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def board_freshness(path, now_epoch=None):
    """The board file's last write and age, judged by board_is_stale(), plus
    the L-R7 reconcile stamp. verdict:
      FRESH      board written within the window;
      UNCHANGED  not written within the window, BUT the board machinery
                 reconciled within the window AND the board bytes are exactly
                 the ones it reconciled (sha256 match) -- quiet, not dead;
      STALE      neither: the board machinery has not reconciled.
    Raises OSError when the board cannot be stat'd: the caller must then
    report the board's freshness as UNKNOWN, never assume it is fresh."""
    mtime = int(os.stat(path).st_mtime)
    if now_epoch is None:
        now_epoch = int(_utcnow().timestamp())
    out = {
        "last_write_epoch": mtime,
        "last_write_utc": datetime.datetime.fromtimestamp(
            mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_s": max(0, int(now_epoch) - mtime),
        "window_h": BOARD_STALE_AFTER_H,
        "reconciled_utc": None, "reconcile_age_s": None,
        "reconcile_matches": None, "inconsistent": [],
    }
    meta = _read_board_meta(path)
    rec_ok = False
    if meta is not None:
        rep = meta.get("reconciled_epoch")
        if isinstance(rep, int) and not isinstance(rep, bool) and rep > 0:
            out["reconciled_utc"] = meta.get("reconciled_utc")
            out["reconcile_age_s"] = max(0, int(now_epoch) - rep)
            try:
                with open(path, "rb") as fh:
                    cur = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                cur = None
            out["reconcile_matches"] = (cur is not None
                                        and cur == meta.get("board_sha256"))
            inc = meta.get("inconsistent")
            if isinstance(inc, list):
                out["inconsistent"] = [i for i in inc if isinstance(i, int)]
            rec_ok = (out["reconcile_matches"]
                      and not board_is_stale(rep, now_epoch))
    if not board_is_stale(mtime, now_epoch):
        out["verdict"] = "FRESH"
    elif rec_ok:
        out["verdict"] = "UNCHANGED"
    else:
        out["verdict"] = "STALE"
    out["stale"] = out["verdict"] == "STALE"
    return out


def _prior_last_failure(path):
    """The sticky last_failure on the card currently at path. Absent card ->
    None. An unreadable/torn prior card -> an explicit UNKNOWN marker: a
    failure we can no longer see must not be silently erased (L-R8)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            prior = json.load(fh)
        if not isinstance(prior, dict):
            raise ValueError("not an object")
    except Exception:
        return {"unknown": True,
                "summary": "prior card unreadable; last failure unknown"}
    lf = prior.get("last_failure")
    if isinstance(lf, dict):
        return lf
    if prior.get("status") == "fail":
        # A card written before L-R8 that is itself a failure.
        return {"utc": prior.get("updated_utc"),
                "epoch": prior.get("updated_epoch"),
                "run_id": prior.get("run_id"),
                "summary": prior.get("summary")}
    return None


def write_status(agent, status, summary, detail=None, stale_after_hours=None,
                 run_id=None, clear_last_failure=False):
    """An agent calls this to record its own status. Example: Moon calls it
    after a backup to say "ok, backed up 13 files."."""

    # GUARD (Cheap_provider #1 / ChatGPT nit): the agent name becomes a filename, so
    # reject anything that isn't a plain ASCII word -- this stops "../something"
    # path tricks and odd Unicode filenames.
    if not isinstance(agent, str) or not _AGENT_RE.match(agent):
        raise ValueError("invalid agent name: %r (use letters, digits, _)" % (agent,))

    # GUARD: status must be one of these three words, nothing else.
    if status not in ("ok", "warn", "fail"):
        raise ValueError("status must be ok|warn|fail, got %r" % (status,))

    # GUARD (Cheap_provider #3): if a cadence is given, it has to be a positive number.
    if stale_after_hours is not None and (
            not isinstance(stale_after_hours, (int, float))
            or stale_after_hours <= 0):
        raise ValueError("stale_after_hours must be a positive number or None")

    # GUARD: run_id is optional; when given it must be a plain id that the
    # box.status reader will display (same rule as the reader).
    if run_id is not None and (not isinstance(run_id, str)
                               or not _RUN_ID_RE.match(run_id)):
        raise ValueError("invalid run_id: %r" % (run_id,))

    # GUARD (Cheap_provider #5): clear error if the folder isn't there, instead of a
    # confusing raw traceback. (We do NOT create it -- that's root's job.)
    if not os.path.isdir(STATUS_DIR):
        raise RuntimeError("status directory missing: %s" % STATUS_DIR)

    now = _utcnow()

    # Build the record we're going to save. This is the whole "what happened":
    payload = {
        "agent": agent,                      # who is this (e.g. "moon")
        "status": status,                    # ok / warn / fail
        "summary": summary,                  # one human sentence
        "detail": detail if detail is not None else {},   # extra info, optional
        "stale_after_hours": stale_after_hours,  # how long before "no news = bad"
        "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),  # readable timestamp
        "updated_epoch": int(now.timestamp()),              # numeric timestamp
    }
    # Only when given: a card without one stays byte-for-byte as before.
    if run_id is not None:
        payload["run_id"] = run_id

    # L-R8 sticky last_failure: a fail write records itself; any other write
    # carries the previous one forward unless explicitly cleared. History,
    # NEVER a current-state or authority signal.
    path = os.path.join(STATUS_DIR, agent + ".json")
    if status == "fail":
        lf = {"utc": payload["updated_utc"],
              "epoch": payload["updated_epoch"],
              "run_id": run_id, "summary": summary}
    elif clear_last_failure:
        lf = None
    else:
        lf = _prior_last_failure(path)
    if lf is not None:
        payload["last_failure"] = lf

    # Turn that record into nicely-formatted text.
    text = json.dumps(payload, indent=2, ensure_ascii=False)

    # The file this agent is allowed to write, e.g. .../status/moon.json
    # (path computed above, where the prior card's last_failure is read.)

    # Overwrite the file in place. "w" truncates the old contents first, then
    # we write the new record.
    #
    # The textbook-safe pattern is write-to-temp-then-rename, so a reader can
    # NEVER catch a half-written file. We knowingly don't do that here, for two
    # reasons: (1) the folder permissions (Step 3) forbid agents from creating
    # or renaming files -- which is the very thing that stops one agent from
    # touching another's file; and (2) we can accept the tradeoff because
    # read_all() treats a torn file as a temporary "fail" row instead of
    # crashing. So a crash mid-write at worst shows ONE agent as failed for a
    # single digest, then self-heals on its next run. The fsync below still
    # forces the finished write to disk promptly to keep that window tiny.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.write("\n")
        fh.flush()              # push out of Python's buffer...
        os.fsync(fh.fileno())   # ...and force it to disk.

    return payload


def read_all():
    """Sun calls this to gather EVERY agent's status into one list, which it
    then turns into the table in the email."""

    out = []

    # Find every status file in the folder and go through them in order.
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "*.json"))):
        name = os.path.basename(path)[:-5]   # filename minus ".json" -> agent name

        try:
            # Read the file and parse it back into a record.
            with open(path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)

            # Sanity check it's actually a record and not junk.
            if not isinstance(obj, dict):
                raise ValueError("section is not a JSON object")

            # (Cheap_provider #4) The FILENAME is the source of truth for who this is.
            # If the file's contents claim a different name, the filename wins.
            obj["agent"] = name

            # If any other field is missing, fill in a safe default so Sun
            # never chokes on a half-written file.
            obj.setdefault("status", "warn")
            obj.setdefault("summary", "")
            obj.setdefault("detail", {})
            obj.setdefault("stale_after_hours", None)
            obj.setdefault("updated_epoch", 0)
            obj.setdefault("updated_utc", None)
            out.append(obj)

        except Exception as exc:
            # If a file is corrupt or unreadable, DON'T crash the whole email.
            # Instead, add a "fail" row so you can SEE that something's wrong.
            out.append({
                "agent": name,
                "status": "fail",
                "summary": "section unreadable: %s" % (exc.__class__.__name__,),
                "detail": {"error": str(exc)},
                "stale_after_hours": None,
                "updated_utc": None,
                "updated_epoch": 0,
            })

    return out
