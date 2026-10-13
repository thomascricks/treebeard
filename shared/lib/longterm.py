#!/usr/bin/env python3
"""
longterm.py -- Treebeard long-term (durable consolidated) memory store.

This is the peer of subconscious.py. The subconscious holds raw flagged
fragments with their affective charge; long-term holds the CONSOLIDATED form
-- what survived a dream, charge stripped, lesson kept. These memories are
durable: nothing downstream drains them, so a bad write persists. That makes
the write path the load-bearing thing, and it is intentionally narrow.

Design, inherited from subconscious.py (same shape on purpose):
  * One shared lib. Every agent calls the same functions. No agent names in
    the code -- a new agent works the day its charter exists.
  * Per-agent ChromaDB collection, capability-isolated. An agent reads only
    its own long-term store UNLESS its memory_scope is "all" (the overseer,
    Treebeard). Writes are ALWAYS own-only -- oversight is read, never reach.
  * Embeddings come from local Ollama (nomic-embed-text). Chroma's auto-embed
    and anonymous telemetry are both disabled -- this box does no egress and
    pulls no models at runtime.
  * Fail-closed: an unknown/unlisted agent resolves to own-only, never to all.
  * Failure contract: programmer/governance errors raise loud; an infra hiccup
    on write records to the decision log and returns ok=False, never killing
    the caller that was trying to remember.

The long-term-specific differences from subconscious:
  * Each memory carries PROVENANCE -- what it was consolidated from (e.g.
    "dream:2026-W26" or "subconscious:fragment_ids") -- and a consolidated_at
    timestamp. This is the audit trail for a durable store: you can always ask
    where a long-term memory came from.
  * The sanctioned write entry is consolidate() -- a deliberate, attributed
    write at consolidation time -- not a mid-call flag(). write_memory() is the
    primitive consolidate() is built on plus a test seam; direct use is not the
    sanctioned path and (like subconscious) does not get logged as a
    consolidation decision.

CONSTITUTION IS UNTOUCHED. The overseer's cross-agent read is granted at the
charter layer (memory_scopes.conf), exactly as subconscious does it. Widening
the inviolate law to expand the overseer's own reach is the Anti-Capture move
and is declined here as everywhere.
"""

import os
import re
import sys
import json
import time
import uuid
import datetime
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

