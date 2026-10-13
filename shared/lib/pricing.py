"""pricing.py - single source of truth for model cost.

Extracted 2026-08-13. Previously two tables disagreed: budget.PRICES carried
real per-model rates while telemetry.COST_PER_MTOK held a one-entry stub
({'phi3:mini': (0,0)}). telemetry.log_call therefore wrote NULL cost_usd for
every non_local_model_family model -- 1514 of 1710 rows -- while budget.month_spend_usd()
re-derived correct totals at read time from its own table. Both now call here.

Rates are USD per MILLION tokens, (input, output). Unknown model -> conservative
fallback (Frontier_model_family's rate) so a new unpriced paid model is never silently free.
"""
import sys

# USD per million tokens: (input, output)
PRICES = {
    "phi3:mini":               (0.0,  0.0),    # local Ollama
    "free_v2":   (0.0,  0.0),    # free tier
    "cheap_v1":       (0.14, 0.28),
    "cheap_v2":           (0.14, 0.28),
    "cheap_v3":       (0.14, 0.28),
    "frontier_v1":       (3.0,  15.0),
    # advanced_a_v1: verified against advanced_a_provider published pricing, 2026-08-13. The earlier
    # conservative fallback happened to equal the true published rate, so this
    # is a PROVENANCE correction rather than a cost correction -- dollar totals
    # must not move, and that invariance is part of its acceptance.
    # advanced_a_v1 retained: 237 historical ledger rows carry this slug and their
    # recorded costs are NOT being recalculated (the redirect date is unknown).
    "advanced_a_v1":                  (3.0,  15.0),
    # advanced_a_v2 published rates, <200k context. FALLBACK ONLY -- advanced_a_provider reports
    # authoritative billed cost per call, and this table cannot express the
    # >=200k tier ($2.50/$5.00), cached-input discounting, or reasoning-token
    # charges that actually govern the bill.
    "advanced_a_v2":                (1.25, 2.50),
    # free_v1: pinned alias target. Free_provider publishes no cost in
    # its usage block, so the table remains authoritative here.
    "free_v1":     (0.0,  0.0),
    "advanced_b_v1":  (2.5,  10.0),
}

# Structurally-free pseudo-models: router bookkeeping, not providers. These
# must NOT take the fallback -- charging them would invent spend for calls
# that never reached a paid backend.
NON_MODELS = ("none", "<configure-free-model>", "<configure>")

FALLBACK_IN, FALLBACK_OUT = 3.0, 15.0

BASIS_KNOWN, BASIS_FALLBACK, BASIS_FREE, BASIS_UNKNOWN = (
    "price_table", "fallback", "free", "unknown")

# Provider-reported ACTUAL billed cost. Outranks every local estimate: the
# provider has already applied whatever context tier, discount or rounding
# actually governed the charge. advanced_a_provider returns usage.cost_in_usd_ticks; others
# may follow. The rate table remains for forecasting and for providers that
# report nothing -- it is NEVER allowed to overwrite a reported actual.
BASIS_PROVIDER = "provider_reported"

# advanced_a_provider reports cost in "ticks". Per official advanced_a_provider documentation:
#   1 tick = 1e-10 USD, i.e. 1 USD = 1e10 ticks.
#
# Do NOT infer this from observed calls. An attempt to calibrate it against
# three samples failed and could not have succeeded: cached-token discounting
# and billed-but-hidden reasoning tokens distort the implied rate enough that
# no divisor fits cleanly. A wrong constant here is off by orders of magnitude
# AND carries provider_reported provenance -- the most trusted basis we have.
ADVANCED_A_TICKS_PER_USD = 10_000_000_000


def provider_cost(usage):
    """(cost_usd, basis) from a provider usage block, or (None, None) when the
    provider reported nothing. Never guesses."""
    if not isinstance(usage, dict):
        return None, None
    ticks = usage.get("cost_in_usd_ticks")
    if ticks is not None:
        try:
            return float(ticks) / ADVANCED_A_TICKS_PER_USD, BASIS_PROVIDER
        except (TypeError, ValueError):
            return None, None
    return None, None


def rate(model):
    """(pin, pout, basis) for a model. Never returns None."""
    if model in NON_MODELS:
        return 0.0, 0.0, BASIS_FREE
    price = PRICES.get(model)
    if price is None:
        sys.stderr.write(
            f"[pricing] WARNING: no price for model {model!r}; charging "
            f"conservative fallback ${FALLBACK_IN}/${FALLBACK_OUT} per MTok. "
            f"Add it to pricing.PRICES.\n")
        return FALLBACK_IN, FALLBACK_OUT, BASIS_FALLBACK
    return price[0], price[1], BASIS_KNOWN


def cost_and_basis(model, tokens_in, tokens_out, missing_tokens_as_zero=True):
    """(cost_usd, basis). missing_tokens_as_zero=True mirrors the read path
    (budget._row_cost): absent tokens count as 0. False mirrors ingestion:
    absent tokens -> (None, 'unknown'), because the call is unmeasurable."""
    if tokens_in is None or tokens_out is None:
        if not missing_tokens_as_zero:
            return None, BASIS_UNKNOWN
    pin, pout, basis = rate(model)
    ti = tokens_in or 0
    to = tokens_out or 0
    return (ti / 1_000_000.0) * pin + (to / 1_000_000.0) * pout, basis


def row_cost(model, tokens_in, tokens_out):
    """Read-path float cost. Exact behavioural replacement for budget._row_cost."""
    return cost_and_basis(model, tokens_in, tokens_out, True)[0]


def ingest_cost(model, tokens_in, tokens_out):
    """Write-path cost. None when tokens are missing (unmeasurable call)."""
    return cost_and_basis(model, tokens_in, tokens_out, False)[0]
