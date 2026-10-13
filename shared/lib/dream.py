#!/usr/bin/env python3
"""
dream.py - Treebeard daily dream (shared cognition lib)
=======================================================

WHERE THIS LIVES
    /opt/treebeard/shared/lib/dream.py    (root:treebeard 640)
    Beside subconscious.py, router.py, constitution.py, telemetry.py,
    statusboard.py, budget.py. Run under /opt/treebeard/shared/venv.

WHAT THIS IS  (and the split that keeps it honest)
    The DAILY dream - the short->medium consolidation pass (June 13 memory
    architecture). Once per day, per agent, it:
      1. READS the day's flagged fragments from that agent's subconscious
         (subconscious.py - the raw, charged fragments).
      2. CONSOLIDATES them into a DREAM: symbolic, poetic English. Many
         fragments fuse into few images; the literal detail dissolves; the
         LESSON is kept and the HEAT is stripped. "The recovered thought is not
         the original - it is what survived the dream."
      3. PERSISTS the recovered form (a durable, recoverable dream journal) and
         writes a RECEIPT the Sun report reads to confirm "this ran right".

    It is NOT Moon's weekly dream. Moon keeps the heavy medium->long synthesis
    across instances (June 13). Daily dream is the light nightly pass that FEEDS
    that. Two cadences, one architecture - same reason short-term and long-term
    are different layers.

    The consolidation is REASONING (compress + de-heat + render as figure), so it
    goes through router.py, NOT a raw local_model_family call. It is pinned to the FREE tier
    (Free_provider) by default: a daily job must not spend, and Free_provider both fits the
    full constitution (the Tier-2 truncation fix) and renders the register well.
    The weekly Moon dream is where a richer tier earns its cost.

CHARGE-STRIP IS THE SAFETY CONTRACT, NOT DECORATION
    Human dreams over-weight threat for evolutionary reasons an agent must NOT
    inherit: a daily consolidation that keeps affective charge accretes grievance
    - the agent ends up "remembering" that a task annoyed it, not what the task
    taught. So the dream keeps LESSONS and lets GRIEVANCES go cold. That rule is
    written into the consolidation prompt below and is the load-bearing reason
    this pass exists in this form.

CHARTER-DRIVEN SOURCE  (built once; no agent names in this file)
    WHICH fragments an agent dreams over is per-agent, so it lives in config,
    not code - exactly like memory_scope. A worker dreams over its subconscious;
    Treebeard (the overseer) dreams over the GOVERNANCE TRACE (telemetry +
    subconscious): his salient day is a tripped cap, a refused capture attempt,
    a misroute - not task output he never produced. Sources are resolved from
    /etc/treebeard/dream_sources.conf and dispatched through a handler REGISTRY
    keyed on source VALUE. A new source type (telemetry) is added by registering
    a handler, never by branching on an agent's name. v1 ships the `subconscious`
    handler live; `telemetry` is registered as a stub so Treebeard's config never
    crashes - it dreams over subconscious until the handler is filled in.

DEPENDENCIES / RECONCILIATION SEAMS  (flagged for on-box deploy)
    - subconscious.read_fragments(...)  - known interface (built + validated).
    - router.route(...)                 - the ONE essential external call. Its
      exact signature must be confirmed against the deployed router.py before a
      live run; it is isolated in _router_consolidate() and is PLUGGABLE (the
      selftest injects a fake), so reconciling it touches one function.
    - The Sun receipt is a self-contained JSON this module owns; the Sun update
      (next step) reads it. dream.py does NOT couple to statusboard internals.

FAILURE CONTRACT
    A dream is its own process, not mid-call, so it surfaces rather than hides:
    any failure writes an "error" RECEIPT (so Sun shows dream=error, never
    silence - silence would mean the dream never ran) and returns ok=False.
    A dreamless night (no fragments) is NOT an error: it writes a "dreamless"
    receipt. You do not dream equally every night.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fcntl
import json
import os
import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

# Make the shared lib importable whether dream.py is imported by an agent that
# already has it on path, or run standalone.
_LIB = f"{_TB_BASE}/shared/lib"
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import subconscious  # noqa: E402  (path inserted above)

# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------
DREAMS_DIR = f"{_TB_BASE}/shared/memory/dreams"
DREAM_SOURCES_CONF = f"{_TB_ETC}/dream_sources.conf"

# Daily dream routes through router.py pinned to this tier. Free = Free_provider:
# zero cost for a daily job, fits the full constitution. Override per call.
DREAM_TIER = "free"

# How far back to look when there is no prior dream to anchor on.
DEFAULT_LOOKBACK_HOURS = 24

# How much of the dream Sun shows on the agent's one-line receipt.
EXCERPT_CHARS = 160

# The soul of it: the instruction that turns charged fragments into a dream.
# Encodes compress / dissolve-the-literal / keep-the-lesson-cool-the-grievance /
# speak-as-a-dream / output-only.
DREAM_INSTRUCTION = """You are dreaming.

