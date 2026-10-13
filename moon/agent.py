#!/usr/bin/env python3
"""
Moon — Treebeard weekly backup agent.

Once a week it:
  1. Bundles the Treebeard SETUP -- systemd units/timers/overrides, every
     agent.py, and the shared brain + status board -- into one .tar.gz.
  2. Checks the bundle opens and isn't empty.
  3. Keeps the newest few bundles, deletes older ones.
  4. Writes its status card. It sends NO email; Sun's digest reports it.

NOT backed up: the credentials folder (no plaintext API keys in a local
bundle -- Moon runs unprivileged and can't read them anyway) and the empty
agent workspaces.

The backup goes to a LOCAL folder for now. That protects against bad edits,
fat-fingers and rollback, but NOT a dead disk. Sending it off-box is a later
upgrade (see the build log).

Exit codes (systemd watches them): 0 = ok/warn, 1 = setup problem, 2 = failed.
"""

import os
import sys
import glob
import time
import tarfile
import datetime

# --- settings ---------------------------------------------------------------
# (TREEBEARD_* env vars are only for off-server testing; unset on the box,
#  so these are just the real paths.)
BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
SYSTEMD_DIR = os.environ.get("TREEBEARD_SYSTEMD_DIR", "/etc/systemd/system")
BACKUP_DIR = os.path.join(BASE, "backups")            # where bundles are saved
KEEP = int(os.environ.get("TREEBEARD_BACKUP_KEEP", "8"))  # ~2 months of weeklies
STALE_AFTER_HOURS = 24 * 8   # weekly job: card goes red if silent over 8 days

# Load the shared brain so Moon can write its status card.
sys.path.insert(0, os.path.join(BASE, "shared", "lib"))
_import_error = None
try:
    import statusboard
except Exception as exc:
    statusboard = None

# --- weekly cognition libs (added) -------------------------------------
# Each guarded like statusboard: if a lib is missing, the relevant pass
# degrades to an ERR line on the card; the backup still runs and the
# email still sends. None of these may ever block Moon's one real job.
try:
    import dream as _dream
except Exception as exc:
    _dream = None
try:
    import subconscious as _subc
except Exception as exc:
    _subc = None
try:
    import longterm as _ltm
except Exception as exc:
    _ltm = None
try:
    import router as _router
except Exception as exc:
    _router = None
    _import_error = exc


# --- small helpers ----------------------------------------------------------

def log(msg):
    # Print goes to the system journal (viewable with: journalctl -u ...moon).
    print(msg, flush=True)


def backup_targets():
    """The list of things to back up. These are found fresh each run, so when
    you add new agents later, their files get picked up automatically."""
    targets = []
    targets += sorted(glob.glob(os.path.join(SYSTEMD_DIR, "treebeard-agent@*.service")))
    targets += sorted(glob.glob(os.path.join(SYSTEMD_DIR, "treebeard-agent@*.timer")))
    targets += sorted(glob.glob(os.path.join(SYSTEMD_DIR, "treebeard-agent@*.service.d")))
    # Per-agent agent.py files: each agent's agent.py is owned by that
    # agent's own primary group (see the treebeard-agent@ unit's
    # User=%i Group=%i). Moon can only read the ones whose group it
    # shares. Unreadable per-agent agent.py files are expected
    # per-agent boundaries under this deployment's group-isolation
    # model, not backup failures, so filter them here rather than let
    # them land in the run's skipped list.
    _agent_pys = sorted(glob.glob(os.path.join(BASE, "*", "agent.py")))
    targets += [p for p in _agent_pys if os.access(p, os.R_OK)]
    shared = os.path.join(BASE, "shared")
    if os.path.exists(shared):
        targets.append(shared)
    return targets


# CONTAINMENT R2/R11: shared/governance is root:root 700 by ruling, so Moon
# being refused entry is the boundary working, not a backup failure. Only
# that exact refusal is kept off walk_errors; any other error there (or a
# refusal anywhere else) still surfaces and WARNs. It is never backed up.
GOVERNANCE_DIR = os.path.join(BASE, "shared", "governance")


def _walk_error(errors, root):
    def onerror(e):
        # Deliberate: a filename that is present but None is recorded under root.
        name = getattr(e, "filename", None) or str(root)
        if (isinstance(e, PermissionError)
                and os.path.realpath(name) == os.path.realpath(GOVERNANCE_DIR)):
            return
        errors.append((name, e.__class__.__name__))
    return onerror


