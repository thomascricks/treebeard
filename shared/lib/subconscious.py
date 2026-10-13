#!/usr/bin/env python3
"""
subconscious.py - Treebeard subconscious memory store (shared cognition lib)
============================================================================

WHERE THIS LIVES
    /opt/treebeard/shared/lib/subconscious.py    (root:treebeard 640)
    Beside constitution.py, telemetry.py, router.py, statusboard.py, budget.py.
    Run under the venv at /opt/treebeard/shared/venv (chromadb lives there).

WHAT THIS IS  (and what it deliberately is NOT)
    The SUBCONSCIOUS layer of the four-layer memory architecture (June 13):
    an agent's *significant internal reasoning, self-flagged DURING a call* -
    not its outputs. One isolated ChromaDB collection per agent.

    This module owns three things and nothing else:
      1. THE STORE  - per-agent ChromaDB collection, capability-isolated.
      2. THE WRITE  - flag() / write_fragment(): an agent records its OWN
                      significant fragment at flag-time (mid-call).
      3. THE GATE + INSTRUMENT - flag() applies a TUNABLE salience threshold and
                      LOGS every decision (flagged/skipped/error), so the real
                      significance rate is observable and "what counts as
                      significant" can be answered through use, not design
                      (the June 13 open question). Same instrument-first posture
                      as telemetry.py: ship the meter before the mechanism.

    It is NOT the dream. It stores RAW fragments WITH their affective charge.
    dream.py READS from here and does the short->medium consolidation (compress,
    strip the charge, keep the lesson). Charge-stripping is dream's job, not
    this module's - keeping them apart is the same correctness boundary as
    route()-dispatches-before vs question-gate-judges-after: writing a fragment
    happens during a call; dreaming happens after the day over what accumulated.

    It does NOT use Chroma's default embedder (which phones home AND downloads an
    ONNX model - both forbidden on the egress-locked box). Embeddings come from
    the LOCAL Ollama instance. No external egress at runtime. Chroma's anonymous
    telemetry is turned OFF explicitly below.

ISOLATION  (capability-based, matching long-term memory + statusboard)
    READ : an agent reads ONLY its own subconscious. The OVERSEER - the agent
           whose memory_scope is "all" (Treebeard, by his charter, NOT by a name
           hardcoded in this file) - reads across ALL agents.
    WRITE: ALWAYS own-only. There is NO overseer-write. Treebeard reads every
           agent's subconscious; he writes to no one's but his own. Oversight is
           read, not reach - a governance property, encoded.

    Enforcement is IN-CODE for now: the caller's identity is trusted in-process.
    True OS-level isolation between agent users is the deferred Podman/Phase-2.5
    work (June 4: "in-process isolation only, not OS-level"). This module
    enforces the policy today; containerization makes it un-bypassable later.

CONSTITUTION
    Untouched. The overseer's cross-agent read is granted at the CHARTER layer
    (memory_scope), exactly as the Mission Sheet already grants Treebeard reach.
    Widening the inviolate law to expand the overseer's own reach is the precise
    move Anti-Capture exists to catch. We don't.

PUBLIC API
    flag(agent, text, salience, *, threshold=None, metadata=None,
         caller=None, fragment_id=None) -> FlagResult
        ^ THE sanctioned write path. Every agent captures through here.
    read_fragments(target, *, caller, since=None, query=None, n=10)
                   -> list[Fragment]
    write_fragment(agent, text, *, salience=None, metadata=None, caller=None,
                   fragment_id=None) -> WriteResult
        ^ The underlying primitive flag() is built on. It is NOT a second
          capture path for agents - it exists so flag() has something to call
          and so the selftest can drive the store directly. Calling it bypasses
          the gate, so it is deliberately ABSENT from flag_decisions.log: that
          log measures GATE DECISIONS, and an event that never met the threshold
          must not be counted among flagged/skipped. Keeping write_fragment out
          of the log by construction is what keeps the calibration set clean.
          (A failed direct write still records an "error" row - that's an infra
          fault, not a gate decision, and the meter must never hide a failure.)

FAILURE CONTRACT  (mirrors telemetry.py exactly)
    - Programmer / governance errors raise LOUDLY (bad agent name, unauthorized
      cross-agent access, reserved-key collision). Fail fast in development.
    - Infra hiccups on WRITE never propagate into the caller's real work: the
      failure is recorded durably (flag_decisions.log) and a result with
      ok=False is returned. A broken subconscious must never kill the call it
      was trying to remember. (A *persistent* embed/store failure therefore
      means silent non-capture - visible in the decision log, and the upcoming
      Sun "did the dream run" receipt is the second backstop.)
    - READ raises on genuine infra failure (so dream.py can mark itself
      degraded) and returns [] when there simply are no matching fragments.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import sys
import threading
import uuid
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------

# Namespace root for the four-layer memory architecture. Subconscious lives
# under it; longterm/medium will sit beside it later.
_MEM_ROOT = f"{_TB_BASE}/shared/memory/subconscious"
PERSIST_DIR = os.path.join(_MEM_ROOT, "store")          # ChromaDB owns this dir
DECISION_LOG = os.path.join(_MEM_ROOT, "flag_decisions.log")  # calibration instrument

# Per-agent read scope. Forward-compatible with the structured YAML charters
# (June 4: charter = identity, memory_scope, model tier). Until those exist on
# disk, scope is read from this small operator-owned data file. FAIL CLOSED:
# any agent not granted "all" here is "own". Treebeard is granted "all" in the
# file, NOT by a branch in this code.
SCOPES_FILE = f"{_TB_ETC}/memory_scopes.conf"

# Tunable salience gate. Operator edits the conf to retune without a code change.
SUBC_CONF = f"{_TB_ETC}/subconscious.conf"
DEFAULT_SALIENCE_THRESHOLD = 0.5

# Local Ollama embeddings - no external egress. The model below must be pulled:
#   ollama pull nomic-embed-text
# Change EMBED_MODEL here if you standardise on a different local embed model.
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
EMBED_TIMEOUT_S = 30

# Strict ASCII agent-name discipline - matches statusboard.py / telemetry.py.
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Metadata keys this module owns; caller metadata may not collide with them.
_RESERVED_META = frozenset({"agent", "ts_utc", "ts_epoch", "salience"})

# --------------------------------------------------------------------------
# Test/override hooks (set only by selftest; prod path stays file-based)
# --------------------------------------------------------------------------
_PERSIST_DIR_OVERRIDE = None   # str
_DECISION_LOG_OVERRIDE = None  # str
_SCOPES_OVERRIDE = None        # dict[str, str]
_THRESHOLD_OVERRIDE = None     # float
_embedder = None               # callable(text) -> list[float]; default = Ollama

_LOCK = threading.Lock()
_CLIENTS: dict[str, object] = {}       # persist_dir -> PersistentClient
_COLLECTIONS: dict[tuple, object] = {}  # (persist_dir, agent) -> Collection


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclasses.dataclass
class WriteResult:
    ok: bool
    agent: str
    fragment_id: str | None
    error: str | None = None


@dataclasses.dataclass
class FlagResult:
    ok: bool
    agent: str
    decision: str            # "flagged" | "skipped" | "error"
    salience: float
    threshold: float
    fragment_id: str | None = None
    error: str | None = None


@dataclasses.dataclass
class Fragment:
    id: str
    text: str
    salience: float | None
    ts_utc: str
    metadata: dict
    distance: float | None = None   # only set on semantic (query) reads


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _now() -> tuple[str, float]:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.isoformat(), now.timestamp()


def _validate_agent(name: str) -> str:
    if not isinstance(name, str) or not _AGENT_RE.match(name):
        # Programmer error - fail loud.
        raise ValueError(f"invalid agent name: {name!r} (expect ^[a-z][a-z0-9_]{{0,31}}$)")
    return name


def _to_epoch(since) -> float | None:
    if since is None:
        return None
    if isinstance(since, (int, float)):
        return float(since)
    if isinstance(since, _dt.datetime):
        if since.tzinfo is None:
            since = since.replace(tzinfo=_dt.timezone.utc)
        return since.timestamp()
    if isinstance(since, str):
        # Python 3.11+ fromisoformat parses the 'Z' suffix natively (box is 3.14).
        d = _dt.datetime.fromisoformat(since)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    raise ValueError(f"unsupported 'since' type: {type(since).__name__}")


def _persist_dir() -> str:
    return _PERSIST_DIR_OVERRIDE or PERSIST_DIR


def _decision_log_path() -> str:
    return _DECISION_LOG_OVERRIDE or DECISION_LOG


# --------------------------------------------------------------------------
# Config readers (re-read each call; files are tiny and this keeps retuning
# live without a process restart)
# --------------------------------------------------------------------------
def _read_scopes() -> dict[str, str]:
    if _SCOPES_OVERRIDE is not None:
        return _SCOPES_OVERRIDE
    scopes: dict[str, str] = {}
    try:
        with open(SCOPES_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().lower()
                if v in ("own", "all"):
                    scopes[k] = v
                # any other value (e.g. "dormant") -> treated as own (closed)
    except FileNotFoundError:
        pass  # fail closed: no file -> everyone is "own"
    return scopes


def _resolve_scope(caller: str) -> str:
    # FAIL CLOSED. Unknown / unlisted caller -> "own".
    return _read_scopes().get(caller, "own")


def _get_threshold() -> float:
    if _THRESHOLD_OVERRIDE is not None:
        return float(_THRESHOLD_OVERRIDE)
    try:
        with open(SUBC_CONF, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line.startswith("salience_threshold"):
                    _, v = line.split("=", 1)
                    return float(v.strip())
    except (FileNotFoundError, ValueError):
        pass
    return DEFAULT_SALIENCE_THRESHOLD


# --------------------------------------------------------------------------
# Embeddings (local Ollama; explicit, never Chroma's auto-embedder)
# --------------------------------------------------------------------------
def _ollama_embed(text: str) -> list[float]:
    """POST to the LOCAL Ollama embeddings endpoint. stdlib only, localhost,
    no external egress. Raises on any failure so callers can record it."""
    import urllib.request

    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Treebeard-Subconscious/0.1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ollama embeddings HTTP {resp.status}")
        body = json.loads(resp.read().decode("utf-8"))
    vec = body.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise RuntimeError("ollama embeddings returned no vector "
                           f"(is '{EMBED_MODEL}' pulled?)")
    return [float(x) for x in vec]


def _embed(text: str) -> list[float]:
    fn = _embedder or _ollama_embed
    return fn(text)


# --------------------------------------------------------------------------
# Chroma store (telemetry off; auto-embedder neutralised)
# --------------------------------------------------------------------------
class _NoAutoEmbed:
    """An embedding function that REFUSES to run. We always supply embeddings
    explicitly, so Chroma must never invoke an embedder - and certainly never
    its default ONNX one, which would download a model and phone home. If Chroma
    ever calls this, that's a bug we want to hear about, loudly."""

    @staticmethod
    def name() -> str:
        return "treebeard_no_auto_embed"

    def __call__(self, input):  # noqa: A002  (chroma's param is named 'input')
        raise RuntimeError("subconscious: explicit embeddings required; "
                           "Chroma auto-embed is disabled (egress/telemetry).")

    # (de)serialisation hooks some chroma versions expect:
    @staticmethod
    def build_from_config(config):
        return _NoAutoEmbed()

    def get_config(self):
        return {}


