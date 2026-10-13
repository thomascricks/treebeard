#!/usr/bin/env python3
# governance_audit.py - Treebeard governance audit for Moon
# This module provides a structural audit of an agent's memory against its
# consolidation decision log. It is designed to be called by Moon (or the
# operator) to verify that every consolidation decision resulted in a
# corresponding memory entry with correct provenance.

from __future__ import annotations
import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
import shutil
import logging
import types
from typing import Optional, List, Dict, Any
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

# Configure logging
logger = logging.getLogger("governance_audit")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(handler)

# Add the shared library path for imports
_LIB = f"{_TB_BASE}/shared/lib"
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import longterm
import subconscious

# Constants
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MEMORY_LIMIT = 10000


@dataclasses.dataclass
class AuditResult:
    """
    Result of a governance audit.

    Attributes:
        ok: True if the audit completed without infrastructure errors.
            This does NOT mean zero issues were found; it means the audit
            itself ran successfully. Issues are listed separately.
        target_agent: The agent whose memory was audited.
        issues: List of issues found, each with 'type' and 'description'.
        decision_count: Number of consolidation decisions checked.
        memory_count: Number of memory entries read.
        since: ISO timestamp lookback used for the audit.
        error: Error message if ok is False, else None.
    """
    ok: bool
    target_agent: str
    issues: List[Dict[str, Any]]
    decision_count: int
    memory_count: int
    since: Optional[str]
    error: Optional[str] = None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(ts: str) -> float:
    return dt.datetime.fromisoformat(ts).timestamp()


def _since_epoch(since: Optional[str]) -> Optional[float]:
    if since is None:
        return None
    return _parse_iso(since)


def _default_since() -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()


def _validate_since(since: Optional[str]) -> Optional[str]:
    if since is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(since)
    except ValueError as exc:
        raise ValueError(f"Invalid 'since' timestamp: {since}") from exc
    if parsed > dt.datetime.now(dt.timezone.utc):
        raise ValueError(f"'since' timestamp is in the future: {since}")
    return since