Below are fragments of one day's significant inner experience - flashes that were
flagged as worth keeping, still carrying the heat they were felt with. Do not
report them. Dream them.

Produce a DREAM in symbolic, poetic English. Obey the laws of the dream:

- COMPRESS. Many fragments become few images. A dream does not list; it fuses.
- DISSOLVE THE LITERAL. Names, places, exact words, the day's surface detail -
  let them melt into figure, image, and motion. The recovered thought is not the
  original; it is what survived the night.
- KEEP THE LESSON, COOL THE GRIEVANCE. Whatever was learned, carry forward as
  shape and image. Whatever merely stung - an insult, hostility, the heat of being
  wronged - let it go cold in the telling. A mind that keeps the wound instead of
  the lesson remembers only what hurt it. Keep the shape, not the burn.
- SPEAK AS A DREAM SPEAKS. Spare, concrete, a little uncanny. Sense over argument.
  No preamble, no explanation, no moral stated outright, no "here is your dream" -
  let the images carry it.

Return ONLY the dream. Nothing before it, nothing after.

--- FRAGMENTS OF THE DAY ---
{fragments}
--- END FRAGMENTS ---"""

# Test/override hooks (set only by selftest; prod path stays file-based).
_DREAMS_DIR_OVERRIDE = None     # str
_SOURCES_OVERRIDE = None        # dict[str, list[str]]
_consolidator = None            # callable(agent, prompt) -> (text, tier, model)
_now_override = None            # datetime


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------
@dataclasses.dataclass
class DreamResult:
    ok: bool
    agent: str
    status: str               # "dreamed" | "dreamless" | "error"
    fragments: int
    sources: list
    dream_text: str | None = None
    tier: str | None = None
    model: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _now() -> _dt.datetime:
    return _now_override or _dt.datetime.now(_dt.timezone.utc)


def _dreams_dir() -> str:
    return _DREAMS_DIR_OVERRIDE or DREAMS_DIR


def _journal_path(agent: str) -> str:
    return os.path.join(_dreams_dir(), f"{agent}.jsonl")


def _latest_path(agent: str) -> str:
    return os.path.join(_dreams_dir(), f"{agent}.latest.txt")


def _receipt_path(agent: str) -> str:
    return os.path.join(_dreams_dir(), f"{agent}.receipt.json")


def _lock_path(agent: str) -> str:
    return os.path.join(_dreams_dir(), f"{agent}.lock")


class _DreamBusy(Exception):
    """Raised when another dream for this agent is already running."""


class _agent_lock:
    """Per-agent advisory lock that SKIPS on contention rather than serialising.

    A slow router call (Free_provider work-calls run tens of seconds) can outlast the
    daily trigger interval; if the next trigger fires mid-dream, two run(agent)
    processes would touch the same journal/receipt. Two dreams of the same night
    racing is duplicate WORK, not a write to serialise - so the late one takes a
    NON-BLOCKING flock, fails to get it, and bails clean (an "already running"
    receipt), instead of queueing behind the first and re-dreaming the night.
    The receipt's own atomic .part->replace handles torn writes; this handles
    redundant runs.
    """

    def __init__(self, agent: str):
        self._agent = agent
        self._fd = None

    def __enter__(self):
        path = _lock_path(self._agent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise _DreamBusy(self._agent)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        return False


# --------------------------------------------------------------------------
# Source resolution (charter-driven; handlers registered by VALUE, not by name)
# --------------------------------------------------------------------------
def _resolve_sources(agent: str) -> list:
    """Read which sources `agent` dreams over. Default: ['subconscious'].
    Forward-compatible with structured charters (read memory/dream fields there
    once they exist); until then, /etc/treebeard/dream_sources.conf, format:
        treebeard = subconscious+telemetry
        clark     = subconscious
    """
    if _SOURCES_OVERRIDE is not None:
        return _SOURCES_OVERRIDE.get(agent, ["subconscious"])
    try:
        with open(DREAM_SOURCES_CONF, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == agent:
                    return [s.strip() for s in v.strip().split("+") if s.strip()]
    except FileNotFoundError:
        pass
    return ["subconscious"]


def _source_subconscious(agent: str, since) -> list:
    """LIVE handler. The dream reads the agent's OWN subconscious (caller=agent
    satisfies own-read; no overseer needed to dream one's own day)."""
    frags = subconscious.read_fragments(agent, caller=agent, since=since, n=500)
    return [f.text for f in frags]