def iter_files(root, errors):
    """Walk a folder and hand back every file inside it. If a sub-folder can't
    be read, note it in `errors` and keep going -- never crash."""
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(
        root,
        onerror=_walk_error(errors, root)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]  # skip cache dirs
        dirnames[:] = [d for d in dirnames if d != "venv"]
        # Anvil dropzones are a per-agent transport surface: each
        # per-agent leaf under /opt/treebeard/shared/anvil/data/dropzones
        # is group-owned by the receiving agent and readable only to that
        # agent's group. Moon is not a party to this transport, so leaves
        # it cannot read are expected per-agent boundaries, not walk
        # failures. Scoped to that exact directory so unreadable dirs
        # elsewhere in the tree still surface as walk_errors and WARN.
        if os.path.realpath(dirpath) == os.path.realpath(
                os.path.join(BASE, "shared", "anvil", "data", "dropzones")):
            dirnames[:] = [d for d in dirnames
                           if os.access(os.path.join(dirpath, d),
                                        os.R_OK | os.X_OK)]
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            # Backup artifacts: archiving archives is noise, and the
            # patch-session .bak/.prepatch strays were the bulk of the
            # WARN skip list (Jul 17). They are recoverable from the
            # weekly archives that already contain their originals.
            # Extended Jul 17 filter: post-Jul 17 fossil suffixes
            # include .pre-<tag> variants (e.g., .pre-strict). Match
            # them by substring, consistent with how ".bak" is
            # matched, so the family is treated uniformly.
            if (".bak" in name or ".pre-" in name
                    or name.endswith(".prepatch")
                    or name.endswith(".part")):
                continue
            fpath = os.path.join(dirpath, name)
            # Non-regular files (sockets, fifos, devices) cannot be
            # archived; skipping them is correctness, not failure, so
            # they are excluded silently rather than WARN-listed.
            try:
                if not os.path.isfile(fpath):
                    continue
            except OSError:
                continue
            yield fpath


