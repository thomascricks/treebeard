"""personality.py - per-agent personality state (schema/storage only).

STEP 6 SCOPE: load, resolve, write, version. Personality has ZERO effect on
model selection until model profiles exist (Step 7) and a meaningful affinity
can be computed. This module deliberately exposes no scoring function.

Two files per agent, permission-separated so "Treebeard may not modify bounds"
is enforced by the FILESYSTEM, not by code that could be reasoned around:

    <agent>.bounds.toml    root:treebeard 0640  owner-controlled, TB read-only
    <agent>.adaptive.toml  root:treebeard 0660  TB-writable

Resolution: adaptive value (clamped to bounds) > bounds default > inert.

Absent bounds => personality is INERT, not an error. An agent with no
personality routes exactly as it does today. Absent adaptive => defaults.
Malformed bounds => inert (never invent constraints). Malformed adaptive =>
fall back to defaults (never fail the call).

Values are clamped ON READ, not only on write: bounds may be tightened after
an adaptive value was written, and a stale out-of-range value must not take
effect merely because it was legal when stored.
"""
import os
import sys
import tomllib
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DIR = f"{_TB_ETC}/personality"

DIMENSIONS = (
    "depth", "discrimination", "social_model", "stress_stability",
    "rational_constraint", "concept_persistence", "memory_weight",
    "frame_flexibility", "narrative_drive", "initiative",
    "limit_sensitivity", "novelty",
)

BOUND_KEYS = ("min", "max", "default", "adaptation_rate", "locked")


def _paths(agent, base=DIR):
    return (os.path.join(base, f"{agent}.bounds.toml"),
            os.path.join(base, f"{agent}.adaptive.toml"))


def _read_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh), None
    except FileNotFoundError:
        return None, "absent"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def load_bounds(agent, base=DIR):
    """Owner-defined bounds, or None when absent/malformed (personality inert).

    A malformed bounds file must NOT be partially parsed: inventing a
    constraint is worse than having none, because a wrong bound would silently
    authorise or forbid adaptation the owner never specified.
    """
    path, _ = _paths(agent, base)
    data, err = _read_toml(path)
    if data is None:
        if err != "absent":
            sys.stderr.write(f"[personality] bounds malformed for {agent!r} "
                             f"({err}); personality INERT\n")
        return None
    out = {}
    try:
        for dim, spec in (data.get("bounds") or {}).items():
            if dim not in DIMENSIONS:
                continue
            lo, hi = float(spec["min"]), float(spec["max"])
            dflt = float(spec["default"])
            if not (lo <= dflt <= hi):
                raise ValueError(f"{dim}: default {dflt} outside [{lo},{hi}]")
            out[dim] = {"min": lo, "max": hi, "default": dflt,
                        "adaptation_rate": float(spec.get("adaptation_rate", 0.05)),
                        "locked": bool(spec.get("locked", False))}
    except Exception as e:
        sys.stderr.write(f"[personality] bounds invalid for {agent!r} ({e}); "
                         f"personality INERT\n")
        return None
    return out or None


def load_adaptive(agent, base=DIR):
    """Treebeard-written state. Returns (values, revision). Never raises."""
    _, path = _paths(agent, base)
    data, err = _read_toml(path)
    if data is None:
        if err != "absent":
            sys.stderr.write(f"[personality] adaptive malformed for {agent!r} "
                             f"({err}); using bounds defaults\n")
        return {}, 0
    try:
        rev = int(data.get("revision", 0))
        vals = {}
        for dim, spec in (data.get("adaptive") or {}).items():
            if dim in DIMENSIONS and "value" in spec:
                vals[dim] = {"value": float(spec["value"]),
                             "confidence": float(spec.get("confidence", 0.0)),
                             "updated": spec.get("updated"),
                             "evidence": spec.get("evidence")}
        return vals, rev
    except Exception as e:
        sys.stderr.write(f"[personality] adaptive unreadable for {agent!r} "
                         f"({e}); using bounds defaults\n")
        return {}, 0


def resolve(agent, base=DIR):
    """(vector, revision_label). vector is {} when personality is inert."""
    bounds = load_bounds(agent, base)
    if not bounds:
        return {}, f"{agent}:inert"
    vals, rev = load_adaptive(agent, base)
    vec = {}
    for dim, b in bounds.items():
        v = b["default"]
        if dim in vals:
            raw = vals[dim]["value"]
            v = min(max(raw, b["min"]), b["max"])
            if abs(v - raw) > 1e-12:
                sys.stderr.write(f"[personality] {agent}.{dim} adaptive {raw} "
                                 f"outside [{b['min']},{b['max']}]; clamped to {v}\n")
        vec[dim] = v
    return vec, f"{agent}:r{rev}" if vals else f"{agent}:default"


def _render(agent, values, revision):
    """Deterministic TOML for the fixed adaptive shape. No writer dependency:
    the structure is twelve dimensions of scalars, so a hand-rendered file is
    safer than adding a package that cannot be installed offline."""
    lines = [f'# Treebeard-writable adaptive personality for {agent}.',
             '# Owner bounds live in the sibling .bounds.toml and are NOT',
             '# writable through this path. Machine-generated; edit with care.',
             f'agent = "{agent}"', f'revision = {int(revision)}', '']
    for dim in DIMENSIONS:
        if dim not in values:
            continue
        s = values[dim]
        lines.append(f"[adaptive.{dim}]")
        lines.append(f"value = {float(s['value'])!r}")
        lines.append(f"confidence = {float(s.get('confidence', 0.0))!r}")
        lines.append(f'updated = "{s.get("updated", "")}"')
        lines.append(f'evidence = "{s.get("evidence", "")}"')
        lines.append("")
    return "\n".join(lines)


def write_adaptive(agent, values, revision, base=DIR):
    """Atomic versioned write. Returns (ok, path_or_error).

    Writes ONLY the adaptive file. There is deliberately no bounds writer in
    this module: the adaptive path cannot reach owner bounds even by mistake.
    """
    _, path = _paths(agent, base)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(_render(agent, values, revision))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)           # world-readable: the router resolves personality
                                       # as treebeard, a DIFFERENT uid from the
                                       # tbpersona writer. Contains no secrets.
        os.replace(tmp, path)          # atomic
        return True, path
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"{type(e).__name__}: {e}"