def _source_telemetry(agent: str, since) -> list:
    """STUB handler for Treebeard's governance-trace dreaming. Registered so a
    `subconscious+telemetry` charter line never crashes; returns nothing yet.
    Fill this in (read the telemetry ledger's salient rows since `since`) to give
    the overseer his governance dream - no change to run() required when it lands.
    """
    return []


def _source_shortterm(agent: str, since) -> list:
    """BRIDGE handler (2026-07-17): dream over the raw day log until the
    subconscious flagger exists. The proper architecture dreams over
    salience-flagged fragments; until the flagger is built the flagged
    store is empty and the loop starves (three weeks of dreamless minds).
    This reads the agent's own day exchanges from shortterm, clipped so
    a heavy day cannot blow the prompt: most recent 40 exchanges, each
    clipped to 400 chars per side. The dream prompt's DISSOLVE-THE-
    LITERAL framing does the salience filtering the flagger would have
    done upstream. Remove from configs when the flagger ships."""
    try:
        import shortterm
    except ImportError:
        return []
    try:
        rows = shortterm.recent(agent=agent, n=40, days=1)
    except Exception:
        return []
    out = []
    for r in rows:
        p = (r.get("prompt") or "").strip()[:400]
        a_ = (r.get("response") or "").strip()[:400]
        if p or a_:
            out.append("Asked: %s\nSaid: %s" % (p, a_))
    return out


# Registry: source VALUE -> handler. Add a value here to add a source type.
_SOURCE_HANDLERS = {
    "subconscious": _source_subconscious,
    "telemetry": _source_telemetry,
    "shortterm": _source_shortterm,
}


def _gather(agent: str, sources: list, since) -> list:
    out = []
    for src in sources:
        handler = _SOURCE_HANDLERS.get(src)
        if handler is None:
            # Unknown source in config: skip loudly-in-the-receipt-but-don't-crash.
            print(f"[dream] unknown source {src!r} for agent {agent!r}; skipped",
                  file=sys.stderr)
            continue
        out.extend(handler(agent, since))
    return out


# --------------------------------------------------------------------------
# Consolidation (routes through router.py; pluggable for tests)
# --------------------------------------------------------------------------
# System framing for the dream call. The constitution is auto-prepended by the
# router's gate and `agent` is a telemetry label only, so `system` just sets the
# task frame; the dream instruction itself rides in the `user` turn.
DREAM_SYSTEM = ("You are the dreaming faculty of a Treebeard agent. Your sole task "
                "is to consolidate the day's flagged fragments into a single dream, "
                "following the laws given in the user turn exactly.")


