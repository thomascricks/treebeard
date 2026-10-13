"""policy_regression.py - regression proofs for the 2026-08-16 routing policy:
functionality/measured quality before cost, bounded exploration, no provider
favoritism. Each test names the policy clause it pins. Runs the REAL
router._select_start with injected policy/perf/profiles so proofs are
deterministic and independent of /etc content drift."""
import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import affinity
import perf as perfmod
import selection
import router

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def mkpolicy(cost=0.35, perf_w=1.0, eps=0.15, explore=0.05):
    return {"order": ["basic", "standard", "advanced"],
            "tier_band": {"local": "basic", "free": "standard",
                          "cheap": "standard", "frontier": "advanced",
                          "advanced_a": "advanced", "advanced_b": "advanced"},
            "class_floor": {"simple": "basic", "medium": "standard",
                            "complex": "advanced"},
            "weights": {"cost": cost, "health": 0.5, "perf": perf_w},
            "selection": {"rate_reference_mtok": 20.0, "near_tie_epsilon": eps,
                          "affinity_epsilon": 0.02, "explore_rate": explore,
                          "explore_rate_min": 0.01, "explore_rate_max": 0.25,
                          "explore_weight_threshold": 1.0}}


RATES = {"frontier": 9.0, "advanced_a": 1.875, "advanced_b": 6.25}


def perf_stub(vals, n=20, status="measured"):
    def derive(models, **kw):
        by_model = {"frontier_v1": "frontier", "advanced_a_v2": "advanced_a",
                    "advanced_b_v1": "advanced_b"}
        out = {}
        for m in models:
            t = by_model.get(m, m)
            out[m] = {"perf": vals.get(t, 0.75), "n": n, "ok": n,
                      "neg": 0, "pos": 0, "status": status}
        return out
    return derive


def profiles_stub(profs):
    return lambda path=None: (profs, "test-rev")


def find_seed(fire, policy, lo=0, hi=4000):
    """Deterministic decision_id whose explore draw does/doesn't fire."""
    r = policy["selection"]
    for i in range(lo, hi):
        _, audit = selection.explore_pick(f"seed-{i}", ["x"], r["explore_rate"],
                                          r["explore_rate_min"], r["explore_rate_max"])
        if audit["fired"] == fire:
            return f"seed-{i}"
    raise RuntimeError("no seed found")


def run_select(policy, perf_vals=None, profiles=None, pvec=None,
               decision_id="seed-none", label="complex"):
    o_pol, o_der, o_prof = selection.load_policy, perfmod.derive, affinity.load_profiles
    selection.load_policy = lambda path=None: policy
    if perf_vals is not None:
        perfmod.derive = perf_stub(perf_vals)
    affinity.load_profiles = profiles_stub(profiles or {})
    try:
        tier, reason, cap = router._select_start(
            label, "test", "prompt", "policytest", pvec=pvec,
            backends=None, decision_id=decision_id)
        return tier, reason, cap
    finally:
        selection.load_policy, perfmod.derive, affinity.load_profiles = o_pol, o_der, o_prof


def file_sig(path):
    """(sha256, st_mtime_ns) of a file, or None when it is absent."""
    import hashlib
    if not _tb_os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest(), _tb_os.stat(path).st_mtime_ns