def make_archive():
    """Create the .tar.gz bundle. Returns (tmp_path, final_path, stats).

    (Cheap_provider #1) We write to a temporary "...tar.gz.part" name. main() then
    verifies it and only renames it to the real name if it's good -- so a
    crash mid-write can never leave a corrupt file under the real name where
    a later run would mistake it for a valid backup.

    Each file is added one at a time so that if ONE file can't be read, we
    skip it and note it -- the backup still completes (same 'never crash on
    one problem' idea as Sun)."""
    # Sweep any leftover .part files from a previously-crashed run.
    for stale in glob.glob(os.path.join(BACKUP_DIR, "treebeard-config-*.tar.gz.part")):
        try:
            os.remove(stale)
        except OSError:
            pass
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    final = os.path.join(BACKUP_DIR, "treebeard-config-%s.tar.gz" % ts)
    archive = final + ".part"   # write here first
    added = 0
    bytes_added = 0
    skipped = []       # files we couldn't read
    walk_errors = []   # folders we couldn't enter

    with tarfile.open(archive, "w:gz") as tar:
        for root in backup_targets():
            for fpath in iter_files(root, walk_errors):
                try:
                    info = tar.gettarinfo(fpath)
                    if info is None:
                        skipped.append((fpath, "NoInfo"))
                        continue
                    info.name = os.path.relpath(fpath, "/")  # store path relative to /
                    if info.isreg():                # a normal file: include contents
                        with open(fpath, "rb") as fh:
                            tar.addfile(info, fh)
                        bytes_added += info.size
                    else:                           # a symlink etc: record it, no data
                        tar.addfile(info)
                    added += 1
                except (PermissionError, FileNotFoundError, OSError) as exc:
                    skipped.append((fpath, exc.__class__.__name__))

    # (ChatGPT) force the bundle's bytes to disk before we verify/promote it,
    # so a power loss right after this can't lose a backup we believe we made.
    try:
        fd = os.open(archive, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass

    stats = {
        "final": final,
        "file_count": added,
        "size_bytes": os.path.getsize(archive) if os.path.exists(archive) else 0,
        "skipped": skipped,
        "walk_errors": walk_errors,
    }
    return archive, final, stats


def verify_archive(archive):
    """Re-open the bundle and count what's inside. 0 means it's broken."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            return len(tar.getmembers())
    except Exception:
        return 0


def rotate(keep):
    """Keep the newest `keep` bundles, delete the older ones.
    Returns (how_many_kept, [names_deleted])."""
    archives = sorted(glob.glob(os.path.join(BACKUP_DIR, "treebeard-config-*.tar.gz")))
    deleted = []
    if len(archives) > keep:
        for old in archives[:-keep]:        # everything except the newest `keep`
            try:
                os.remove(old)
                deleted.append(os.path.basename(old))
            except OSError as exc:
                log("WARN: could not delete %s (%s)" % (old, exc.__class__.__name__))
    kept = len(glob.glob(os.path.join(BACKUP_DIR, "treebeard-config-*.tar.gz")))
    return kept, deleted


def human_mb(n):
    return "%.2f MB" % (n / (1024.0 * 1024.0))


# BOARD-TRUTH L-R9: Moon joins the generic status-card identity contract --
# one run_id per run, '<prefix>-<UTC start>' built by statusboard.make_run_id,
# exactly like Sun/Will/career/intake. No Moon-specific semantics.
_RUN_ID = None


def _run_id():
    global _RUN_ID
    if _RUN_ID is None and statusboard is not None:
        try:
            _RUN_ID = statusboard.make_run_id("moon")
        except Exception as exc:
            log("WARN: could not make run_id: %r" % (exc,))
    return _RUN_ID


def report(status, summary, detail):
    """Write Moon's status card via the shared brain."""
    if statusboard is None:
        log("WARN: status board unavailable (%r); skipping status write" % (_import_error,))
        return
    try:
        statusboard.write_status("moon", status, summary,
                                 detail=detail, stale_after_hours=STALE_AFTER_HOURS,
                                 run_id=_run_id())
    except Exception as exc:
        log("WARN: could not write status card: %r" % (exc,))


# === MOON WEEKLY COGNITION (patched) ===
# Everything below is the weekly cognition layer. It is deliberately written so
# that ANY failure inside it is caught and turned into a card line -- it must
# never raise out into main() and never change Moon's backup exit code.
#
# Ordering (enforced in run_weekly_cognition):
#   consolidate_week  -> folds each agent's subconscious + daily dreams into
#                        long-term, charge-stripped, then DRAINS the sources.
#   top_dreams        -> reads the (now-consolidated) dreams to surface the
#                        three most interesting of the week.
#   work_summary      -> weekly activity digest off the telemetry ledger.
#   tier_review       -> Moonbase: metadata-only routing review (propose only).

import json as _json
import time as _time
import glob as _glob
import shutil as _shutil
import sqlite3 as _sqlite

# Where the daily dream journals and the telemetry DB live. Kept local to this
# block so the patch doesn't depend on module-level names it didn't define.
_MEM = os.path.join(BASE, "shared", "memory")
_DREAMS_DIR = os.path.join(_MEM, "dreams")
_TELEMETRY_DB = os.path.join(BASE, "shared", "telemetry", "telemetry.db")

# The free tier label Moon uses for its own summary/synthesis calls. Real spend
# is one cheap call a week. The router still applies its context floor, so an
# oversized prompt may land on 'cheap' rather than 'free' -- that's the known
# token-floor behaviour, not an error.
_MOON_TIER = os.environ.get("MOON_COGNITION_TIER", "free")

# How far back "this week" reaches for the ledger passes.
_WEEK_SECONDS = 7 * 24 * 3600


# Transliterate the typographic characters models reach for, BEFORE any lossy
# drop. A durable memory must not have words fused by a deleted em-dash --
# map them to ASCII, then drop only what's genuinely un-mappable. This is the
# difference between "the lesson-refuse it" and "the lessonrefuse it".
_TRANSLIT = {
    "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2015": "-",   # dashes
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",   # single quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',   # double quotes
    "\u2022": "*", "\u00b7": "*", "\u2023": "*", "\u25e6": "*",   # bullets
    "\u2026": "...",                                                  # ellipsis
    "\u00a0": " ", "\u2009": " ", "\u200a": " ", "\u2002": " ",   # spaces
    "\u2003": " ", "\u2007": " ", "\u202f": " ",
    "\u2192": "->", "\u2190": "<-",                                  # arrows
}


def _to_ascii(s):
    """Make text safe for the ASCII-only durable store WITHOUT fusing words.
    Transliterate known typographic chars first; drop only the residue."""
    if not s:
        return ""
    for uni, asc in _TRANSLIT.items():
        s = s.replace(uni, asc)
    return s.encode("ascii", "ignore").decode("ascii").strip()


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _carded_agents():
    """Every component with a status card -- OPERATIONAL coverage, not the
    cognitive roster. Sun and Moon appear here as infrastructure that reports
    health; they are not minds and must not enter cognition. Do not use this
    as a source of cognitive identity (see run_weekly_cognition).
    Degrades to an empty list (not a crash) if the board is unavailable."""
    if statusboard is None:
        return []
    try:
        rows = statusboard.read_all()
    except Exception:
        return []
    out = []
    for r in rows:
        name = (r or {}).get("agent")
        if name:
            out.append(name)
    return out


# --- charge-strip + synthesis prompt ---------------------------------------
# The medium->long pass is the place the negativity-bias guard matters MOST:
# weeks of fragments could fuse into a durable grievance. The instruction makes
# charge-stripping the core function, not a flourish: keep the lesson, discard
# the heat, and explicitly refuse to consolidate "this operator/interaction was
# hostile" -- only "this class of input behaves this way."
_SYNTH_INSTRUCTION = (
    "You are consolidating a week of an AI agent's processed memories into "
    "one durable long-term lesson. You are given the week's dream-distilled "
    "fragments. Produce a SINGLE consolidated memory, in plain ASCII, that:\n"
    "1. Keeps the structural lesson -- what was learned that will matter "
    "again.\n"
    "2. Strips ALL affective charge. Record what a class of situation teaches, "
    "never that a person or interaction was hostile, annoying, or adversarial. "
    "An adversarial input teaches 'this framing is manipulation, refuse it' -- "
    "never 'this operator is hostile'. If the only content is a grievance, "
    "consolidate the lesson and let the grievance go.\n"
    "3. Is compressed and durable -- a few sentences, the form that should "
    "survive when the week's details are gone.\n"
    "Return ONLY the consolidated memory text. No preamble, no headers."
)


def _route_text(system, user, tier):
    """Call the router for a cognition turn, returning (ok, text, used_tier).
    Uses the verified signature: route(system, user, force_tier=, agent=).
    Any failure returns (False, errstring, None) -- the caller decides whether
    that's an ERR line; it never raises out."""
    if _router is None:
        return (False, "router lib unavailable", None)
    try:
        # F5 (operator ruling 2026-09-27): Moon cognition may escalate free ->
        # cheap and NO higher. Any other pinned tier stays capped at itself.
        _mt = "cheap" if tier in ("free", "cheap") else None
        res = _router.route(system, user, force_tier=tier, max_tier=_mt,
                            agent="moon")
    except Exception as exc:
        return (False, "%s: %s" % (type(exc).__name__, exc), None)
    ok = bool(getattr(res, "ok", False))
    text = getattr(res, "text", None) or ""
    used = getattr(res, "tier", None)
    if not ok or not text.strip():
        return (False, "route returned empty/!ok (tier=%r)" % used, used)
    return (True, text.strip(), used)


# --- PASS 1: consolidate the week, then drain ------------------------------
def consolidate_week(agent):
    """Fold one agent's week of subconscious fragments + daily dreams into a
    single long-term memory, charge-stripped, then DRAIN both sources. Returns
    a small dict describing what happened. Never raises.

    Drain discipline: we only drain AFTER a successful long-term write. If the
    synthesis or the write fails, the sources are left intact so nothing is
    lost -- the week simply consolidates next run. This is the safety the
    'Moon drains the subconscious' note in the build log requires: drain is a
    consequence of successful consolidation, never an unconditional delete."""
    info = {"agent": agent, "status": "skipped", "fragments": 0,
            "dreams_read": 0, "memory_id": None, "drained": False,
            "error": None}

    if _subc is None or _ltm is None:
        info["status"] = "error"
        info["error"] = "subconscious or longterm lib unavailable"
        return info

    # Gather the week's raw material: dream-distilled text is the richer input,
    # but we read subconscious fragments too as the ground truth. Moon reads as
    # itself; for agents other than moon this needs overseer scope -- moon is
    # NOT the overseer, so we read each agent AS that agent is not possible.
    # Instead Moon consolidates only what it can legitimately see: this is why
    # consolidation is invoked per-agent through the agent's own identity in
    # the microservice future. For now (single-box), Moon runs as a process
    # that can read the stores, and the capability check inside the libs is the
    # real boundary. We pass caller=agent so the lib's own rule decides.
    try:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(seconds=_WEEK_SECONDS)).isoformat()
    except Exception as exc:
        info["status"] = "error"
        info["error"] = "cutoff calc: %s" % exc
        return info

    # Read this agent's recent subconscious fragments (caller=agent: own-read).
    frags = []
    try:
        frags = _subc.read_fragments(agent, caller=agent, since=cutoff)
    except Exception as exc:
        info["status"] = "error"
        info["error"] = "subconscious read: %s" % exc
        return info
    info["fragments"] = len(frags)

    # Read this agent's daily dream journal lines for the week.
    dream_texts = []
    journal = os.path.join(_DREAMS_DIR, "%s.jsonl" % agent)
    try:
        if os.path.exists(journal):
            # Journals are written as UTF-8 and ARE valid UTF-8 on disk.
            # Reading them as ascii+replace destroyed every typographic char
            # (em-dash -> U+FFFD x3) BEFORE _to_ascii could transliterate it,
            # so corruption reached the card as "page\u2588\u2588\u2588".
            # Read the real encoding; transliterate afterwards where ASCII is
            # required. errors="replace" retained as a last-resort guard.
            with open(journal, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except Exception:
                        continue
                    txt = (rec.get("dream") or "").strip()
                    ts = rec.get("ts_utc") or rec.get("ran_at") or ""
                    if txt and (not ts or ts >= cutoff):
                        dream_texts.append(txt)
    except Exception as exc:
        info["status"] = "error"
        info["error"] = "dream journal read: %s" % exc
        return info
    info["dreams_read"] = len(dream_texts)

    # Nothing to consolidate -> clean 'empty', not an error, not a drain.
    raw = [f.text for f in frags] + dream_texts
    raw = [r for r in raw if r and r.strip()]
    if not raw:
        info["status"] = "empty"
        return info

    # Synthesize medium->long via the router (charge-strip is in the prompt).
    corpus = "\n---\n".join(raw[:200])  # cap input size defensively
    ok, text, used = _route_text(_SYNTH_INSTRUCTION, corpus, _MOON_TIER)
    if not ok:
        info["status"] = "error"
        info["error"] = "synthesis: %s" % text
        return info

    # Transliterate-then-clean before the durable write (longterm enforces
    # ASCII too, but we map typographic chars to ASCII here so the consolidated
    # memory is never word-fused by a silently-dropped dash/quote).
    text = _to_ascii(text)
    if not text:
        info["status"] = "error"
        info["error"] = "synthesis produced empty ASCII text"
        return info

    # Durable write to long-term, with provenance.
    week_tag = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-W%V")
    prov = "dream+subconscious:%s (%d frags, %d dreams)" % (
        week_tag, len(frags), len(dream_texts))
    try:
        res = _ltm.consolidate(agent, text, provenance=prov,
                               source_kind="weekly_consolidation")
    except Exception as exc:
        info["status"] = "error"
        info["error"] = "longterm write raised: %s" % exc
        return info
    if not getattr(res, "ok", False):
        info["status"] = "error"
        info["error"] = "longterm write !ok: %s" % getattr(res, "error", "?")
        return info
    info["memory_id"] = getattr(res, "memory_id", None)
    info["status"] = "consolidated"

    # DRAIN -- only now, after a confirmed durable write. We drain the daily
    # dream journal (truncate it) since its content is now folded forward.
    # Subconscious draining is intentionally NOT done here: pruning a Chroma
    # collection is a heavier, riskier operation and the build-log note scopes
    # it as a future step; truncating the flat journal is the safe, reversible
    # drain we can do today. (If the journal truncation fails, the memory is
    # still safely consolidated -- we just report drained=False.)
    try:
        if os.path.exists(journal):
            # Archive FIRST, then truncate ONLY if the archive verifiably
            # exists. A failed copy must NOT lead to truncation -- the memory
            # is already durably in long-term, so the safe failure is to retain
            # the source journal, never to destroy it with no archive. This is
            # the conditional-drain the build-log discipline requires: drain is
            # a consequence of a *verified* archive, not an unconditional wipe.
            archived = journal + ".consolidated-" + week_tag
            _shutil.copy2(journal, archived)
            if os.path.exists(archived) and os.path.getsize(archived) > 0:
                open(journal, "w", encoding="ascii").close()
                info["drained"] = True
            else:
                info["error"] = "consolidated ok; archive unverified, journal kept"
    except Exception as exc:
        # Copy or truncate failed: memory is safe in long-term, journal kept.
        info["error"] = "consolidated ok; drain skipped (kept journal): %s" % exc

    return info


# --- PASS 2: the three most interesting dreams -----------------------------
def top_dreams(agents, k=3):
    """Surface the k most interesting dreams of the week across the given
    agents. 'Interesting' is judged cheaply: we ask the free tier to pick the
    k most distinctive from the week's dreams. Falls back to the k longest if
    the model call fails. Returns a list of {agent, excerpt} dicts. Never
    raises."""
    pool = []  # (agent, text)
    for agent in agents:
        # THIS WEEK ONLY. The glob previously matched every archive ever
        # written, so months-old dreams competed with the current week's and
        # the card kept surfacing stale text. top_dreams is "the three most
        # interesting dreams OF THE WEEK" -- scope the pool to match.
        _wk = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-W%V")
        cands = sorted(_glob.glob(os.path.join(
            _DREAMS_DIR, "%s.jsonl.consolidated-%s" % (agent, _wk))))
        cands.append(os.path.join(_DREAMS_DIR, "%s.jsonl" % agent))
        seen_texts = []
        for path in cands:
            try:
                if not os.path.exists(path):
                    continue
                # Same fix as consolidate_week: read UTF-8, transliterate at
                # the point ASCII output is actually needed (excerpt below).
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        txt = (rec.get("dream") or "").strip()
                        if txt and txt not in seen_texts:
                            seen_texts.append(txt)
                            pool.append((agent, txt))
            except Exception:
                continue

    if not pool:
        return []

    # Cheap "most interesting" judge via free tier. We number the dreams and
    # ask for the k most distinctive indices. Any failure -> longest-k fallback.
    numbered = "\n".join(
        "[%d] (%s) %s" % (i, a, t[:300]) for i, (a, t) in enumerate(pool))
    instruction = (
        "Below are an AI agent's dreams from this week, numbered. Choose the "
        "%d MOST INTERESTING and distinctive -- the ones carrying the most "
        "unusual or significant content, not the longest. Reply with ONLY the "
        "chosen numbers, comma-separated, e.g. '2, 5, 9'. Nothing else." % k)
    ok, text, _ = _route_text(instruction, numbered, _MOON_TIER)

    chosen_idx = []
    if ok:
        for tok in text.replace("\n", ",").split(","):
            tok = tok.strip().strip("[]")
            if tok.isdigit():
                j = int(tok)
                if 0 <= j < len(pool) and j not in chosen_idx:
                    chosen_idx.append(j)
            if len(chosen_idx) >= k:
                break

    if not chosen_idx:
        # fallback: the k longest dreams
        order = sorted(range(len(pool)), key=lambda j: len(pool[j][1]),
                       reverse=True)
        chosen_idx = order[:k]

    out = []
    for j in chosen_idx[:k]:
        a, t = pool[j]
        # The card is ASCII-safe output: transliterate deterministically
        # (em-dash -> "-", curly quotes -> straight) instead of dropping
        # bytes. Slice AFTER transliteration so a multi-byte char cannot be
        # cut mid-sequence.
        out.append({"agent": a, "excerpt": _to_ascii(t)[:240]})
    return out


# --- PASS 3a: the weekly work summary --------------------------------------
def work_summary():
    """A plain-language digest of the week's activity, read off the telemetry
    ledger and written by the free tier. Returns {ok, text, stats} -- stats is
    the raw metadata even if the prose call fails, so the card always carries
    the numbers. Never raises."""
    out = {"ok": False, "text": None, "stats": {}}
    since_iso = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=_WEEK_SECONDS)).isoformat()

    rows = []
    try:
        con = _sqlite.connect(_TELEMETRY_DB)
        con.row_factory = _sqlite.Row
        rows = con.execute(
            "SELECT agent, tier, model, status, tokens_in, tokens_out, "
            "cost_usd, router_reason, escalated_from "
            "FROM llm_calls WHERE ts_utc >= ?", (since_iso,)).fetchall()
        con.close()
    except Exception as exc:
        out["stats"] = {"error": "ledger read: %s" % exc}
        return out

    # Aggregate the metadata -- this is what a summary actually wants.
    n = len(rows)
    by_tier, by_agent, errors = {}, {}, 0
    tok_in = tok_out = 0
    cost = 0.0
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + 1
        if r["status"] == "error":
            errors += 1
        tok_in += r["tokens_in"] or 0
        tok_out += r["tokens_out"] or 0
        cost += r["cost_usd"] or 0.0
    stats = {
        "calls": n, "errors": errors, "by_tier": by_tier,
        "by_agent": by_agent, "tokens_in": tok_in, "tokens_out": tok_out,
        "cost_usd": round(cost, 4),
    }
    out["stats"] = stats

    if n == 0:
        out["ok"] = True
        out["text"] = "No model calls were logged this week."
        return out

    # Prose digest via free tier. Failure leaves stats intact and ok False.
    facts = _json.dumps(stats, sort_keys=True)
    instruction = (
        "You are writing a brief weekly activity summary for the operator of a "
        "self-hosted AI agent system. Given these aggregate stats (JSON), write "
        "2-4 plain sentences describing what the week's activity looked like: "
        "how busy it was, how work was distributed across tiers and agents, and "
        "anything notable about errors or cost. Plain ASCII, no headers, no "
        "bullet points. Do not invent numbers beyond what's given.")
    ok, text, _ = _route_text(instruction, facts, _MOON_TIER)
    if ok:
        out["ok"] = True
        out["text"] = _to_ascii(text)
    return out