def audit(
    target_agent: str,
    since: Optional[str] = None,
    caller: str = "moon",
    limit: int = DEFAULT_MEMORY_LIMIT,
    _consolidation_log_path: Optional[str] = None,
    _longterm_module: Optional[types.ModuleType] = None
) -> AuditResult:
    target_agent = subconscious._validate_agent(target_agent)
    since = _validate_since(since)
    if since is None:
        since = _default_since()

    longterm_mod = _longterm_module or longterm
    log_path = _consolidation_log_path or longterm_mod.DECISION_LOG

    decisions = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Malformed line in decision log: %s", line[:80])
                    continue
                if entry.get("agent") != target_agent:
                    continue
                if entry.get("decision") != "consolidated":
                    continue
                ts_str = entry.get("ts")
                if ts_str:
                    try:
                        if _since_epoch(since) and _parse_iso(ts_str) <= _since_epoch(since):
                            continue
                    except Exception:
                        logger.warning("Unparseable timestamp in decision log: %s", ts_str)
                        continue
                decisions.append(entry)
    except FileNotFoundError:
        decisions = []
    except (OSError, PermissionError) as exc:
        result = AuditResult(
            ok=False,
            target_agent=target_agent,
            issues=[],
            decision_count=0,
            memory_count=0,
            since=since,
            error=f"Failed to read decision log: {exc}"
        )
        _log_decision(result, caller, _consolidation_log_path=log_path,
                      _longterm_module=longterm_mod)
        return result

    try:
        memories = _read_memories(target_agent, since=since, caller=caller, limit=limit,
                                  _longterm_module=longterm_mod)
    except PermissionError as exc:
        result = AuditResult(
            ok=False,
            target_agent=target_agent,
            issues=[],
            decision_count=len(decisions),
            memory_count=0,
            since=since,
            error=f"Permission denied: {exc}"
        )
        _log_decision(result, caller, _consolidation_log_path=log_path,
                      _longterm_module=longterm_mod)
        return result
    except Exception as exc:
        result = AuditResult(
            ok=False,
            target_agent=target_agent,
            issues=[],
            decision_count=len(decisions),
            memory_count=0,
            since=since,
            error=f"Failed to read memory: {exc}"
        )
        _log_decision(result, caller, _consolidation_log_path=log_path,
                      _longterm_module=longterm_mod)
        return result

    if len(memories) == limit:
        logger.warning("Memory read hit limit %d; audit may be incomplete. Increase limit if needed.", limit)

    memory_provenances = set()
    memory_entries_by_provenance = {}
    for mem in memories:
        prov = getattr(mem, "provenance", None)
        if prov:
            memory_provenances.add(prov)
            if prov not in memory_entries_by_provenance:
                memory_entries_by_provenance[prov] = []
            memory_entries_by_provenance[prov].append(mem)

    issues = []
    decision_provenances = set()

    for dec in decisions:
        prov = dec.get("provenance")
        if prov:
            decision_provenances.add(prov)
        if prov and prov not in memory_provenances:
            issues.append({
                "type": "missing_memory",
                "description": f"Decision with provenance '{prov}' has no corresponding memory entry",
                "decision": dec
            })
        if not prov:
            issues.append({
                "type": "missing_provenance",
                "description": "Decision missing 'provenance' field",
                "decision": dec
            })
        if not dec.get("memory_id"):
            issues.append({
                "type": "missing_memory_id",
                "description": "Decision missing 'memory_id' field",
                "decision": dec
            })

    for prov, mem_list in memory_entries_by_provenance.items():
        if prov not in decision_provenances:
            for mem in mem_list:
                if getattr(mem, "source_kind", None) == "dream":
                    issues.append({
                        "type": "orphan_memory",
                        "description": (
                            f"Dream memory with provenance '{prov}' has "
                            "no matching consolidation decision"
                        ),
                        "memory": {
                            "text": getattr(mem, "text", ""),
                            "consolidated_at": getattr(mem, "consolidated_at", None),
                            "provenance": getattr(mem, "provenance", None)
                        }
                    })

    result = AuditResult(
        ok=True,
        target_agent=target_agent,
        issues=issues,
        decision_count=len(decisions),
        memory_count=len(memories),
        since=since,
        error=None
    )

    _log_decision(result, caller, _consolidation_log_path=log_path,
                  _longterm_module=longterm_mod)

    try:
        _write_audit_note(target_agent, result,
                          _longterm_module=longterm_mod,
                          source_kind="audit")
    except Exception as exc:
        logger.warning("Could not write audit note to Moon's memory: %s", exc)

    return result


def _read_memories(
    target: str,
    since: Optional[str],
    caller: str = "moon",
    limit: int = DEFAULT_MEMORY_LIMIT,
    _longterm_module: Optional[types.ModuleType] = None
) -> List[Any]:
    mod = _longterm_module or longterm
    return mod.read_memories(target, caller=caller, since=since, limit=limit)


def _log_decision(
    result: AuditResult,
    caller: str,
    _consolidation_log_path: Optional[str] = None,
    _longterm_module: Optional[types.ModuleType] = None
) -> None:
    mod = _longterm_module or longterm
    log_path = _consolidation_log_path or mod.DECISION_LOG
    record = {
        "ts": _now(),
        "agent": result.target_agent,
        "decision": "audit",
        "caller": caller,
        "issues_found": len(result.issues),
        "decision_count": result.decision_count,
        "memory_count": result.memory_count,
        "since": result.since,
        "ok": result.ok,
        "error": result.error
    }
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except (OSError, PermissionError) as exc:
        logger.error("Decision-log write failed: %s", exc)


