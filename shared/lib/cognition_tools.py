#!/usr/bin/env python3
"""
cognition_tools.py — Treebeard shared prompt-only cognition helpers.

Target path:  /opt/treebeard/shared/lib/cognition_tools.py  (root:treebeard 640)

Fleet principle (Treebeard ruling, 2026-07-18)
----------------------------------------------
All lib tools are fleet-wide by default. Caller passes its own identity as
`agent=` for telemetry. Nothing in the lib is owned by or scoped to a single
agent. Anything only one agent would ever plausibly call belongs in that
agent's own logic, not the lib.

These three are pure prompt-backed inference over router.route() — no side
effects, no external I/O, no persistence. Any agent can call any of them:
Clark can translate a JD, Treebeard can tone-check a disagreement note, Han
can generate positioning variants. That's why they live here, not in an
agent module.

Contract
--------
- Each function returns a structured dict (Treebeard's spec: structured
  result, not raw LLM text), always with the same top-level shape:
      {"ok": bool, "tool": str, "agent": str, "tier": str|None,
       "model": str|None, "result": <tool-specific>, "error": str|None}
- Never raises on operational failure: router.route() returns ok=False with
  .error rather than raising, and these mirror that. A caller can always
  branch on result["ok"].
- The constitution is auto-prepended by route()'s gate; these supply only the
  task-specific system prompt.
"""
from __future__ import annotations
import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import router


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _run(tool, agent, system, user, *, force_tier=None, max_tokens=1024):
    """Route one cognition call and package the Result into the standard dict.
    Never raises: an operational failure comes back as ok=False + error."""
    try:
        res = router.route(system, user, agent=agent,
                           force_tier=force_tier, max_tokens=max_tokens)
    except Exception as e:  # defensive; route() is documented not to raise
        return {"ok": False, "tool": tool, "agent": agent, "tier": None,
                "model": None, "result": None,
                "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": bool(res.ok),
        "tool": tool,
        "agent": agent,
        "tier": res.tier,
        "model": res.model,
        "result": res.text if res.ok else None,
        "error": res.error,
    }


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------
def jd_to_pain(jd_text: str, *, agent: str = "treebeard"):
    """Extract the real organizational need behind a job description —
    strip the boilerplate, name the problem the role actually exists to solve.
    Returns the standard structured dict; result is prose analysis."""
    system = (
        "You are an analyst that reads a job description and extracts the "
        "underlying organizational pain — the real problem the role exists to "
        "solve — beneath the boilerplate and generic requirements. Output: "
        "(1) the core problem in one sentence, (2) the 2-4 signals in the JD "
        "that reveal it, (3) what the org actually needs from whoever fills "
        "this, distinct from the listed qualifications. Be concrete and "
        "skeptical; do not restate the JD."
    )
    if not (jd_text or "").strip():
        return {"ok": False, "tool": "jd_to_pain", "agent": agent, "tier": None,
                "model": None, "result": None, "error": "empty jd_text"}
    return _run("jd_to_pain", agent, system, jd_text)


def positioning_variants(profile_and_target: str, *, agent: str = "treebeard", n: int = 3):
    """Given the operator's background and a specific target, produce N honest
    framings of the background for that target. Honesty is a hard constraint:
    every framing must be defensible against the operator's real record.
    Returns the standard structured dict; result is the framings."""
    system = (
        f"You produce exactly {int(n)} distinct, HONEST framings of a person's "
        "background for a specific target (role/org/audience). Each framing "
        "must be true to the actual record — you may emphasize, sequence, and "
        "frame, but never fabricate or imply experience that isn't there. For "
        "each: give it a short label, the one-line angle, and why it fits this "
        "target. If the background genuinely doesn't fit the target, say so "
        "plainly rather than stretching. Number them."
    )
    if not (profile_and_target or "").strip():
        return {"ok": False, "tool": "positioning_variants", "agent": agent,
                "tier": None, "model": None, "result": None,
                "error": "empty profile_and_target"}
    return _run("positioning_variants", agent, system, profile_and_target)