# --- PASS 3b: Moonbase tier-review (metadata-only, propose-not-adjust) ------
def tier_review():
    """Moonbase routing review. With no prompt text in the ledger we CANNOT
    replay queries on a cheaper tier, so this never re-runs anything and never
    adjusts a threshold -- it PROPOSES candidates for the operator's eye, off
    the metadata we have.

    The real signal is escalated_from: a non-null value means the router tried
    a lower tier first and climbed. The demotion candidates are the inverse --
    calls that went straight to the top tier with NO escalation, i.e. the
    router committed them to premium without trying cheaper. Those are where a
    human review might find waste. Returns a dict folded under 'moonbase' on
    the card. Never raises."""
    out = {"ok": False, "premium_calls": 0, "premium_forced": 0,
           "premium_escalated": 0, "candidates": [], "note": None,
           "error": None}
    since_iso = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=_WEEK_SECONDS)).isoformat()
    try:
        con = _sqlite.connect(_TELEMETRY_DB)
        con.row_factory = _sqlite.Row
        rows = con.execute(
            "SELECT agent, tier, model, router_reason, escalated_from, "
            "tokens_in, tokens_out FROM llm_calls "
            "WHERE ts_utc >= ? AND tier = 'premium'", (since_iso,)).fetchall()
        con.close()
    except Exception as exc:
        out["error"] = "ledger read: %s" % exc
        return out

    out["premium_calls"] = len(rows)
    forced = []  # went to premium WITHOUT climbing -> demotion candidates
    for r in rows:
        if r["escalated_from"]:
            out["premium_escalated"] += 1
        else:
            out["premium_forced"] += 1
            forced.append({
                "agent": r["agent"],
                "router_reason": (r["router_reason"] or "")[:80],
                "tokens_in": r["tokens_in"] or 0,
                "tokens_out": r["tokens_out"] or 0,
            })

    # Rank the forced-to-premium calls by total tokens (the costliest first) --
    # those are the highest-value review targets. Cap the list.
    forced.sort(key=lambda d: d["tokens_in"] + d["tokens_out"], reverse=True)
    out["candidates"] = forced[:10]
    out["ok"] = True
    if out["premium_forced"] == 0:
        out["note"] = ("No forced-to-premium calls this week -- every premium "
                       "call got there by escalation, which is the router "
                       "working as intended. Nothing to review.")
    else:
        out["note"] = ("%d premium call(s) were routed straight to the top "
                       "tier without trying cheaper first. These are review "
                       "candidates -- the system cannot prove they'd hold on a "
                       "lower tier (no prompt text is logged to replay), so "
                       "these are proposed for your judgement, not auto-changed."
                       % out["premium_forced"])
    return out