def policy_checks():
    pol = mkpolicy()
    no_fire = find_seed(False, pol)
    fire = find_seed(True, pol)

    # 1. Materially better measured perf beats a cheaper model. "Material"
    #    means a perf gap the cost term cannot offset: max cost offset within
    #    the advanced set is w_cost*(9.0-1.875)/20 = 0.125, so a 0.35 perf gap
    #    always splits the band and the better performer wins outright.
    #    (A smaller gap, e.g. 0.25, may legitimately stay comparable to a 4.8x
    #    cheaper model -- that is the intended cost-influences-close-calls
    #    zone, exercised by p2b/p5, not an override of this rule.)
    t, r, cap = run_select(pol, {"frontier": 0.95, "advanced_a": 0.60, "advanced_b": 0.60},
                           decision_id=no_fire)
    check("p1_perf_beats_cheaper", t == "frontier" and cap["method"] == "score"
          and cap["band"] == ["frontier"])

    # 2. Cheapest wins OUTRIGHT when comparable on perf and the price gap is
    #    material (outside epsilon).
    o = selection.base_score(["a", "b"], {"a": 1.0, "b": 18.0},
                             {"a": 1.0, "b": 1.0}, pol,
                             perf={"a": 0.8, "b": 0.8})
    band = selection.tie_band(o, 0.15)
    check("p2_cheaper_wins_comparable", band == ["a"])
    #    ...and cost still ORDERS the band when candidates are close.
    t, r, cap = run_select(pol, {"frontier": 0.75, "advanced_a": 0.75, "advanced_b": 0.75},
                           decision_id=no_fire)
    check("p2b_cost_orders_band", cap["band"][0] == "advanced_a" and len(cap["band"]) == 3)

    # 3. Qualified non-cheapest alternatives are exploration candidates and
    #    actually receive exploration traffic across seeds. (band[0] itself
    #    may also be drawn -- the exclusion was removed deliberately: the
    #    score winner is not the exploitation winner under weighted shares.)
    even = {"frontier": 0.75, "advanced_a": 0.75, "advanced_b": 0.75}
    picks, cands_ok, scope_ok = set(), True, True
    i = 0
    while len(picks) < 2 and i < 5000:
        seed = f"p3-{i}"; i += 1
        _, audit = selection.explore_pick(seed, ["x"], 0.05, 0.01, 0.25)
        if not audit["fired"]:
            continue
        t, r, cap = run_select(pol, even, decision_id=seed)
        if cap["method"] == "explore":
            picks.add(t)
            cands_ok &= {"frontier", "advanced_b"} <= set(cap["explore"]["candidates"])
            scope_ok &= cap["explore"]["scope"] == "eligible"
    check("p3_explore_reaches_noncheapest",
          cands_ok and scope_ok and any(x != "advanced_a" for x in picks))

    # 4. A singleton cost-driven band cannot block evidence collection
    #    (the OLD policy's self-seal, now broken).
    old = mkpolicy(cost=1.0, perf_w=0.0)
    picks = set()
    i = 0
    band_len = None
    while len(picks) < 2 and i < 5000:
        seed = f"p4-{i}"; i += 1
        _, audit = selection.explore_pick(seed, ["x"], 0.05, 0.01, 0.25)
        if not audit["fired"]:
            continue
        t, r, cap = run_select(old, even, decision_id=seed)
        band_len = len(cap["band"])
        if cap["method"] == "explore":
            picks.add(t)
    check("p4_singleton_band_still_explores",
          band_len == 1 and any(x != "advanced_a" for x in picks))
    #    Non-fired draws on a singleton band still exploit normally.
    t2, _, cap2 = run_select(old, {"frontier": 0.75, "advanced_a": 0.75, "advanced_b": 0.75},
                             decision_id=find_seed(False, old))
    check("p4b_singleton_exploits_when_not_fired",
          t2 == "advanced_a" and cap2["method"] == "score")

    # 5/7. Personality differentiates comparable candidates; different agent
    #      vectors produce different selections under identical conditions.
    profs = {"frontier": {"depth": {"value": 0.9, "confidence": 0.9,
                                    "sample_n": 0, "mode": "match", "basis": "asserted"}},
             "advanced_b": {"depth": {"value": 0.2, "confidence": 0.9,
                                  "sample_n": 0, "mode": "match", "basis": "asserted"}},
             "advanced_a": {"depth": {"value": 0.55, "confidence": 0.9,
                                "sample_n": 0, "mode": "match", "basis": "asserted"}}}
    import collections
    cA, cB = collections.Counter(), collections.Counter()
    for i in range(300):
        tA, _, capA = run_select(pol, even, profiles=profs, pvec={"depth": 0.9},
                                 decision_id=f"p57-{i}")
        tB, _, capB = run_select(pol, even, profiles=profs, pvec={"depth": 0.2},
                                 decision_id=f"p57-{i}")
        if capA["method"] == "personality_weighted": cA[tA] += 1
        if capB["method"] == "personality_weighted": cB[tB] += 1
    # 5. Personality shapes comparable candidates: each vector's preferred
    #    model takes the LARGEST share -- but never all of it (no winner-
    #    take-all; every qualified band member keeps a floored share).
    check("p5_personality_weights_share",
          cA["frontier"] == max(cA.values()) and cB["advanced_b"] == max(cB.values()))
    #    Pinned on the draw AUDIT (deterministic), not realized counts: the
    #    floor share is trace-level by design and need not appear in a small
    #    sample. Every band member keeps weight > 0; nobody holds weight 1.0.
    wA = capA["affinity_draw"]["weights"]
    check("p5b_no_winner_take_all",
          set(wA) == {"frontier", "advanced_a", "advanced_b"}
          and all(v > 0 for v in wA.values())
          and max(wA.values()) < 1.0)
    # 7. Different agent vectors produce materially different selections
    #    under identical conditions and identical decision seeds.
    check("p7_agents_can_differ",
          cA["frontier"] > cB["frontier"] and cB["advanced_b"] > cA["advanced_b"])

    # 6. Personality cannot override materially better measured performance:
    #    the perf gap splits the band, so affinity (which strongly prefers
    #    advanced_b/advanced_a under this vector) never gets a vote.
    t, _, cap = run_select(pol, {"frontier": 0.95, "advanced_a": 0.60, "advanced_b": 0.60},
                           profiles=profs, pvec={"depth": 0.2},
                           decision_id=no_fire)
    check("p6_personality_subordinate_to_perf",
          t == "frontier" and cap["method"] == "score"
          and cap["band"] == ["frontier"])

    # 8. No provider favoritism: identical evidence -> identical perf; scoring
    #    is symmetric under model renaming.
    d = perfmod.derive(["model_x", "model_y"],
                       db_path="/nonexistent/no.db")
    check("p8_derivation_name_blind", d["model_x"]["perf"] == d["model_y"]["perf"])
    s = selection.base_score(["m1", "m2"], {"m1": 5.0, "m2": 5.0},
                             {"m1": 1.0, "m2": 1.0}, pol,
                             perf={"m1": 0.8, "m2": 0.8})
    check("p8b_scoring_symmetric", s["m1"] == s["m2"])

    # 9. Advanced_a still wins when evidence justifies it.
    t, _, cap = run_select(pol, {"frontier": 0.70, "advanced_a": 0.95, "advanced_b": 0.70},
                           profiles=profs, pvec={"depth": 0.9},  # prefers frontier
                           decision_id=no_fire)
    check("p9_advanced_a_wins_on_evidence", t == "advanced_a" and cap["method"] == "score")

    # 10. Exploration is reproducible: same decision_id -> identical outcome.
    r1 = selection.explore_pick("repro-1", ["a", "b", "c"], 0.9, 0.01, 1.0)
    r2 = selection.explore_pick("repro-1", ["a", "b", "c"], 0.9, 0.01, 1.0)
    check("p10_explore_reproducible", r1 == r2)

    # Guard: perf telemetry lands in the decision capture.
    t, _, cap = run_select(pol, {"frontier": 0.75, "advanced_a": 0.75, "advanced_b": 0.75},
                           decision_id=no_fire)
    check("g1_perf_in_capture",
          isinstance(cap.get("perf"), dict)
          and all(k in cap["perf"] for k in ("frontier", "advanced_a", "advanced_b"))
          and all("status" in v for v in cap["perf"].values()))