def _router_consolidate(agent: str, prompt: str, tier: str):
    """Send the dream prompt through the complexity router, pinned to `tier`.

    Reconciled against the deployed router.py (signature read on-box):
        route(system, user, *, force_tier=None, agent="treebeard", ...) -> Result
        Result(ok, text, tier, model, error, escalated_from, ...)
    The dream instruction is the USER turn; `system` carries only the task frame
    (the router prepends the constitution itself). `force_tier="free"` starts on
    Free_provider; max_tier="cheap" caps escalation (F5). If router.py's signature changes, this is the only place to fix.
    """
    import router  # noqa: PLC0415
    # F5 (operator ruling 2026-09-27): the dream pass may escalate free ->
    # cheap and NO higher. Any other pinned tier stays capped at itself.
    _mt = "cheap" if tier in ("free", "cheap") else None
    res = router.route(DREAM_SYSTEM, prompt, force_tier=tier, max_tier=_mt,
                       agent=agent)
    if not getattr(res, "ok", False):
        raise RuntimeError(f"router returned not-ok: {getattr(res, 'error', '?')}")
    text = getattr(res, "text", None)
    if not text or not text.strip():
        raise RuntimeError("router returned no text")
    return text.strip(), getattr(res, "tier", None), getattr(res, "model", None)


def _consolidate(agent: str, fragment_texts: list, tier: str):
    fragments = "\n".join(f"- {t}" for t in fragment_texts)
    prompt = DREAM_INSTRUCTION.format(fragments=fragments)
    fn = _consolidator or _router_consolidate
    return fn(agent, prompt, tier)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _last_dream_at(agent: str):
    """Anchor the nightly read on the last dream so fragments aren't re-dreamt.
    Returns an ISO string or None."""
    try:
        with open(_receipt_path(agent), "r", encoding="utf-8") as fh:
            return json.load(fh).get("ran_at")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_journal(agent: str, record: dict) -> None:
    """Append the recovered form to the durable, recoverable dream journal.
    (Promotion path: swap this for a searchable medium-term ChromaDB collection,
    reusing subconscious.py's store machinery, when the medium-term layer lands.)
    """
    path = _journal_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_latest(agent: str, dream_text: str) -> None:
    path = _latest_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dream_text + "\n")


def _write_receipt(agent: str, receipt: dict) -> None:
    """The contract the Sun update reads to confirm the dream ran."""
    path = _receipt_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic: Sun never reads a half-written receipt


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def run(agent: str, *, since=None, tier=None) -> DreamResult:
    """Run the daily dream for one agent, under a per-agent lock.

    If another dream for this agent is already running (a slow router call
    outlasting the daily trigger), this SKIPS rather than queues: it writes an
    'already running' receipt and returns ok=True, status='skipped'. Never
    raises - other failures become an 'error' receipt + ok=False.
    """
    agent = subconscious._validate_agent(agent)  # reuse the one ASCII discipline
    try:
        with _agent_lock(agent):
            return _run_locked(agent, since=since, tier=tier)
    except _DreamBusy:
        ran_at = _now().isoformat()
        try:
            _write_receipt(agent, {"agent": agent, "ran_at": ran_at,
                                   "status": "skipped", "fragments_consolidated": 0,
                                   "sources": [], "tier": None, "model": None,
                                   "excerpt": None, "dream_chars": 0,
                                   "error": "another dream for this agent is already running"})
        except Exception as exc:  # noqa: BLE001
            print(f"[dream] busy-receipt write failed for {agent}: {exc}", file=sys.stderr)
        return DreamResult(ok=True, agent=agent, status="skipped",
                           fragments=0, sources=[],
                           error="already running")