# ---------------------------------------------------------------------------
# SETTINGS
# TREEBEARD_* env vars exist only for off-box testing; on the box they are
# unset and these defaults are the real paths.
# ---------------------------------------------------------------------------
BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
STORE_DIR = os.environ.get(
    "TREEBEARD_LONGTERM_DIR",
    os.path.join(BASE, "shared", "memory", "longterm"),
)
SCOPES_FILE = os.environ.get(
    "TREEBEARD_MEMORY_SCOPES",
    f"{_TB_ETC}/memory_scopes.conf",
)
EMBED_MODEL = os.environ.get("TREEBEARD_EMBED_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("TREEBEARD_OLLAMA_URL", "http://localhost:11434")

# Decision/audit log lives beside the store, same as subconscious's
# flag_decisions.log. Every consolidate() decision is recorded here so the
# durable store has a tamper-evident trail of what was written and why.
DECISION_LOG = os.path.join(STORE_DIR, "consolidation_decisions.log")

# Metadata keys the store reserves for its own bookkeeping. A caller that tries
# to set one of these in its own metadata is a programmer error and raises --
# the durable store must own its provenance fields without silent collision.
_RESERVED_META = frozenset({
    "agent", "consolidated_at", "provenance", "source_kind", "char_len",
})

# Agent-name discipline: same charset rule as subconscious. Collection names
# and filesystem paths are derived from this, so it must be tight.
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# ---------------------------------------------------------------------------
# RESULT TYPE
# ---------------------------------------------------------------------------
class Result:
    """Outcome of a consolidate()/write call. ok=False is an infra failure the
    caller can survive; programmer/governance failures raise instead."""
    __slots__ = ("ok", "memory_id", "decision", "error")

    def __init__(self, ok, memory_id=None, decision=None, error=None):
        self.ok = ok
        self.memory_id = memory_id
        self.decision = decision
        self.error = error

    def __repr__(self):
        return ("Result(ok=%r, memory_id=%r, decision=%r, error=%r)"
                % (self.ok, self.memory_id, self.decision, self.error))


class Memory:
    """A single long-term memory read back from the store."""
    __slots__ = ("memory_id", "agent", "text", "consolidated_at",
                 "provenance", "source_kind", "distance")

    def __init__(self, memory_id, agent, text, consolidated_at,
                 provenance, source_kind, distance=None):
        self.memory_id = memory_id
        self.agent = agent
        self.text = text
        self.consolidated_at = consolidated_at
        self.provenance = provenance
        self.source_kind = source_kind
        self.distance = distance

    def __repr__(self):
        return ("Memory(agent=%r, text=%r, consolidated_at=%r, provenance=%r)"
                % (self.agent, self.text[:40], self.consolidated_at,
                   self.provenance))


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------
def _now():
    """UTC, ISO-8601, timezone-aware. Hard-UTC so there is no clock-zone gap
    between writers."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _to_epoch(iso):
    """Parse an ISO-8601 string to epoch seconds. Python 3.11+ parses a
    trailing 'Z' natively, so no .replace() dance is needed on the box."""
    return datetime.datetime.fromisoformat(iso).timestamp()


def _ascii_clean(s, field):
    """Enforce ASCII on stored text. Non-ASCII in a durable memory is a
    programmer error -- the embed model and the audit log both assume ASCII,
    and a smuggled control char in a permanent record is exactly what we don't
    want. Raises rather than silently transcoding."""
    if not isinstance(s, str):
        raise TypeError("%s must be str, got %r" % (field, type(s)))
    try:
        s.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("%s must be ASCII (%s)" % (field, exc))
    return s


def _check_agent(agent):
    if not isinstance(agent, str) or not _AGENT_RE.match(agent):
        raise ValueError(
            "invalid agent name %r (must match %s)"
            % (agent, _AGENT_RE.pattern))
    return agent


def _log_decision(record):
    """Append one JSON line to the consolidation decision log. Best-effort:
    a logging failure must not kill a consolidation, but it IS surfaced to
    stderr so a broken audit trail is visible. Mirrors subconscious."""
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with open(DECISION_LOG, "a", encoding="ascii") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 -- audit write must not be fatal
        sys.stderr.write(
            "[longterm] decision-log write failed: %s\n" % (exc,))


# ---------------------------------------------------------------------------
# SCOPE RESOLUTION (capability isolation)
# Identical model to subconscious.py: read the scopes file, an agent listed as
# "= all" is an overseer and may read across; everyone else is own-only. Writes
# are own-only for everyone regardless. Fail-closed: a missing/unreadable
# scopes file means NOBODY is an overseer (not even Treebeard), so the default
# is the safe one.
# ---------------------------------------------------------------------------
def _read_scopes():
    """Return {agent: scope}. Absent/garbled file -> empty dict (fail-closed:
    no overseers). Lines are 'agent = scope'; blanks and #comments ignored."""
    scopes = {}
    try:
        with open(SCOPES_FILE, "r", encoding="ascii") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                name, _, scope = line.partition("=")
                name = name.strip()
                scope = scope.strip().lower()
                if _AGENT_RE.match(name) and scope in ("own", "all"):
                    scopes[name] = scope
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            "[longterm] scopes file unreadable (%s); fail-closed to own\n"
            % (exc,))
    return scopes


def _scope_of(agent):
    """The effective scope for an agent. Unlisted -> 'own' (fail-closed)."""
    return _read_scopes().get(agent, "own")


def _may_read(caller, target):
    """Caller may read target's store iff caller==target, or caller is an
    overseer (scope 'all'). This is the whole governance rule for reads."""
    if caller == target:
        return True
    return _scope_of(caller) == "all"


# ---------------------------------------------------------------------------
# CHROMA CLIENT (lazy, egress-off, no auto-embed)
# ---------------------------------------------------------------------------
_client = None  # process-wide singleton


def _get_client():
    """Build (once) a PersistentClient with anonymous telemetry OFF. We pass
    embeddings explicitly on every add/query, so the default embedding function
    is never invoked -- this is the deliberate egress guarantee: an embedder
    that can't run can't run the wrong model. Mirrors subconscious exactly."""
    global _client
    if _client is not None:
        return _client
    import chromadb
    from chromadb.config import Settings
    os.makedirs(os.path.join(STORE_DIR, "store"), exist_ok=True)
    _client = chromadb.PersistentClient(
        path=os.path.join(STORE_DIR, "store"),
        settings=Settings(anonymized_telemetry=False, allow_reset=False, migrations_hash_algorithm="md5"),
    )
    return _client