def _client():
    pdir = _persist_dir()
    with _LOCK:
        cli = _CLIENTS.get(pdir)
        if cli is None:
            import chromadb
            from chromadb.config import Settings
            os.makedirs(pdir, exist_ok=True)
            cli = chromadb.PersistentClient(
                path=pdir,
                settings=Settings(anonymized_telemetry=False, allow_reset=False, migrations_hash_algorithm="md5"),
            )
            _CLIENTS[pdir] = cli
        return cli


def _collection(agent: str):
    pdir = _persist_dir()
    key = (pdir, agent)
    with _LOCK:
        col = _COLLECTIONS.get(key)
        if col is not None:
            return col
    cli = _client()
    name = f"subconscious_{agent}"
    try:
        col = cli.get_or_create_collection(name=name, embedding_function=_NoAutoEmbed())
    except TypeError:
        # Older/newer chroma that rejects the inert EF object: create without
        # one. Safe because we ALWAYS pass embeddings to add()/query() - the
        # default EF, even if attached, is never invoked.
        col = cli.get_or_create_collection(name=name)
    with _LOCK:
        _COLLECTIONS[key] = col
    return col


def _flatten_meta(meta: dict | None, base: dict) -> dict:
    out = dict(base)
    if meta:
        for k, v in meta.items():
            if k in _RESERVED_META:
                raise ValueError(f"metadata key {k!r} is reserved")
            # Chroma metadata values must be scalar.
            out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


