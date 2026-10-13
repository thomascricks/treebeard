"""affinity.py - agent-personality x model-profile affinity.

STEP 7 SCOPE: computed and telemetered, NOT yet used for selection.

Deliberately NOT one global cosine similarity. Cosine over the whole vector
assumes every dimension wants similarity, which is false: some dimensions are
COMPLEMENTARY -- a high-narrative_drive agent may need a low-narrative_drive
model as ballast. The mode is declared per dimension in the profile and the
maths is explicit here so it can be inspected and argued with.

Per dimension, affinity in [0,1], 0.5 neutral:

    match       a = 1 - |agent - model|
    complement  a = 1 - |(1 - agent) - model|

complement is match against the MIRRORED target, not the inverse of match.
At agent 0.5 both modes want model 0.5, which is the correct degenerate case.

Weight:  w = confidence * sample_n / (sample_n + K)

sample_n = 0 yields zero weight regardless of stated confidence: an operator
assertion is not an observation, and one observation must not weigh as much
as a hundred.

Aggregate:  0.5 + sum(w * (a - 0.5)) / max(sum(w), 1.0)

The floored denominator keeps thinly-evidenced profiles near neutral instead
of letting a single weak dimension swing the result.
"""
import sys
import tomllib
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

PROFILES = f"{_TB_ETC}/model_profiles.toml"
K = 5.0
NEUTRAL = 0.5
VALID_MODES = ("match", "complement")
VALID_BASIS = ("asserted", "measured")


def load_profiles(path=PROFILES):
    """(profiles_by_tier, revision). ({}, None) when absent or malformed."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}, None
    except Exception as e:
        sys.stderr.write(f"[affinity] model profiles malformed ({e}); "
                         f"affinity NEUTRAL for all models\n")
        return {}, None
    rev = data.get("revision")
    out = {}
    for tier, dims in (data.get("tier") or {}).items():
        clean = {}
        for dim, spec in (dims or {}).items():
            try:
                mode = str(spec["mode"])
                if mode not in VALID_MODES:
                    raise ValueError(f"unknown mode {mode!r}")
                basis = str(spec["basis"])          # required: no default
                if basis not in VALID_BASIS:
                    raise ValueError(f"unknown basis {basis!r}")
                v = float(spec["value"])
                c = float(spec["confidence"])
                n = float(spec.get("sample_n", 0))
                if not (0.0 <= v <= 1.0 and 0.0 <= c <= 1.0 and n >= 0):
                    raise ValueError("value/confidence out of range")
                if basis == "measured" and n <= 0:
                    raise ValueError("measured basis requires sample_n > 0")
                clean[dim] = {"value": v, "confidence": c, "sample_n": n,
                              "mode": mode, "basis": basis}
            except Exception as e:
                # Drop the dimension. NEVER default it: a fabricated value is
                # worse than a missing one, because it silently votes.
                sys.stderr.write(f"[affinity] {tier}.{dim} invalid ({e}); "
                                 f"dimension dropped\n")
        if clean:
            out[tier] = clean
    return out, rev


def dimension_affinity(agent_value, model_value, mode):
    """Explicit per-dimension maths. See module docstring."""
    if mode == "complement":
        target = 1.0 - agent_value
    elif mode == "match":
        target = agent_value
    else:
        raise ValueError(f"unknown affinity mode: {mode}")
    return 1.0 - abs(target - model_value)


def weight(confidence, sample_n, basis, k=K):
    """Provenance-aware weight.

    asserted : w = confidence. A deliberate operator bootstrap value carries
               the weight its author claims -- it does not need a fabricated
               sample count to be usable, and inventing one would launder an
               assertion into fake evidence.
    measured : w = confidence * n/(n+k). Real observations still discount
               small samples, so one data point never weighs as much as many.

    An unrecognised basis never reaches here: load_profiles drops the
    dimension, because a value with unknown provenance must not vote.
    """
    if basis == "asserted":
        return confidence
    if basis == "measured":
        return confidence * (sample_n / (sample_n + k)) if sample_n > 0 else 0.0
    return 0.0


def score(agent_vector, tier, profiles):
    """(affinity, detail). Neutral 0.5 when the profile is absent or carries
    no usable weight; detail names why so it can be telemetered."""
    prof = profiles.get(tier)
    if not prof:
        return NEUTRAL, {"status": "profile-unavailable", "dims": {}}
    if not agent_vector:
        return NEUTRAL, {"status": "personality-inert", "dims": {}}
    num = tot = 0.0
    dims = {}
    for dim, spec in prof.items():
        if dim not in agent_vector:
            continue
        a = dimension_affinity(agent_vector[dim], spec["value"], spec["mode"])
        w = weight(spec["confidence"], spec["sample_n"], spec["basis"])
        num += w * (a - NEUTRAL)
        tot += w
        dims[dim] = {"affinity": round(a, 4), "weight": round(w, 4),
                     "mode": spec["mode"], "basis": spec["basis"],
                     "model_value": spec["value"],
                     "agent_value": agent_vector[dim]}
    if not dims:
        return NEUTRAL, {"status": "no-shared-dimensions", "dims": {}}
    return (NEUTRAL + num / max(tot, 1.0),
            {"status": "ok", "total_weight": round(tot, 4), "dims": dims})
