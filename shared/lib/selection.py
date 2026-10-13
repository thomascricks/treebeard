"""selection.py - capability-band candidate selection for the router.

Replaces the CLASS_TO_TIER scalar map, which had three values and could
therefore only ever name three destinations -- leaving cheap, advanced_a and advanced_b
reachable only by force_tier or token-floor accident.

CAPABILITY IS A FLOOR, NOT A REWARD. Band membership eliminates; it never
scores. A tier above the task's required band gets no advantage for the
excess. This is the structural guard against re-deriving frontier bias.

Pure functions over injected state: no I/O, no router import, no agent names.
"""
import configparser
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

CONF = f"{_TB_ETC}/routing.conf"


class PolicyError(Exception):
    """Malformed or missing routing policy. Fail loudly; do not guess."""


def load_policy(path=CONF):
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    if not cp.read(path):
        raise PolicyError(f"routing policy unreadable/absent: {path}")
    try:
        order = [b.strip() for b in cp["bands"]["order"].split(",") if b.strip()]
        tier_band = {k: v.strip() for k, v in cp["tier_band"].items()}
        class_floor = {k: v.strip() for k, v in cp["class_floor"].items()}
        weights = {k: float(v) for k, v in cp["weights"].items()}
        sel = {k: float(v) for k, v in cp["selection"].items()}
    except (KeyError, ValueError) as e:
        raise PolicyError(f"malformed routing policy: {e}") from e
    for t, b in tier_band.items():
        if b not in order:
            raise PolicyError(f"tier '{t}' has unknown band '{b}'")
    for c, b in class_floor.items():
        if b not in order:
            raise PolicyError(f"class '{c}' has unknown floor band '{b}'")
    return {"order": order, "tier_band": tier_band, "class_floor": class_floor,
            "weights": weights, "selection": sel}


def eligible(task_class, tiers, policy):
    """Tiers meeting the class's capability FLOOR. Returns (kept, eliminated).

    A tier with no band assignment is not organically routable (still
    reachable via force_tier). An unknown class raises: silently defaulting
    a class floor is how tasks end up on the wrong models.
    """
    order, tb, cf = policy["order"], policy["tier_band"], policy["class_floor"]
    if task_class not in cf:
        raise PolicyError(f"no class_floor for task class '{task_class}'")
    floor = order.index(cf[task_class])
    kept, gone = [], {}
    for t in tiers:
        if t not in tb:
            gone[t] = "no-band-assigned"
        elif order.index(tb[t]) < floor:
            gone[t] = f"below-floor({tb[t]}<{cf[task_class]})"
        else:
            kept.append(t)
    return kept, gone


def normalize_cost(costs, reference):
    """Cost -> [0,1] against a STABLE ABSOLUTE reference, clamped.

        norm = 1.0 - min(cost / reference, 1.0)

    Free -> 1.0; cost at or above the reference -> 0.0. Clamping stops one
    unusually expensive call from dominating the score without limit.

    Replaces within-set relative normalization, which rescaled whatever
    spread happened to be present: among complex candidates a 20% price gap
    ($0.0300 vs $0.0375) became a 100% score gap, putting Advanced_b alone in
    the tie band on every complex task. That is the frontier monopoly
    inverted, not removed. An absolute scale keeps real price differences
    proportional so the near-tie band means what it says.

    `reference` is a ROUTING-POLICY SCALE, not an empirical average.
    """
    if not costs:
        return {}
    if reference <= 0:
        raise PolicyError(f"cost_reference_usd must be > 0, got {reference}")
    return {k: 1.0 - min(max(v, 0.0) / reference, 1.0) for k, v in costs.items()}


def normalize_rate(rates, reference):
    """Provider RATE (USD per Mtok, in/out blended) -> [0,1], clamped.

        norm = 1.0 - min(blended_rate / reference, 1.0)

    Rate, not per-call spend. Absolute call cost scales with prompt length,
    so two providers 30% apart on price sat 0.0075 apart at a 10k prompt and
    0.013 apart at 17k -- moving in and out of the near-tie band purely
    because the task got longer. A provider's rate is a stable property of
    the provider; total spend is a property of the task. Absolute dollar
    estimates remain in use for the budget hard-stop, telemetry, Moon
    observations and reporting -- this function feeds SELECTION only.
    """
    if reference <= 0:
        raise PolicyError(f"rate_reference_mtok must be > 0, got {reference}")
    return {k: 1.0 - min(max(v, 0.0) / reference, 1.0) for k, v in rates.items()}