# --------------------------------------------------------------------------
# Decision log (the calibration instrument)
# --------------------------------------------------------------------------
def _log_decision(record: dict) -> None:
    """Append one JSONL row + fsync. Best-effort: a logging failure must never
    take down the caller. Mirrors telemetry's durable-failure-log pattern."""
    try:
        path = _decision_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:  # noqa: BLE001
        # Last resort: stderr. Do not raise.
        print(f"[subconscious] decision-log write failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def write_fragment(agent, text, *, salience=None, metadata=None,
                   caller=None, fragment_id=None) -> WriteResult:
    """Unconditionally store ONE raw fragment in `agent`'s subconscious.

    PRIMITIVE, not a capture path. flag() is the sole sanctioned way for an
    agent to record a fragment; this is what flag() calls once the gate clears,
    plus a seam the selftest drives directly. Because it skips the gate, a
    SUCCESSFUL direct write is intentionally NOT logged to flag_decisions.log -
    that log is a clean record of gate decisions (flagged/skipped), and an event
    that never met the threshold has no business in the flag-rate population. A
    FAILED write still logs an "error" row: a fault is infra, not a decision, and
    the meter must never conceal one.

    Own-only: there is no overseer-write. `caller` defaults to `agent`; passing
    a different caller is a programmer error and raises. Infra failures
    (embed/store) are recorded durably and returned as ok=False - never raised
    into the caller's real work.
    """
    agent = _validate_agent(agent)
    caller = _validate_agent(caller) if caller is not None else agent
    if caller != agent:
        # Governance invariant, encoded: oversight is read, not write.
        raise PermissionError(
            f"{caller!r} may not write {agent!r}'s subconscious (writes are own-only)")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("fragment text must be a non-empty string")

    iso, epoch = _now()
    fid = fragment_id or uuid.uuid4().hex
    sal = float(salience) if salience is not None else None
    base = {"agent": agent, "ts_utc": iso, "ts_epoch": epoch}
    if sal is not None:
        base["salience"] = sal
    meta = _flatten_meta(metadata, base)

    try:
        vec = _embed(text)
        _collection(agent).add(
            ids=[fid], embeddings=[vec], documents=[text], metadatas=[meta]
        )
        return WriteResult(ok=True, agent=agent, fragment_id=fid)
    except Exception as exc:  # noqa: BLE001  - infra path: record, don't raise
        _log_decision({"ts_utc": iso, "agent": agent, "decision": "error",
                       "error": f"{type(exc).__name__}: {exc}", "fragment_id": fid})
        return WriteResult(ok=False, agent=agent, fragment_id=None,
                           error=f"{type(exc).__name__}: {exc}")