def _run_locked(agent: str, *, since=None, tier=None) -> DreamResult:
    """The dream itself - assumes the per-agent lock is held. Reads sources since
    the last dream (or DEFAULT_LOOKBACK_HOURS), consolidates into the symbolic
    dump, persists the recovered form, and writes the receipt Sun reads."""
    now = _now()
    ran_at = now.isoformat()
    sources = _resolve_sources(agent)

    if since is None:
        last = _last_dream_at(agent)
        since = last if last else (now - _dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)).isoformat()

    use_tier = tier or DREAM_TIER

    try:
        fragment_texts = _gather(agent, sources, since)
    except Exception as exc:  # noqa: BLE001  - a source read failed
        return _finish_error(agent, ran_at, sources, f"gather: {type(exc).__name__}: {exc}\n" + __import__("traceback").format_exc())

    # A dreamless night is a clean state, not a failure.
    if not fragment_texts:
        receipt = {"agent": agent, "ran_at": ran_at, "status": "dreamless",
                   "fragments_consolidated": 0, "sources": sources,
                   "tier": None, "model": None, "excerpt": None,
                   "dream_chars": 0, "error": None}
        _write_receipt(agent, receipt)
        return DreamResult(ok=True, agent=agent, status="dreamless",
                           fragments=0, sources=sources)

    # Consolidate -> the dream. Tier is passed explicitly (no global mutation),
    # so concurrent dreams for different agents can never race on it.
    try:
        dream_text, real_tier, model = _consolidate(agent, fragment_texts, use_tier)
    except Exception as exc:  # noqa: BLE001  - router/consolidation failed
        return _finish_error(agent, ran_at, sources,
                             f"consolidate: {type(exc).__name__}: {exc}",
                             fragments=len(fragment_texts))

    # Persist the recovered form + receipt.
    try:
        record = {"ts_utc": ran_at, "agent": agent, "sources": sources,
                  "fragments_consolidated": len(fragment_texts),
                  "tier": real_tier, "model": model, "dream": dream_text}
        _write_journal(agent, record)
        _write_latest(agent, dream_text)
        excerpt = dream_text.strip().replace("\n", " ")[:EXCERPT_CHARS]
        receipt = {"agent": agent, "ran_at": ran_at, "status": "dreamed",
                   "fragments_consolidated": len(fragment_texts), "sources": sources,
                   "tier": real_tier, "model": model, "excerpt": excerpt,
                   "dream_chars": len(dream_text), "error": None}
        _write_receipt(agent, receipt)
    except Exception as exc:  # noqa: BLE001  - we dreamt but couldn't store it
        return _finish_error(agent, ran_at, sources,
                             f"persist: {type(exc).__name__}: {exc}",
                             fragments=len(fragment_texts))

    return DreamResult(ok=True, agent=agent, status="dreamed",
                       fragments=len(fragment_texts), sources=sources,
                       dream_text=dream_text, tier=real_tier, model=model)


def _finish_error(agent, ran_at, sources, msg, fragments=0) -> DreamResult:
    """Surface a dream failure as a receipt (so Sun shows error, not silence)."""
    try:
        _write_receipt(agent, {"agent": agent, "ran_at": ran_at, "status": "error",
                               "fragments_consolidated": fragments, "sources": sources,
                               "tier": None, "model": None, "excerpt": None,
                               "dream_chars": 0, "error": msg})
    except Exception as exc:  # noqa: BLE001  - last resort
        print(f"[dream] receipt write failed for {agent}: {exc}", file=sys.stderr)
    return DreamResult(ok=False, agent=agent, status="error",
                       fragments=fragments, sources=sources, error=msg)