def base_score(candidates, rates, health, policy, perf=None):
    """Weighted base score over RATES and derived PERFORMANCE. NO capability
    term by design: capability eliminates, it never scores.

    `rates` maps tier -> blended USD/Mtok. Pass provider rates, not call costs.
    `perf`  maps tier -> derived performance in [0,1] (perf.derive). When None
    or missing a tier, a shared neutral 0.75 is used: a constant common to all
    candidates shifts every score equally and therefore cannot change ranking
    or tie-band membership -- absence of evidence differentiates nothing.
    """
    w = policy["weights"]
    ref = policy["selection"].get("rate_reference_mtok", 20.0)
    nc = normalize_rate({k: rates.get(k, 0.0) for k in candidates}, ref)
    pf = perf or {}
    return {t: w.get("cost", 0.0) * nc.get(t, 0.0)
               + w.get("health", 0.0) * health.get(t, 1.0)
               + w.get("perf", 0.0) * pf.get(t, 0.75)
            for t in candidates}


def tie_band(scores, epsilon):
    """Candidates within epsilon of the best score, best-first then by name
    for determinism."""
    if not scores:
        return []
    best = max(scores.values())
    band = [t for t, s in scores.items() if best - s <= epsilon]
    return sorted(band, key=lambda t: (-scores[t], t))

RR_STATE = f"{_TB_BASE}/shared/telemetry/.rr_state"


def round_robin(band, state_path=RR_STATE):
    """Rotate across an already-eligible near-tie band. Returns (choice, ok).

    INTERIM tie-breaker. Personality affinity supersedes this as the principal
    breaker; round-robin then remains only the final fallback when affinity is
    also tied.

    Operates strictly INSIDE the band handed to it -- capability, context,
    health, budget and operator gates have already eliminated everything else,
    so rotation can never reach an ineligible tier.

    State is PERSISTED because each agent call is a fresh process: `ask.py` is
    spawned per agent by will.py, so an in-memory counter would reset every
    call and every process would pick band[0] -- exactly the monopoly this
    exists to prevent. Keyed by band signature so different eligible sets
    rotate independently.

    Any state failure returns (band[0], False): a broken counter degrades to
    deterministic choice, it never fails the call.
    """
    if not band:
        return None, False
    if len(band) == 1:
        return band[0], False
    key = "+".join(sorted(band))
    try:
        import json, os
        state = {}
        if os.path.exists(state_path) and os.path.getsize(state_path) > 0:
            try:
                with open(state_path) as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    state = loaded
            except (ValueError, OSError):
                state = {}   # empty/corrupt: self-heal, do not disable rotation
        idx = int(state.get(key, 0)) % len(band)
        state[key] = (idx + 1) % len(band)
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, state_path)          # atomic
        return band[idx], True
    except Exception as e:
        import sys as _s
        _s.stderr.write(f"[selection] round-robin state unavailable ({e}); "
                        f"using {band[0]}\n")
        return band[0], False


def under_sampled(band, agent_vector, profiles, threshold, affinity_mod):
    """Candidates whose model-profile EVIDENCE is thin, with the reason.

    Uncertainty is measured against the profile itself -- total affinity
    weight -- not against routing counts. Routing counts measure popularity;
    what exploration exists to improve is profile evidence, so that is what it
    targets. An absent profile is maximally uncertain; an asserted-only profile
    carries its author's confidence but no observations; a well-measured
    profile does not qualify.
    """
    out = {}
    for t in band:
        prof = profiles.get(t)
        if not prof:
            out[t] = "no-profile"
            continue
        _s, det = affinity_mod.score(agent_vector, t, profiles)
        w = det.get("total_weight", 0.0) or 0.0
        if w < threshold:
            bases = sorted({d.get("basis") for d in det.get("dims", {}).values()})
            out[t] = f"weight={w:.3f}<{threshold} basis={','.join(b for b in bases if b)}"
    return out