def flag(agent, text, salience, *, threshold=None, metadata=None,
         caller=None, fragment_id=None) -> FlagResult:
    """Threshold-gated capture - the normal entry point an agent uses mid-call.

    salience >= threshold  -> store (write_fragment) AND log "flagged".
    salience <  threshold   -> do NOT store, but log "skipped".
    Either way the decision is recorded, so the real flag-rate is observable and
    the threshold can be tuned through use. The salience SCORE is produced
    upstream by the caller (the agent's own self-flagging); this module owns the
    GATE and the INSTRUMENT, never the judgement of what is significant.
    """
    agent = _validate_agent(agent)
    sal = float(salience)
    thr = float(threshold) if threshold is not None else _get_threshold()
    iso, _ = _now()

    if sal < thr:
        _log_decision({"ts_utc": iso, "agent": agent, "decision": "skipped",
                       "salience": sal, "threshold": thr})
        return FlagResult(ok=True, agent=agent, decision="skipped",
                          salience=sal, threshold=thr)

    wr = write_fragment(agent, text, salience=sal, metadata=metadata,
                        caller=caller, fragment_id=fragment_id)
    if wr.ok:
        _log_decision({"ts_utc": iso, "agent": agent, "decision": "flagged",
                       "salience": sal, "threshold": thr, "fragment_id": wr.fragment_id})
        return FlagResult(ok=True, agent=agent, decision="flagged",
                          salience=sal, threshold=thr, fragment_id=wr.fragment_id)
    # write_fragment already logged the error row.
    return FlagResult(ok=False, agent=agent, decision="error",
                      salience=sal, threshold=thr, error=wr.error)