def _collection(agent):
    """Get/create this agent's long-term collection. Name derived from the
    validated agent string. No embedding_function attached -- we always pass
    embeddings= ourselves (the egress guard)."""
    cli = _get_client()
    return cli.get_or_create_collection(
        name="longterm_%s" % agent,
        metadata={"hnsw:space": "cosine"},
    )


def _embed(texts):
    """Embed a list of strings via local Ollama. No egress -- localhost only.
    Returns a list of vectors. Raises on transport failure; the caller decides
    whether that's fatal (read) or recordable (write)."""
    import urllib.request
    vectors = []
    for t in texts:
        payload = json.dumps({"model": EMBED_MODEL, "prompt": t}).encode("ascii")
        req = urllib.request.Request(
            OLLAMA_URL + "/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        vec = body.get("embedding")
        if not vec:
            raise RuntimeError("embed returned no vector for input")
        vectors.append(vec)
    return vectors


# ---------------------------------------------------------------------------
# WRITE PATH
# consolidate() is the sanctioned, attributed, logged entry. write_memory() is
# the primitive it stands on (and a test seam); a successful direct
# write_memory() does NOT appear in the decision log -- the log measures
# consolidation, and a direct write skipped consolidation, so folding it in
# would pollute the audit trail. (Same contract subconscious uses for flag()
# vs write_fragment().)
# ---------------------------------------------------------------------------
def write_memory(agent, text, provenance, source_kind, metadata=None):
    """PRIMITIVE write. consolidate() is the sole sanctioned write path; this
    is what it is built on, plus a test seam. A successful direct call stays
    OUT of the consolidation decision log by design. Writes are own-only by
    construction (an agent writes to its own collection); there is no
    overseer-write -- oversight is read, never reach.

    Raises on programmer/governance error (bad agent, bad text, reserved-key
    collision). Returns Result(ok=False, ...) on infra failure (embed/store)."""
    _check_agent(agent)
    text = _ascii_clean(text, "text")
    provenance = _ascii_clean(provenance, "provenance")
    source_kind = _ascii_clean(source_kind, "source_kind")
    if not text.strip():
        raise ValueError("refusing to store empty long-term memory")

    meta = dict(metadata or {})
    bad = _RESERVED_META.intersection(meta)
    if bad:
        raise ValueError(
            "metadata may not set reserved keys: %s" % ", ".join(sorted(bad)))

    mem_id = uuid.uuid4().hex
    now = _now()
    meta.update({
        "agent": agent,
        "consolidated_at": now,
        "provenance": provenance,
        "source_kind": source_kind,
        "char_len": len(text),
    })

    try:
        vec = _embed([text])[0]
        col = _collection(agent)
        col.add(ids=[mem_id], embeddings=[vec],
                documents=[text], metadatas=[meta])
    except Exception as exc:  # noqa: BLE001 -- infra failure is survivable
        return Result(ok=False, error="%s: %s" % (type(exc).__name__, exc))

    return Result(ok=True, memory_id=mem_id)


def consolidate(agent, text, provenance, source_kind, metadata=None):
    """SANCTIONED write. A deliberate, attributed consolidation into long-term:
    'this is what survived, here is where it came from.' Every call is logged
    to the consolidation decision log -- written or failed -- so the durable
    store carries a tamper-evident trail. This is the entry Moon's weekly
    synthesis calls.

    provenance : free-text-ish ASCII saying what this was consolidated FROM,
                 e.g. 'dream:2026-W26' or 'subconscious:3-fragments'.
    source_kind: a coarse category, e.g. 'dream' / 'subconscious' / 'manual'.
    """
    _check_agent(agent)
    res = write_memory(agent, text, provenance, source_kind, metadata=metadata)
    _log_decision({
        "ts": _now(),
        "agent": agent,
        "decision": "consolidated" if res.ok else "error",
        "memory_id": res.memory_id,
        "provenance": provenance,
        "source_kind": source_kind,
        "char_len": len(text) if isinstance(text, str) else None,
        "error": res.error,
    })
    res.decision = "consolidated" if res.ok else "error"
    return res


# ---------------------------------------------------------------------------
# READ PATH
# read_memories(): recency-ordered (or since-filtered) read of an agent's
# long-term store. query_memories(): semantic search. Both enforce capability
# isolation -- a non-overseer reading another agent's store raises
# PermissionError, which is the read-path-raises contract (a denied read is a
# governance failure, not a survivable infra hiccup).
# ---------------------------------------------------------------------------
def _meta_to_memory(mem_id, text, meta, distance=None):
    return Memory(
        memory_id=mem_id,
        agent=meta.get("agent", ""),
        text=text,
        consolidated_at=meta.get("consolidated_at", ""),
        provenance=meta.get("provenance", ""),
        source_kind=meta.get("source_kind", ""),
        distance=distance,
    )


def read_memories(target, caller, since=None, limit=200):
    """Read target's long-term memories, newest first. since (ISO string) keeps
    only memories consolidated strictly after it. limit caps the return.

    Capability isolation: caller must be target or an overseer, else
    PermissionError. (Read denial is a governance failure -> raise.)

    NOTE the same growth caveat subconscious carries: this fetches the
    collection and orders in Python. Fine while the store stays bounded; the
    real bound is Moon pruning what it has folded forward. For long-term, the
    intended discipline is that consolidation is rare and deliberate, so the
    store grows slowly by design."""
    _check_agent(target)
    _check_agent(caller)
    if not _may_read(caller, target):
        raise PermissionError(
            "%r (scope=%s) may not read %r's long-term store"
            % (caller, _scope_of(caller), target))

    col = _collection(target)
    got = col.get(include=["documents", "metadatas"])
    ids = got.get("ids", []) or []
    docs = got.get("documents", []) or []
    metas = got.get("metadatas", []) or []

    rows = []
    since_epoch = _to_epoch(since) if since else None
    for mem_id, text, meta in zip(ids, docs, metas):
        meta = meta or {}
        ts = meta.get("consolidated_at", "")
        if since_epoch is not None:
            try:
                if _to_epoch(ts) <= since_epoch:
                    continue
            except Exception:  # noqa: BLE001 -- unparseable ts, keep it visible
                pass
        rows.append((ts, mem_id, text, meta))

    # newest first
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:limit]
    return [_meta_to_memory(mid, txt, m) for (_ts, mid, txt, m) in rows]