def main():
    # WHY: this suite is in the SAFE tier, whose contract is "no production
    # write". p2b/g1 reach the real selection.round_robin, and .rr_state is
    # LIVE routing state: every run advanced production rotation by +2
    # (observed 2026-09-26). Redirect round-robin to a private temp file for
    # the whole run; restore the saved function and remove the dir on exit.
    import json, shutil, tempfile
    saved_rr = selection.round_robin
    saved_defaults = saved_rr.__defaults__
    prod_rr = saved_defaults[0]
    prod_before = file_sig(prod_rr)
    rr_dir = tempfile.mkdtemp(prefix="policy-rr-")
    rr_path = _tb_os.path.join(rr_dir, ".rr_state")
    rr_calls = []

    def isolated_round_robin(band, state_path=None):
        rr_calls.append(("+".join(sorted(band)), len(band), rr_path))
        return saved_rr(band, state_path=rr_path)

    selection.round_robin = isolated_round_robin
    try:
        policy_checks()
        # R1-R3 run inside the isolation.
        check("r1_rr_isolation_active",
              len(rr_calls) >= 1 and all(p == rr_path for _, _, p in rr_calls))
        calls, sizes = {}, {}
        for key, n, _ in rr_calls:
            calls[key] = calls.get(key, 0) + 1
            sizes[key] = n
        expected = {k: c % sizes[k] for k, c in calls.items() if sizes[k] > 1}
        try:
            with open(rr_path) as fh:
                private = json.load(fh)
        except (OSError, ValueError):
            private = None
        print("INFO private rr_state: " + json.dumps(private, sort_keys=True))
        check("r2_rr_private_state_real", private == expected)
        check("r3_rr_production_untouched", file_sig(prod_rr) == prod_before)
    finally:
        selection.round_robin = saved_rr
        shutil.rmtree(rr_dir, ignore_errors=True)
    check("r4_rr_restored",
          selection.round_robin is saved_rr
          and saved_rr.__defaults__ == saved_defaults
          and not _tb_os.path.exists(rr_dir))

    print("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS))
    return 1 if bool(FAILS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