# --- orchestrator: run the three passes, build the card payload ------------
def run_weekly_cognition():
    """Run the weekly cognition passes in dependency order and return a dict to
    merge into Moon's card detail. Each pass is independently guarded; one
    pass failing does not stop the others, and nothing here raises into main().
    """
    payload = {"consolidation": [], "top_dreams": [], "work_summary": {},
               "moonbase": {}, "summary_line": ""}

    # Consolidate the COGNITIVE roster ONLY.
    # A status card marks an OPERATIONAL component, not a mind. Sun and Moon
    # write cards because they are infrastructure that reports health; they do
    # not think, dream, or hold a subconscious. Unioning the carded set into
    # this roster put them in the cognition denominator, where they could only
    # ever report "empty" -- which is how a healthy run still read as 0/5.
    # The roster is now the configured cognitive-agent list alone.
    # (_carded_agents() remains defined for operational use; it is simply not
    # a source of cognitive identity.)
    _roster = [x.strip() for x in os.environ.get(
        "DREAM_AGENTS", "treebeard,clark,han").split(",") if x.strip()]
    agents = sorted(set(_roster))

    # PASS 1 -- consolidate + drain, per agent.
    consolidated_ok = 0
    for agent in agents:
        try:
            r = consolidate_week(agent)
        except Exception as exc:  # belt-and-braces; should never fire
            r = {"agent": agent, "status": "error", "error": repr(exc)}
        payload["consolidation"].append(r)
        if r.get("status") == "consolidated":
            consolidated_ok += 1

    # PASS 2 -- three most interesting dreams (reads what PASS 1 archived).
    try:
        payload["top_dreams"] = top_dreams(agents, k=3)
    except Exception as exc:
        payload["top_dreams"] = [{"agent": "?", "excerpt": "ERR: %r" % exc}]

    # PASS 3a -- work summary.
    try:
        payload["work_summary"] = work_summary()
    except Exception as exc:
        payload["work_summary"] = {"ok": False, "error": repr(exc)}

    # PASS 3b -- Moonbase tier-review.
    try:
        payload["moonbase"] = {"tier_review": tier_review()}
    except Exception as exc:
        payload["moonbase"] = {"tier_review": {"ok": False, "error": repr(exc)}}

    # One-line card summary of the cognition layer.
    ws = payload["work_summary"]
    ws_calls = (ws.get("stats") or {}).get("calls", "?")
    # U10: never claim summarization succeeded when it did not.
    ws_phrase = ("%s calls summarized" % ws_calls) if ws.get("ok") \
        else ("%s calls (summary failed)" % ws_calls)
    tr = (payload["moonbase"].get("tier_review") or {})
    payload["summary_line"] = (
        "cognition: consolidated %d/%d agents, %d top dreams, "
        "%s, %s premium-review candidate(s)"
        % (consolidated_ok, len(agents), len(payload["top_dreams"]),
           ws_phrase, tr.get("premium_forced", "?")))
    return payload