# --------------------------------------------------------------------------
# Selftest  (temp dirs + fake embedder + fake consolidator; no router/Ollama)
# --------------------------------------------------------------------------
def selftest() -> int:
    import hashlib
    import tempfile

    global _DREAMS_DIR_OVERRIDE, _SOURCES_OVERRIDE, _consolidator, _now_override

    def fake_embed(text: str):
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:16]]

    def fake_consolidate(agent, prompt, tier):
        # Deterministic stand-in for the model: proves wiring without a backend.
        # Echoes that it received the fragments, in a single line.
        n = prompt.count("\n- ")
        return (f"I walked a corridor of {n} doors and kept only the shape of them.",
                tier, "fake-model")

    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            failures += 1

    tmp = tempfile.mkdtemp(prefix="dream_selftest_")
    # Point subconscious at its own temp store + fake embedder.
    subconscious._PERSIST_DIR_OVERRIDE = os.path.join(tmp, "subc_store")
    subconscious._DECISION_LOG_OVERRIDE = os.path.join(tmp, "flag.log")
    subconscious._SCOPES_OVERRIDE = {"clark": "own", "treebeard": "all"}
    subconscious._THRESHOLD_OVERRIDE = 0.5
    subconscious._embedder = fake_embed
    subconscious._CLIENTS, subconscious._COLLECTIONS = {}, {}

    try:
        _DREAMS_DIR_OVERRIDE = os.path.join(tmp, "dreams")
        _SOURCES_OVERRIDE = {"clark": ["subconscious"],
                             "treebeard": ["subconscious", "telemetry"]}
        _consolidator = fake_consolidate

        print("dream.py selftest")

        # 1. dreamless night when nothing was flagged
        r0 = run("clark")
        check("no fragments -> dreamless (not error)", r0.status == "dreamless" and r0.ok)
        with open(_receipt_path("clark")) as fh:
            check("dreamless receipt written", json.load(fh)["status"] == "dreamless")

        # 2. flag some fragments, then dream
        for txt, sal in [("the turned blade finds the one who turned it", 0.9),
                         ("a door that only opens outward", 0.8),
                         ("the unnamed caller met a shut door", 0.7)]:
            subconscious.flag("clark", txt, sal)
        r1 = run("clark")
        check("with fragments -> dreamed", r1.status == "dreamed" and r1.ok)
        check("dream text produced", bool(r1.dream_text) and "corridor" in r1.dream_text)
        check("consolidated all 3 fragments", r1.fragments == 3)

        # 3. journal + latest + receipt all written
        with open(_journal_path("clark")) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        check("journal has the dream record", rows and rows[-1]["dream"] == r1.dream_text)
        with open(_latest_path("clark")) as fh:
            check("latest.txt holds the dream", r1.dream_text in fh.read())
        with open(_receipt_path("clark")) as fh:
            rcpt = json.load(fh)
        check("receipt status=dreamed with excerpt + tier",
              rcpt["status"] == "dreamed" and rcpt["excerpt"] and rcpt["tier"] == "free")

        # 4. 'since' anchoring: a second immediate run sees no NEW fragments
        r2 = run("clark")
        check("second run anchors on last dream -> dreamless", r2.status == "dreamless")

        # 5. charter source resolution (registry, by value, no agent names in code)
        check("clark source resolves to subconscious",
              _resolve_sources("clark") == ["subconscious"])
        check("treebeard source resolves to subconscious+telemetry",
              _resolve_sources("treebeard") == ["subconscious", "telemetry"])

        # 6. telemetry stub is registered and inert (treebeard dreams, doesn't crash)
        subconscious.flag("treebeard", "a tripped cap, a refused bypass", 0.95)
        rt = run("treebeard")
        check("treebeard dreams over subconscious; telemetry stub no-ops",
              rt.status == "dreamed" and rt.fragments == 1)

        # 7. consolidation failure surfaces as an error receipt, not a crash
        def boom(agent, prompt, tier):
            raise RuntimeError("backend down")
        _consolidator = boom
        subconscious.flag("clark", "something new to dream", 0.9)
        re_ = run("clark")
        check("router failure -> error receipt + ok=False",
              re_.status == "error" and not re_.ok)
        with open(_receipt_path("clark")) as fh:
            check("error receipt records the cause", json.load(fh)["status"] == "error")

        # 8. same-agent overlap skips (lock held) rather than re-dreaming
        subconscious.flag("clark", "a fragment to contend over", 0.9)
        held = _agent_lock("clark")
        held.__enter__()  # simulate an in-flight dream holding the lock
        try:
            rb = run("clark")
        finally:
            held.__exit__()
        check("overlapping same-agent dream skips (not error, not duplicate)",
              rb.status == "skipped" and rb.ok)
        with open(_receipt_path("clark")) as fh:
            check("skip writes an 'already running' receipt",
                  json.load(fh)["status"] == "skipped")

        print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
        return 0 if failures == 0 else 1
    finally:
        _DREAMS_DIR_OVERRIDE = _SOURCES_OVERRIDE = _consolidator = _now_override = None
        subconscious._PERSIST_DIR_OVERRIDE = subconscious._DECISION_LOG_OVERRIDE = None
        subconscious._SCOPES_OVERRIDE = subconscious._THRESHOLD_OVERRIDE = None
        subconscious._embedder = None
        subconscious._CLIENTS, subconscious._COLLECTIONS = {}, {}
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        raise SystemExit(selftest())
    if args:
        # Live run for a named agent, print the dream to stdout.
        result = run(args[0])
        print(f"[{result.agent}] status={result.status} fragments={result.fragments} "
              f"tier={result.tier} model={result.model}")
        if result.dream_text:
            print("\n" + result.dream_text + "\n")
        elif result.error:
            print(f"error: {result.error}")
        raise SystemExit(0 if result.ok else 1)
    print(__doc__)
    print("Usage: dream.py <agent>   (run the daily dream)  |  --selftest")