def tone_check(draft_text: str, *, agent: str = "treebeard"):
    """Evaluate a draft against the 'operator reads the full log and is not
    embarrassed' test. Flags anything deceptive, generic, sycophantic, or
    cringe BEFORE it reaches the operator. Returns the standard structured
    dict; result includes a verdict and specific flags."""
    system = (
        "You are a tone gate. Evaluate the draft against one test: if the "
        "operator read the full log of this being sent, would they be "
        "embarrassed? Flag anything that is deceptive, manipulative, generic/"
        "templated, sycophantic, or cringe. Output: (1) a one-word verdict — "
        "PASS or REVISE, (2) a bulleted list of specific flags with the "
        "offending phrase quoted, empty if PASS, (3) if REVISE, the single "
        "most important fix. Be strict; a false PASS is worse than a false "
        "REVISE."
    )
    if not (draft_text or "").strip():
        return {"ok": False, "tool": "tone_check", "agent": agent, "tier": None,
                "model": None, "result": None, "error": "empty draft_text"}
    return _run("tone_check", agent, system, draft_text)


# ---------------------------------------------------------------------------
# Thin CLI shim — for the OPERATOR's use (per Treebeard's convention:
# agents import and call; the CLI is the operator surface). Reads input text
# from stdin so multi-line JDs/drafts pipe in cleanly.
# ---------------------------------------------------------------------------
def _main(argv):
    import json
    tools = {
        "jd_to_pain": jd_to_pain,
        "positioning_variants": positioning_variants,
        "tone_check": tone_check,
    }
    if len(argv) < 2 or argv[1] not in tools:
        print(f"usage: cognition_tools.py {{{'|'.join(tools)}}} [--agent NAME]  "
              f"(input text on stdin)", file=sys.stderr)
        return 2
    tool = argv[1]
    agent = "treebeard"
    if "--agent" in argv:
        i = argv.index("--agent")
        if i + 1 < len(argv):
            agent = argv[i + 1]
    text = sys.stdin.read()
    out = tools[tool](text, agent=agent)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


# ---------------------------------------------------------------------------
# Selftest — validates structure and no-raise contract WITHOUT calling the
# real router (which needs live backends/keys). Injects a fake route().
# ---------------------------------------------------------------------------
def _selftest():
    import types
    passed = total = 0

    def ok(label, cond):
        nonlocal passed, total
        total += 1
        passed += bool(cond)
        print(("PASS " if cond else "FAIL ") + label)

    # Fake Result + route so the selftest is hermetic (no backends/keys).
    class _FakeResult:
        def __init__(self, ok=True, text="stub output", tier="free",
                     model="stub-model", error=None):
            self.ok, self.text, self.tier, self.model, self.error = \
                ok, text, tier, model, error

    real_route = router.route
    try:
        # success path
        router.route = lambda system, user, **kw: _FakeResult()
        r = jd_to_pain("Senior PM wanted, must herd cats", agent="clark")
        ok("jd_to_pain returns ok dict", r["ok"] and r["tool"] == "jd_to_pain")
        ok("agent attribution passes through", r["agent"] == "clark")
        ok("structured shape has all keys",
           set(r) == {"ok", "tool", "agent", "tier", "model", "result", "error"})
        ok("result carries router text on success", r["result"] == "stub output")

        r2 = positioning_variants("bg: 9y TPM; target: fintech PM", agent="han", n=3)
        ok("positioning_variants ok", r2["ok"] and r2["tool"] == "positioning_variants")

        r3 = tone_check("Dear Sir, I am writing to humbly beseech you", agent="treebeard")
        ok("tone_check ok", r3["ok"] and r3["tool"] == "tone_check")

        # empty-input guards return ok=False without calling route
        ok("empty jd guarded", jd_to_pain("   ", agent="han")["ok"] is False)
        ok("empty draft guarded", tone_check("", agent="han")["ok"] is False)

        # router failure -> ok=False, error surfaced, no raise
        router.route = lambda system, user, **kw: _FakeResult(
            ok=False, text="", error="all tiers exhausted")
        rf = jd_to_pain("real jd", agent="clark")
        ok("router failure -> ok False", rf["ok"] is False)
        ok("router error surfaced", rf["error"] == "all tiers exhausted")
        ok("result None on failure", rf["result"] is None)

        # route() raising is caught, not propagated
        def _boom(system, user, **kw):
            raise RuntimeError("backend on fire")
        router.route = _boom
        rb = tone_check("draft", agent="han")
        ok("route raise caught, ok False", rb["ok"] is False)
        ok("raise packaged into error", "backend on fire" in (rb["error"] or ""))
    finally:
        router.route = real_route

    print(f"\n{passed}/{total} passed")
    return passed == total


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    sys.exit(_main(sys.argv))