def weighted_pick(decision_id, weights, sharpness=3.0, floor=0.001):
    """Deterministic weighted draw over comparable candidates.

    `weights` maps candidate -> affinity in [0,1]. The draw weight is the
    candidate's advantage over NEUTRAL (0.5), raised to `sharpness`: a strong
    personality preference dominates the distribution without ever taking it
    to 100%, while a qualified candidate the personality is lukewarm about
    keeps a small floored share. This is the concentration control inside the
    tie band -- a small asserted-profile affinity edge earns a LARGER SHARE,
    never ALL the traffic (winner-take-all is reserved for evidence: a real
    perf gap splits the band upstream and never reaches this draw).

    Deterministic given decision_id (same replayability contract as
    explore_pick): the audit carries the seed, weights and draw so any
    selection can be reconstructed from telemetry alone.
    Returns (choice, audit); (None, audit) only on empty input.
    """
    import random
    if not weights:
        return None, {"error": "no-candidates"}
    w = {t: max(float(a) - 0.5, 0.0) ** sharpness + floor
         for t, a in weights.items()}
    total = sum(w.values())
    order = sorted(w)                      # deterministic iteration order
    rng = random.Random(str(decision_id))
    draw = rng.random() * total
    audit = {"seed": str(decision_id), "sharpness": sharpness, "floor": floor,
             "weights": {t: round(w[t] / total, 4) for t in order},
             "draw": round(draw / total, 6)}
    acc = 0.0
    for t in order:
        acc += w[t]
        if draw <= acc:
            audit["picked"] = t
            return t, audit
    audit["picked"] = order[-1]
    return order[-1], audit


def weighted_pick(decision_id, weights, sharpness=3.0, floor=0.001):
    """Deterministic weighted draw over comparable candidates.

    `weights` maps candidate -> affinity in [0,1]. The draw weight is the
    candidate's advantage over NEUTRAL (0.5), raised to `sharpness`: a strong
    personality preference dominates the distribution without ever taking it
    to 100%, while a qualified candidate the personality is lukewarm about
    keeps a small floored share. This is the concentration control inside the
    tie band -- a small asserted-profile affinity edge earns a LARGER SHARE,
    never ALL the traffic (winner-take-all is reserved for evidence: a real
    perf gap splits the band upstream and never reaches this draw).

    Deterministic given decision_id (same replayability contract as
    explore_pick): the audit carries the seed, weights and draw so any
    selection can be reconstructed from telemetry alone.
    Returns (choice, audit); (None, audit) only on empty input.
    """
    import random
    if not weights:
        return None, {"error": "no-candidates"}
    w = {t: max(float(a) - 0.5, 0.0) ** sharpness + floor
         for t, a in weights.items()}
    total = sum(w.values())
    order = sorted(w)                      # deterministic iteration order
    rng = random.Random(str(decision_id))
    draw = rng.random() * total
    audit = {"seed": str(decision_id), "sharpness": sharpness, "floor": floor,
             "weights": {t: round(w[t] / total, 4) for t in order},
             "draw": round(draw / total, 6)}
    acc = 0.0
    for t in order:
        acc += w[t]
        if draw <= acc:
            audit["picked"] = t
            return t, audit
    audit["picked"] = order[-1]
    return order[-1], audit


def explore_pick(decision_id, candidates, rate, rate_min, rate_max):
    """(choice_or_None, audit). Deterministic given decision_id: the draw is
    seeded from it, so any selection can be replayed from telemetry alone.

    `rate` is clamped into the owner-defined [rate_min, rate_max] band, so
    exploration cannot be tuned to zero from outside that file.
    """
    import random
    eff = min(max(rate, rate_min), rate_max)
    rng = random.Random(str(decision_id))
    draw = rng.random()
    audit = {"rate_requested": rate, "rate_effective": eff,
             "rate_min": rate_min, "rate_max": rate_max,
             "seed": str(decision_id), "draw": round(draw, 6),
             "fired": draw < eff, "candidates": sorted(candidates)}
    if not candidates or draw >= eff:
        return None, audit
    pick = rng.choice(sorted(candidates))
    audit["picked"] = pick
    return pick, audit