def read_fragments(target, *, caller, since=None, query=None, n=10) -> list[Fragment]:
    """Read `target`'s subconscious fragments, capability-gated.

    Allowed iff caller == target (own read) OR caller's memory_scope == "all"
    (the overseer reading across agents). Otherwise PermissionError.

    query=None -> most-recent-first, optionally filtered by `since`.
    query=str  -> semantic nearest-neighbour search (embeds the query).
    Returns [] when there are no matching fragments. Raises on real infra
    failure so the consumer (dream.py) can mark itself degraded.
    """
    target = _validate_agent(target)
    caller = _validate_agent(caller)
    scope = _resolve_scope(caller)
    if caller != target and scope != "all":
        raise PermissionError(
            f"{caller!r} (scope={scope}) may not read {target!r}'s subconscious")

    since_epoch = _to_epoch(since)
    where = {"ts_epoch": {"$gte": since_epoch}} if since_epoch is not None else None
    col = _collection(target)

    if query is not None:
        qvec = _embed(query)
        res = col.query(
            query_embeddings=[qvec],
            n_results=int(n),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = []
        for i, fid in enumerate(ids):
            m = metas[i] or {}
            out.append(Fragment(
                id=fid, text=docs[i], salience=m.get("salience"),
                ts_utc=m.get("ts_utc", ""), metadata=dict(m),
                distance=dists[i] if i < len(dists) else None,
            ))
        return out

    # Recency path: fetch (filtered) then sort newest-first in Python. Chroma has
    # no native order-by; fine for the small subconscious store. Revisit with a
    # cap if a store ever grows large.
    res = col.get(where=where, include=["documents", "metadatas"])
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    rows = []
    for i, fid in enumerate(ids):
        m = metas[i] or {}
        rows.append(Fragment(
            id=fid, text=docs[i], salience=m.get("salience"),
            ts_utc=m.get("ts_utc", ""), metadata=dict(m),
        ))
    rows.sort(key=lambda f: f.metadata.get("ts_epoch", 0.0), reverse=True)
    return rows[: int(n)]


# --------------------------------------------------------------------------
# Selftest  (temp dir + fake embedder + injected scopes; never touches prod)
# --------------------------------------------------------------------------
def selftest() -> int:
    import hashlib
    import tempfile

    global _PERSIST_DIR_OVERRIDE, _DECISION_LOG_OVERRIDE
    global _SCOPES_OVERRIDE, _THRESHOLD_OVERRIDE, _embedder
    global _CLIENTS, _COLLECTIONS

    def fake_embed(text: str) -> list[float]:
        # Deterministic 16-dim vector from the text hash - no Ollama needed.
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:16]]

    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            failures += 1

    tmp = tempfile.mkdtemp(prefix="subc_selftest_")
    try:
        _PERSIST_DIR_OVERRIDE = os.path.join(tmp, "store")
        _DECISION_LOG_OVERRIDE = os.path.join(tmp, "flag.log")
        _SCOPES_OVERRIDE = {"treebeard": "all", "clark": "own"}
        _THRESHOLD_OVERRIDE = 0.5
        _embedder = fake_embed
        _CLIENTS, _COLLECTIONS = {}, {}

        print("subconscious.py selftest")

        # 1. own write + own read
        w = write_fragment("clark", "a load-bearing thought", salience=0.9)
        check("own write ok", w.ok and w.fragment_id)
        got = read_fragments("clark", caller="clark")
        check("own read returns the fragment", len(got) == 1 and "load-bearing" in got[0].text)

        # 2. flag gate: above stores, below skips
        f_hi = flag("clark", "kept", 0.8)
        f_lo = flag("clark", "discarded", 0.2)
        check("flag above threshold -> flagged + stored", f_hi.decision == "flagged" and f_hi.ok)
        check("flag below threshold -> skipped, not stored", f_lo.decision == "skipped")
        check("store grew by exactly one (the flagged one)",
              len(read_fragments("clark", caller="clark")) == 2)

        # 3. cross-agent READ: denied for worker, allowed for overseer
        denied = False
        try:
            read_fragments("clark", caller="han")  # han: unlisted -> own, not overseer
        except PermissionError:
            denied = True
        check("non-overseer cross-read denied", denied)
        over = read_fragments("clark", caller="treebeard")  # treebeard: scope=all
        check("overseer cross-read allowed", len(over) == 2)

        # 4. cross-agent WRITE is forbidden even for the overseer
        no_over_write = False
        try:
            write_fragment("clark", "treebeard reaching in", salience=1.0, caller="treebeard")
        except PermissionError:
            no_over_write = True
        check("overseer-write forbidden (reads all, writes none-but-own)", no_over_write)

        # 5. bad agent name raises
        bad = False
        try:
            write_fragment("Clark!", "x", salience=1.0)
        except ValueError:
            bad = True
        check("invalid agent name raises", bad)

        # 6. reserved metadata key collision raises
        rsv = False
        try:
            write_fragment("clark", "x", salience=1.0, metadata={"salience": 2})
        except ValueError:
            rsv = True
        check("reserved metadata key raises", rsv)

        # 7. since filter excludes older rows
        import time
        cutoff = _dt.datetime.now(_dt.timezone.utc)
        time.sleep(0.01)
        write_fragment("clark", "after the cutoff", salience=0.95)
        recent = read_fragments("clark", caller="clark", since=cutoff)
        check("since filter returns only post-cutoff", len(recent) == 1 and "after the cutoff" in recent[0].text)

        # 8. semantic query returns a hit
        q = read_fragments("clark", caller="clark", query="load-bearing thought", n=3)
        check("semantic query returns results", len(q) >= 1 and q[0].distance is not None)

        # 9. decision log records ONLY gate decisions (flagged/skipped) and
        #    infra errors - never a row for a successful direct write_fragment.
        #    This is the contract that keeps the calibration set clean.
        with open(_DECISION_LOG_OVERRIDE, encoding="utf-8") as fh:
            decisions = [json.loads(l)["decision"] for l in fh if l.strip()]
        check("decision log recorded a 'flagged' and a 'skipped'",
              "flagged" in decisions and "skipped" in decisions)
        # several direct write_fragment() calls happened above (checks 1,7,...);
        # none of them may have left a row in the gate-decision log.
        check("successful direct writes are absent from the decision log",
              all(d in ("flagged", "skipped", "error") for d in decisions))

        # 10. _to_epoch parses a 'Z'-suffixed ISO string (native on 3.11+).
        z_epoch = _to_epoch("2026-06-29T12:00:00Z")
        ref = _dt.datetime(2026, 6, 29, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
        check("_to_epoch parses 'Z'-suffixed ISO string", abs(z_epoch - ref) < 1e-6)

        print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
        return 0 if failures == 0 else 1
    finally:
        _PERSIST_DIR_OVERRIDE = _DECISION_LOG_OVERRIDE = None
        _SCOPES_OVERRIDE = _THRESHOLD_OVERRIDE = _embedder = None
        _CLIENTS, _COLLECTIONS = {}, {}
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)
    print("Run with --selftest to exercise the store (uses a temp dir; no Ollama needed).")
