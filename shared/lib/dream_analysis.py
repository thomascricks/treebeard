"""dream_analysis.py - Dream's evidence -> hypothesis proposal boundary.

Deliberately SEPARATE from dream.py, which does memory consolidation. Different
concern, different failure mode, and entangling adaptation logic with journal
writing would make both harder to reason about.

    router evidence -> Moon observation -> Dream hypothesis
                                             -> Treebeard approval
                                                -> tbpersona mutation

Dream's authority ends at PROPOSAL. This module imports the append-only
hypothesis API and nothing else that writes: it cannot approve, cannot reach
decisions.db, cannot invoke persona_writer, cannot touch bounds or adaptive
state. Those are blocked by UID at the filesystem regardless.

PRODUCTION_RULES IS DELIBERATELY EMPTY.
--------------------------------------
No evidence->dimension relationship has been established. Three candidates were
proposed and REJECTED as not causally defensible:

    truncated        -> depth          (may reflect max_tokens config, not depth)
    validator_reject -> discrimination (may reflect a strict validator or a
                                        model/tier mismatch, not the agent)
    retry_before_ok  -> depth          (may reflect provider outage or a
                                        capability floor, not the agent)

Each could be explained by model, tier or task configuration rather than agent
personality. Encoding them would be fabricated adaptation logic dressed as
learning -- the system would confidently adapt personalities on reasoning
nobody can defend.

A rule may be added ONLY when its evidence->dimension relationship can be
stated and argued. Until then this module reads evidence, finds no applicable
rule, and proposes nothing. That is the correct behaviour, not a gap.
"""
import sqlite3
import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"
MIN_SUPPORT = 10          # below this, propose nothing rather than something weak


class Rule:
    """An evidence->dimension mapping.

    evaluate(evidence, agent) -> dict or None. Returning a dict claims that the
    evidence supports moving one personality dimension. It MUST cite the
    observation ids or a reproducible query_basis behind that claim: an
    unauditable proposal is refused by the hypothesis API.
    """

    def __init__(self, name, dimension, evaluate, rationale_template):
        self.name = name
        self.dimension = dimension
        self.evaluate = evaluate
        self.rationale_template = rationale_template


# Production registry. EMPTY BY DESIGN -- see the module docstring.
PRODUCTION_RULES = []


def eligible_evidence(agent=None, db_path=DB_PATH,
                      fixture_observation_ids=None):
    """Real, learning-eligible evidence only.

    COALESCE(synthetic, 1) = 0 excludes both synthetic=1 and unclassified NULL
    rows. Excluding NULL matters as much as excluding 1: an unclassified row is
    not evidence that something is real.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        # fixture_observation_ids is the ONLY way to see non-real evidence, and
        # it admits exactly the rows named -- it does not drop the synthetic
        # filter globally. An earlier version did, and a fixture run seeded
        # with 12 observations saw 24, picking up unrelated synthetic rows for
        # other agents. A test seam that widens the query is a seam that can
        # surprise you; one that names its rows cannot.
        args = []
        if fixture_observation_ids:
            _ph = ",".join("?" for _ in fixture_observation_ids)
            where = f"WHERE o.id IN ({_ph})"
            args.extend(fixture_observation_ids)
        else:
            where = "WHERE COALESCE(o.synthetic, 1) = 0"
        if agent:
            where += " AND o.agent = ?"
            args.append(agent)
        obs = [dict(r) for r in con.execute(
            f"SELECT o.* FROM observations o {where} ORDER BY o.id", args)]
        args2 = []
        if fixture_observation_ids:
            w2 = "WHERE decision_id IN (SELECT decision_id FROM observations WHERE id IN (%s))" % \
                 ",".join("?" for _ in fixture_observation_ids)
            args2.extend(fixture_observation_ids)
        else:
            w2 = "WHERE COALESCE(synthetic, 1) = 0"
        if agent:
            w2 += " AND agent = ?"
            args2.append(agent)
        sig = [dict(r) for r in con.execute(
            f"SELECT * FROM signals {w2} ORDER BY id", args2)]
        return {"observations": obs, "signals": sig,
                "n_obs": len(obs), "n_signals": len(sig)}
    finally:
        con.close()


def analyse(agent, rules=None, db_path=DB_PATH, synthetic=0, dry_run=False,
            fixture_observation_ids=None):
    """Run the rule registry over eligible evidence. Returns a report.

    `rules` defaults to PRODUCTION_RULES (empty). Test fixtures pass their own
    rule list explicitly, so a fixture rule can never enter production lookup
    by accident.
    """
    rules = PRODUCTION_RULES if rules is None else rules
    ev = eligible_evidence(agent, db_path,
                           fixture_observation_ids=fixture_observation_ids)
    report = {"agent": agent, "n_obs": ev["n_obs"], "n_signals": ev["n_signals"],
              "rules_considered": len(rules), "proposals": [], "skipped": []}

    if not rules:
        report["skipped"].append("no rules in registry: no defensible "
                                 "evidence->dimension mapping established")
        return report
    if ev["n_obs"] < MIN_SUPPORT:
        report["skipped"].append(
            f"insufficient evidence: {ev['n_obs']} observations < {MIN_SUPPORT}")
        return report

    import hypotheses as HY
    for rule in rules:
        try:
            out = rule.evaluate(ev, agent)
        except Exception as e:
            report["skipped"].append(f"{rule.name}: evaluator raised ({e})")
            continue
        if not out:
            report["skipped"].append(f"{rule.name}: no finding")
            continue
        if not out.get("observation_ids") and not out.get("query_basis"):
            report["skipped"].append(f"{rule.name}: REFUSED, cites no evidence")
            continue
        if dry_run:
            report["proposals"].append({"rule": rule.name, "dry_run": True, **out})
            continue
        made = HY.propose(
            agent=agent, dimension=rule.dimension, direction=out["direction"],
            magnitude=out.get("magnitude"), confidence=out.get("confidence"),
            observation_ids=out.get("observation_ids"),
            query_basis=out.get("query_basis"),
            conditioned_on=out.get("conditioned_on"),
            rationale=rule.rationale_template.format(**out),
            proposer="dream", synthetic=synthetic, db_path=db_path)
        if made:
            hid, chash = made
            report["proposals"].append({"rule": rule.name, "hypothesis_id": hid,
                                        "content_hash": chash[:16], **out})
    return report