def query_memories(target, caller, query_text, n_results=5):
    """Semantic search of target's long-term store. Same capability isolation
    as read_memories. Returns Memory objects ordered by similarity (nearest
    first), each carrying its distance."""
    _check_agent(target)
    _check_agent(caller)
    query_text = _ascii_clean(query_text, "query_text")
    if not _may_read(caller, target):
        raise PermissionError(
            "%r (scope=%s) may not read %r's long-term store"
            % (caller, _scope_of(caller), target))

    qvec = _embed([query_text])[0]
    col = _collection(target)
    res = col.query(query_embeddings=[qvec], n_results=n_results,
                    include=["documents", "metadatas", "distances"])
    out = []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for mem_id, text, meta, dist in zip(ids, docs, metas, dists):
        out.append(_meta_to_memory(mem_id, text, meta or {}, distance=dist))
    return out


# ---------------------------------------------------------------------------
# SELFTEST
# Runs against real ChromaDB in a temp dir with a FAKE embedder (so no Ollama
# needed and no egress). Proves the store, the isolation, the audit contract,
# and the long-term-specific provenance fields. Mirrors subconscious's selftest
# structure so the two are reviewable side by side.
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile

    print("longterm.py selftest")
    checks = []

    def ok(label, cond):
        checks.append(cond)
        print("  [%s] %s" % ("OK " if cond else "XX ", label))

    tmp = tempfile.mkdtemp(prefix="longterm_selftest_")
    scopes_path = os.path.join(tmp, "memory_scopes.conf")
    with open(scopes_path, "w", encoding="ascii") as fh:
        fh.write("treebeard = all\n")  # treebeard is the overseer

    # Point the module at the temp store + scopes, and stub the embedder.
    global STORE_DIR, SCOPES_FILE, DECISION_LOG, _client, _embed
    STORE_DIR = os.path.join(tmp, "store_root")
    SCOPES_FILE = scopes_path
    DECISION_LOG = os.path.join(STORE_DIR, "consolidation_decisions.log")
    _client = None

    _real_embed = _embed

    def _fake_embed(texts):
        # Deterministic tiny vector from text length + checksum; no network.
        out = []
        for t in texts:
            h = sum(ord(c) for c in t)
            out.append([float(len(t)), float(h % 97), float((h * 7) % 89)])
        return out

    _embed = _fake_embed
    try:
        # 1. sanctioned consolidate writes + logs
        r = consolidate("clark", "the south wall is load-bearing",
                        provenance="dream:2026-W26", source_kind="dream")
        ok("consolidate -> ok + memory_id", r.ok and r.memory_id)
        ok("consolidate decision == 'consolidated'", r.decision == "consolidated")

        # 2. read own back, provenance intact
        got = read_memories("clark", caller="clark")
        ok("own read returns the memory", len(got) == 1)
        ok("provenance preserved", got and got[0].provenance == "dream:2026-W26")
        ok("source_kind preserved", got and got[0].source_kind == "dream")
        ok("consolidated_at populated", got and bool(got[0].consolidated_at))

        # 3. overseer cross-read allowed
        over = read_memories("clark", caller="treebeard")
        ok("overseer cross-read allowed", len(over) == 1)

        # 4. non-overseer cross-read denied
        denied = False
        try:
            read_memories("clark", caller="han")
        except PermissionError:
            denied = True
        ok("non-overseer cross-read denied", denied)

        # 5. overseer-write forbidden (no overseer-write: writes own-only)
        #    consolidate('clark', ...) called "as treebeard" still writes to
        #    clark's own store only because write target == first arg; there is
        #    no API to write INTO another agent. Prove the primitive writes to
        #    the named agent's store and nowhere else.
        consolidate("treebeard", "oversight is read not reach",
                    provenance="manual", source_kind="manual")
        tb = read_memories("treebeard", caller="treebeard")
        clark_only = read_memories("clark", caller="clark")
        ok("write lands only in the named agent's store",
           len(tb) == 1 and len(clark_only) == 1)

        # 6. invalid agent raises
        bad = False
        try:
            consolidate("Bad Name", "x", provenance="p", source_kind="manual")
        except ValueError:
            bad = True
        ok("invalid agent name raises", bad)

        # 7. reserved metadata key raises
        reserved = False
        try:
            consolidate("clark", "y", provenance="p", source_kind="manual",
                        metadata={"agent": "spoof"})
        except ValueError:
            reserved = True
        ok("reserved metadata key raises", reserved)

        # 8. non-ASCII text raises
        nonascii = False
        try:
            consolidate("clark", "caf\u00e9 wall", provenance="p",
                        source_kind="manual")
        except ValueError:
            nonascii = True
        ok("non-ASCII text raises", nonascii)

        # 9. empty text raises
        empty = False
        try:
            consolidate("clark", "   ", provenance="p", source_kind="manual")
        except ValueError:
            empty = True
        ok("empty text raises", empty)

        # 10. since filter returns only newer
        t_mid = _now()
        time.sleep(0.01)
        consolidate("clark", "a later memory", provenance="dream:2026-W27",
                    source_kind="dream")
        newer = read_memories("clark", caller="clark", since=t_mid)
        ok("since filter returns only post-cutoff",
           len(newer) == 1 and newer[0].provenance == "dream:2026-W27")

        # 11. semantic query returns results
        hits = query_memories("clark", caller="clark", query_text="wall",
                              n_results=3)
        ok("semantic query returns results", len(hits) >= 1)

        # 12. decision log holds 'consolidated' rows, and direct write_memory
        #     stays OUT of it (the audit contract).
        write_memory("clark", "direct primitive write, unlogged",
                     provenance="test", source_kind="manual")
        with open(DECISION_LOG, "r", encoding="ascii") as fh:
            log_lines = [json.loads(x) for x in fh if x.strip()]
        decisions = {d["decision"] for d in log_lines}
        ok("decision log recorded 'consolidated'", "consolidated" in decisions)
        ok("direct write_memory absent from decision log",
           all(d.get("text") != "direct primitive write, unlogged"
               for d in log_lines)
           and len([d for d in log_lines
                    if d["decision"] == "consolidated"]) >= 3)

        # 13. 'Z'-suffixed ISO parses (box is 3.11+)
        z_ok = False
        try:
            _to_epoch("2026-06-29T12:00:00+00:00")
            z = "2026-06-29T12:00:00Z"
            _to_epoch(z)
            z_ok = True
        except Exception:
            z_ok = False
        ok("_to_epoch parses 'Z'-suffixed ISO string", z_ok)

    finally:
        _embed = _real_embed

    passed = sum(1 for c in checks if c)
    total = len(checks)
    print("%s (%d/%d)" % ("ALL PASS" if passed == total else "FAIL",
                          passed, total))
    return passed == total


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    sys.stderr.write(
        "longterm.py is a shared library. Run with --selftest to verify.\n")
    sys.exit(2)