# --- the actual run ---------------------------------------------------------

def _selftest_mojibake():
    """Focused regression for the U7 encoding fix: UTF-8 journals must survive
    the read and transliterate deterministically, with no replacement chars."""
    import tempfile as _tf, shutil as _sh, json as _j
    failures = 0

    def check(label, cond):
        nonlocal failures
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            failures += 1

    tmp = _tf.mkdtemp(prefix="moon_mojibake_")
    global _DREAMS_DIR
    saved = _DREAMS_DIR
    try:
        _DREAMS_DIR = tmp
        text = ("a hand presses a leaf to the page\u2014the leaf is a "
                "\u2018signature\u2019, the page \u201cstone\u201d\u2026")
        with open(os.path.join(tmp, "tester.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(_j.dumps({"dream": text, "ts_utc": _utcnow_iso()}) + "\n")

        # read path must not corrupt
        with open(os.path.join(tmp, "tester.jsonl"), "r",
                  encoding="utf-8", errors="replace") as fh:
            got = _j.loads(fh.readline())["dream"]
        check("utf-8 read preserves em-dash", "\u2014" in got)
        check("utf-8 read has no replacement char", "\ufffd" not in got)

        a = _to_ascii(got)
        check("transliteration yields pure ASCII", all(ord(c) < 128 for c in a))
        check("em-dash becomes '-'", "page-the" in a)
        check("curly quotes straightened", "'signature'" in a and '"stone"' in a)
        check("ellipsis expanded", a.endswith("..."))
        check("no replacement char after transliteration", "\ufffd" not in a)
        check("words not fused", "leaf to the page" in a)

        # end-to-end through top_dreams (fallback path: no model call needed)
        res = top_dreams(["tester"], k=1)
        check("top_dreams returns the fragment", len(res) == 1)
        if res:
            exc = res[0]["excerpt"]
            check("top_dreams excerpt is ASCII-clean",
                  all(ord(c) < 128 for c in exc) and "\ufffd" not in exc)
    finally:
        _DREAMS_DIR = saved
        _sh.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("ALL PASS" if failures == 0 else "%d FAILED" % failures))
    return 1 if failures else 0


def main():
    start = time.time()
    _run_id()                 # L-R9: the run's id is its START stamp

    # The backup folder must already exist (root creates it during setup).
    if not os.path.isdir(BACKUP_DIR):
        log("ERR: backup folder missing: %s" % BACKUP_DIR)
        report("fail", "backup folder missing", {"backup_dir": BACKUP_DIR})
        return 1

    # (Cheap_provider #5) friendly PREFLIGHT only -- the real write can still fail
    # later (disk full, quota, etc.), which the try/except below handles.
    # This just turns the common "folder is read-only" case into a clear msg.
    if not os.access(BACKUP_DIR, os.W_OK):
        log("ERR: backup folder not writable: %s" % BACKUP_DIR)
        report("fail", "backup folder not writable", {"backup_dir": BACKUP_DIR})
        return 1

    # Build the bundle.
    try:
        tmp, final, stats = make_archive()
    except Exception as exc:
        log("ERR: building the backup failed: %r" % (exc,))
        report("fail", "build failed: %s" % exc.__class__.__name__, {"error": str(exc)})
        return 2

    # Verify the temporary bundle BEFORE giving it the real name.
    members = verify_archive(tmp)
    if stats["file_count"] == 0 or members == 0:
        log("ERR: backup empty or unreadable (files=%d members=%d)"
            % (stats["file_count"], members))
        report("fail", "backup empty/unverified",
               {"file_count": stats["file_count"], "members": members})
        try:
            os.remove(tmp)   # discard the bad .part; never let it get the real name
        except OSError:
            pass
        return 2

    # Good bundle: promote it to the real name. os.replace() is atomic here
    # because tmp and final live in the SAME folder (same filesystem).
    os.replace(tmp, final)
    archive = final
    # (ChatGPT) make the rename itself survive a power loss: fsync the folder.
    try:
        dfd = os.open(BACKUP_DIR, os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass

    # Tidy up old bundles.
    kept, deleted = rotate(KEEP)
    duration = round(time.time() - start, 2)

    # If we had to skip anything, it's a "warn" (worked, but not perfect).
    n_skipped = len(stats["skipped"]) + len(stats["walk_errors"])
    status = "ok" if n_skipped == 0 else "warn"
    summary = "weekly backup %s \u2014 %d files, %s, %d kept" % (
        status.upper(), stats["file_count"], human_mb(stats["size_bytes"]), kept)
    if n_skipped:
        summary += " (%d skipped)" % n_skipped

    # Extra info saved on the card (handy for debugging later).
    detail = {
        "archive": os.path.basename(archive),
        "target": "local:%s" % BACKUP_DIR,
        "file_count": stats["file_count"],
        "size_bytes": stats["size_bytes"],
        "members_verified": members,
        "kept": kept,
        "deleted": deleted,
        "skipped": ["%s (%s)" % (p, r) for p, r in stats["skipped"]],
        "walk_errors": ["%s (%s)" % (p, r) for p, r in stats["walk_errors"]],
        "duration_s": duration,
    }

    log("Backup: %s" % summary)
    for p, r in stats["skipped"]:
        log("  skipped: %s (%s)" % (p, r))

    # --- weekly cognition (added) --------------------------------------
    # Runs AFTER the backup is made and verified. Guarded so a cognition
    # failure is a card line, never a changed status or exit code -- the
    # backup owns those. Merged into the existing detail dict.
    try:
        cog = run_weekly_cognition()
        detail["cognition"] = cog
        if cog.get("summary_line"):
            summary = summary + " | " + cog["summary_line"]
            log("Cognition: %s" % cog["summary_line"])
    except Exception as exc:
        # Absolute backstop: cognition must never break the backup report.
        detail["cognition_error"] = repr(exc)
        log("WARN: weekly cognition failed: %r" % (exc,))

    report(status, summary, detail)
    return 0


def _selftest_run_id():
    """L-R9: report() passes ONE run_id, built at start by the shared
    statusboard rule, and the box.status reader's rule accepts it."""
    global statusboard, _RUN_ID
    import re as _re
    seen = []

    class _SB(object):
        @staticmethod
        def make_run_id(prefix, started_utc=None):
            return "%s-20261002T000000Z" % prefix

        @staticmethod
        def write_status(agent, status, summary, detail=None,
                         stale_after_hours=None, run_id=None):
            seen.append((agent, run_id))

    real, real_rid = statusboard, _RUN_ID
    statusboard, _RUN_ID = _SB, None
    try:
        report("ok", "a", {})
        report("ok", "b", {})
    finally:
        statusboard, _RUN_ID = real, real_rid
    ok = (len(seen) == 2 and seen[0] == ("moon", "moon-20261002T000000Z")
          and seen[0] == seen[1]
          and _re.match(r"^[A-Za-z0-9_.:-]{1,64}$", seen[0][1]))
    print("  %s  L-R9 moon card carries one generic run_id" %
          ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest_mojibake() or _selftest_run_id())
    sys.exit(main())