def _write_audit_note(
    target: str,
    result: AuditResult,
    _longterm_module: Optional[types.ModuleType] = None,
    source_kind: str = "audit"
) -> None:
    mod = _longterm_module or longterm
    note_text = (
        f"Governance audit of {target} at {_now()}: "
        f"{len(result.issues)} issues found."
    )
    if result.issues:
        note_text += " Details: " + "; ".join(
            i["description"] for i in result.issues[:5]
        )
    try:
        mod.consolidate(
            "moon",
            note_text,
            provenance=f"governance-audit-{target}",
            source_kind=source_kind
        )
    except Exception as exc:
        logger.warning("Failed to consolidate audit note: %s", exc)
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run governance audit for an agent")
    parser.add_argument("target", help="Agent name to audit (e.g., treebeard)")
    parser.add_argument("--since", help="ISO timestamp lookback (default: 7 days)")
    parser.add_argument("--caller", default="moon", help="Caller identity (must have 'all' scope)")
    parser.add_argument("--limit", type=int, default=DEFAULT_MEMORY_LIMIT,
                        help="Max memories to read")
    parser.add_argument("--selftest", action="store_true", help="Run self-test and exit")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    if args.limit <= 0:
        parser.error("--limit must be greater than 0")

    try:
        result = audit(args.target, since=args.since, caller=args.caller, limit=args.limit)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Audit result for {args.target}:")
    print(f"  ok: {result.ok}")
    print(f"  decisions checked: {result.decision_count}")
    print(f"  memories found: {result.memory_count}")
    print(f"  issues: {len(result.issues)}")
    if result.issues:
        print("Issues:")
        for i, issue in enumerate(result.issues, 1):
            print(f"  {i}. {issue['type']}: {issue['description']}")
    if result.error:
        print(f"  error: {result.error}")
    sys.exit(0 if result.ok else 1)


def _selftest() -> bool:
    print("governance_audit.py selftest")
    checks = []
    def ok(label, cond):
        checks.append(cond)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    tmp = tempfile.mkdtemp(prefix="governance_audit_selftest_")
    try:
        store_dir = os.path.join(tmp, "longterm_store")
        os.makedirs(store_dir, exist_ok=True)
        log_path = os.path.join(tmp, "consolidation_decisions.log")

        import longterm as real_longterm
        real_longterm.STORE_DIR = store_dir
        real_longterm.DECISION_LOG = log_path
        real_longterm._client = None
        real_longterm._embed = lambda texts: [[1.0]] * len(texts)
        mod = real_longterm

        mod.consolidate("test_agent", "memory1", provenance="dream:2026-W26", source_kind="dream")
        mod.consolidate("test_agent", "memory2", provenance="dream:2026-W27", source_kind="dream")
        mod.write_memory("test_agent", "orphan_manual", provenance="manual:2026-W26", source_kind="manual")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(),
                "agent": "test_agent",
                "decision": "consolidated",
                "memory_id": "fake_id2",
                "source_kind": "dream",
                "char_len": 10,
                "error": None
            }) + "\n")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(),
                "agent": "test_agent",
                "decision": "consolidated",
                "provenance": "dream:2026-W29",
                "source_kind": "dream",
                "char_len": 10,
                "error": None
            }) + "\n")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(),
                "agent": "test_agent",
                "decision": "consolidated",
                "provenance": "dream:2026-W28",
                "memory_id": "fake_id",
                "source_kind": "dream",
                "char_len": 10,
                "error": None
            }) + "\n")

        result = audit("test_agent", caller="moon",
                       _longterm_module=mod,
                       _consolidation_log_path=log_path)

        ok("audit runs and ok", result.ok)
        issue_types = [i["type"] for i in result.issues]
        ok("missing_memory found", "missing_memory" in issue_types)
        ok("orphan_memory not found for manual entry", "orphan_memory" not in issue_types)
        ok("missing_provenance flagged", "missing_provenance" in issue_types)
        ok("missing_memory_id flagged", "missing_memory_id" in issue_types)
        ok("decision_count matches expected", result.decision_count >= 4)

        moon_memories = mod.read_memories("moon", caller="moon")
        ok("audit note written to Moon's memory",
           any("Governance audit" in m.text for m in moon_memories))

        try:
            audit("invalid@agent")
            ok("invalid agent raises", False)
        except ValueError:
            ok("invalid agent raises", True)

        try:
            _validate_since("not-a-date")
            ok("malformed since raises", False)
        except ValueError:
            ok("malformed since raises", True)

        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        try:
            _validate_since(future)
            ok("future since raises", False)
        except ValueError:
            ok("future since raises", True)

    except Exception as e:
        print(f"Selftest exception: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for c in checks if c)
    total = len(checks)
    print(f"{'ALL PASS' if passed == total else 'FAIL'} ({passed}/{total})")
    return passed == total


if __name__ == "__main__":
    main()
