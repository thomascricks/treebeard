#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
career_hunter_daily.py -- Sprint 1 daily governed job-search runner.

Pipeline (fixed stages, no planning loop, no task generation):
  1. load career_profile.txt, skill docs, plan lists, seen-URL store
  2. governed Han turn (ask.py, venv interpreter): adjacent_career_mapper
     -> JSON query plan. Parse-fail => one retry => static fallback plan
     (degradation is recorded, never silent).
  3. toolbox run career_search '<plan>' (podman room, door, ledger)
  4. governed Han turn: fit_scorer + stretch_detector +
     application_priority over the kept candidates -> report text. The
     machine-readable result (WORTHWHILE line + SELECTED_JSON block) is
     emitted FIRST, before the narrative, so a report that overruns the
     model output ceiling can only lose the expendable narrative tail (B6,
     2026-10-01). The result is then validated for completeness.
  5. write dated report + atomic status card; update seen store

States (card line 1 and exit code):
  SUCCEEDED             exit 0
  NO_WORTHWHILE_JOBS    exit 0 (valid outcome, distinct in the card)
  FAILED:<stage>        exit 1 (systemd Result=failed; card says why)
  FAILED:scoring-result exit 1 (B6): the scoring turn was contract-valid but
                        its machine-readable result is missing, truncated,
                        malformed, or lost to the output ceiling. The run
                        fails explicitly -- never a SUCCEEDED without a
                        parsed result. A COMPLETE result with zero
                        selections stays green.

Paths (env-overridable for tests):
  CAREER_ROOT   /opt/treebeard/shared/career
  ASK           /opt/treebeard/shared/venv/bin/python3 + ask.py  (venv:
                bare python3 has no chromadb -- the bug #8 launch-path
                lesson, applied here by construction)
  TOOLBOX       /opt/treebeard/shared/toolbox/toolbox.py
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

CAREER_ROOT = os.environ.get("CAREER_ROOT", f"{_TB_BASE}/shared/career")
SKILLS_DIR = os.environ.get(
    "CAREER_SKILLS", f"{_TB_BASE}/shared/toolbox/data/skills")
PROFILE = os.environ.get(
    "CAREER_PROFILE", f"{_TB_BASE}/shared/operator/career_profile.txt")
VENV_PY = os.environ.get(
    "CAREER_VENV_PY", f"{_TB_BASE}/shared/venv/bin/python3")
ASK_PY = os.environ.get("CAREER_ASK", f"{_TB_BASE}/treebeard/ask.py")
TOOLBOX_PY = os.environ.get(
    "CAREER_TOOLBOX", f"{_TB_BASE}/shared/toolbox/toolbox.py")
TOOLBOX_DIR = os.path.dirname(TOOLBOX_PY)
AGENT = "han"
# Lane parameters (Bill-Payer Patrol seam, 2026-09-04). Defaults reproduce
# the Primary lane strings byte-for-byte; a second lane sets these in its
# unit environment. CAREER_ROUTING_CAPSULE is passed to ask.py so the
# mission selector scores the capsule, not the prompt.
_LANE_DEFAULT = "CAREER HUNTER"
LANE = os.environ.get("CAREER_LANE", _LANE_DEFAULT).strip() or _LANE_DEFAULT
LANE_SLUG = (os.environ.get("CAREER_BOARD_AGENT", "career_hunter").strip()
             or "career_hunter")
CAPSULE = os.environ.get("CAREER_ROUTING_CAPSULE", "").strip()
# Explicit operator mission assignment for this lane (binding in ask.py).
MISSION = os.environ.get("CAREER_MISSION", "").strip()
ASK_TIMEOUT = int(os.environ.get("CAREER_ASK_TIMEOUT", "420"))
TOOL_TIMEOUT = int(os.environ.get("CAREER_TOOL_TIMEOUT", "300"))
SCORE_CAP = int(os.environ.get("CAREER_SCORE_CAP", "40"))
SUPPRESS_CAP = 400
SEP = "-" * 60
_BOARD_ENABLED = True

FALLBACK_PLAN = {"queries": [
    {"q": "program manager healthcare operations remote", "family": "program_management", "count": 8},
    {"q": "AI governance program manager remote", "family": "technology_governance", "count": 8},
    {"q": "research program operations manager boston", "family": "research_program_operations", "count": 8},
    {"q": "chief of staff strategy operations remote nonprofit", "family": "institutional_strategy", "count": 8},
    {"q": "innovation program lead remote", "family": "innovation_programs", "count": 8},
    {"q": "technology policy manager remote", "family": "technology_policy", "count": 8},
    {"q": "implementation delivery manager insurance remote", "family": "implementation_leadership", "count": 8},
    {"q": "senior product manager boston hybrid", "family": "conventional_pm_po", "count": 8},
]}


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# --- ask.py governed turn --------------------------------------------------

def _ask_argv(capsule, mission=""):
    argv = [VENV_PY, ASK_PY, "--agent", AGENT]
    if capsule:
        argv += ["--routing-capsule", capsule]
    if mission:
        argv += ["--mission", mission]
    return argv


def han_turn(prompt):
    """Run one governed Han turn. Returns (ok, status, body). Raises
    StageError on subprocess-level failure."""
    # FC04 (2026-10-01): bound to ask.py's machine contract (--json, v1),
    # never to its human output. Contract violation, ok=false or an empty
    # reply is a FAILED turn (ok False), never an empty success.
    if f"{_TB_BASE}/shared/lib" not in sys.path:
        sys.path.insert(0, f"{_TB_BASE}/shared/lib")
    import ask_contract
    r = ask_contract.run(_ask_argv(CAPSULE, MISSION), input=prompt,
                         timeout=ASK_TIMEOUT)
    if r.timed_out:
        raise StageError("han turn timed out after %ds" % ASK_TIMEOUT)
    if r.returncode is None:
        raise StageError("ask.py %s" % r.error)
    if r.returncode != 0:
        raise StageError("ask.py rc=%d %s stderr=%s"
                         % (r.returncode, r.error[:200],
                            (r.stderr or "")[-300:]))
    # C3b(i): receipts acknowledged on machine delivery, once, recorded.
    r.acknowledge("machine:career_hunter_daily")
    return r.triple()


class StageError(Exception):
    pass


# --- plan handling -----------------------------------------------------------

def extract_json(text):
    """Pull the first JSON object out of a model reply (fences tolerated)."""
    t = re.sub(r"```(?:json)?", "", text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in reply")
    return json.loads(t[i:j + 1])


def valid_plan(plan):
    qs = plan.get("queries")
    if not isinstance(qs, list) or not (1 <= len(qs) <= 14):
        return False
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("q"), str) \
                or not q["q"].strip() or len(q["q"]) > 300:
            return False
    return True


def load_lists():
    path = os.path.join(CAREER_ROOT, "plan_lists.json")
    try:
        d = json.loads(read(path))
    except (OSError, ValueError):
        return {"exclude_companies": [], "flag_companies": [],
                "pursued_companies": []}, "plan_lists MISSING/UNREADABLE"
    return d, ""


def load_seen():
    path = os.path.join(CAREER_ROOT, "seen_urls.json")
    try:
        return json.loads(read(path))
    except (OSError, ValueError):
        return {"urls": {}}


def save_seen(seen):
    atomic_write(os.path.join(CAREER_ROOT, "seen_urls.json"),
                 json.dumps(seen, indent=1, sort_keys=True) + "\n")


def suppress_list(seen):
    urls = sorted(seen.get("urls", {}).items(), key=lambda kv: kv[1],
                  reverse=True)
    return [u for u, _ in urls[:SUPPRESS_CAP]]


# --- toolbox run -------------------------------------------------------------

def run_tool(plan):
    try:
        p = subprocess.run(
            [sys.executable, TOOLBOX_PY, "run", "career_search",
             json.dumps(plan)],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT,
            cwd=TOOLBOX_DIR)
    except subprocess.TimeoutExpired:
        raise StageError("toolbox run timed out after %ds" % TOOL_TIMEOUT)
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"run (r[0-9a-f]+): career_search -> (\w+)", out)
    if not m or m.group(2) != "DONE" or p.returncode != 0:
        raise StageError("toolbox run failed rc=%d: %s"
                         % (p.returncode, out[-400:]))
    rid = m.group(1)
    hits = glob.glob(os.path.join(TOOLBOX_DIR, "data", "runs", rid,
                                  "**", "candidates.json"), recursive=True)
    if not hits:
        raise StageError("run %s DONE but candidates.json not found" % rid)
    return rid, json.loads(read(hits[0]))


# --- prompts -----------------------------------------------------------------

def mapping_prompt(profile, mapper_doc, strict=False):
    head = ("You are performing the daily lateral job-search mapping for "
            "the operator. Apply the skill below to the profile below.")
    tail = ("Output ONLY the JSON query plan object. No prose before or "
            "after." if not strict else
            "Your previous reply was not parseable JSON. Output ONLY one "
            "JSON object of the exact shape shown, with no other text.")
    return "\n\n".join((head, "=== SKILL ===", mapper_doc,
                        "=== CAREER PROFILE ===", profile, tail))


def scoring_prompt(profile, docs, candidates):
    slim = [{k: c.get(k, "") for k in
             ("title", "url", "description", "family", "source_tier",
              "source_reason", "stale_evidence", "company_flag",
              "previously_pursued")} for c in candidates]
    return "\n\n".join((
        "You are scoring today's live job-search candidates for the "
        "operator. Apply the three skills below, in order, to the profile "
        "and candidate list below.",
        "=== SKILL: fit_scorer ===", docs["fit_scorer"],
        "=== SKILL: stretch_detector ===", docs["stretch_detector"],
        "=== SKILL: application_priority ===", docs["application_priority"],
        "=== CAREER PROFILE ===", profile,
        "=== TODAY'S CANDIDATES (JSON) ===", json.dumps(slim, indent=1),
        "Produce the daily report now. OUTPUT ORDER IS MANDATORY: the "
        "machine-readable result comes FIRST, so a long report can never "
        "lose it to truncation. In this exact order:\n"
        "1. The FIRST line: 'WORTHWHILE: N' where N is the count of "
        "credible live opportunities rated MAYBE or better.\n"
        "2. The SECOND line: 'SELECTED_JSON: ' followed by a JSON array "
        "(at most 5, use [] if none) of the best APPLY NOW/APPLY jobs the "
        "operator should apply to today, each object copying "
        "url/title/description exactly from the candidate data plus "
        "company, role, fit, comp, and a one-line why. Keep it on/after "
        "that single marker line.\n"
        "3. THEN the human narrative: the six-strongest block per "
        "application_priority, then remaining tiers briefly. Plain text "
        "only.\n"
        "Put WORTHWHILE and SELECTED_JSON at the very beginning, before any "
        "other text or preamble, so only the narrative tail is ever cut."))


# --- scoring result validation (B6) -----------------------------------------

def scoring_result(body):
    """B6: validate the scoring turn's MACHINE-READABLE result. Returns
    (ok, worthwhile, reason). ok is True only when BOTH markers are present
    and well-formed: a 'WORTHWHILE: N' integer line AND a complete, parseable
    'SELECTED_JSON: [ ... ]' array. A missing, truncated (ceiling-lost),
    unterminated or malformed result is ok False -- never a silent success.

    Position-independent: SELECTED_JSON is located with find() and parsed with
    raw_decode(), exactly as career_packets.parse_selected does, so a COMPLETE
    result is accepted wherever it sits (a complete tail is fine); the prompt
    places the result first so the output ceiling can only cut the narrative.
    WORTHWHILE is extracted with the same regex the producer has always used,
    so every report accepted before B6 still parses to the same N."""
    m = re.search(r"^WORTHWHILE:\s*(\d+)", body, re.MULTILINE)
    if not m:
        if re.search(r"^WORTHWHILE:", body, re.MULTILINE):
            return False, -1, "WORTHWHILE line malformed (no integer count)"
        return False, -1, "WORTHWHILE line missing (result lost or truncated)"
    worthwhile = int(m.group(1))
    idx = body.find("SELECTED_JSON:")
    if idx < 0:
        return False, worthwhile, ("SELECTED_JSON block missing "
                                   "(result lost or truncated)")
    start = body.find("[", idx)
    if start < 0:
        return False, worthwhile, ("SELECTED_JSON present but no JSON array "
                                   "(truncated at the marker)")
    try:
        json.JSONDecoder().raw_decode(body[start:])
    except ValueError as e:
        return False, worthwhile, ("SELECTED_JSON truncated or malformed: %s"
                                   % str(e)[:80])
    return True, worthwhile, ""


def classify_run(ok_result, worthwhile):
    """B6: map a validated scoring result to a run state. An invalid/lost
    result is an explicit failure (never a false-green); a complete result is
    SUCCEEDED, or NO_WORTHWHILE_JOBS for a complete genuine zero. Both green
    paths keep the legitimate zero case out of false-red."""
    if not ok_result:
        return "FAILED:scoring-result"
    return "NO_WORTHWHILE_JOBS" if worthwhile == 0 else "SUCCEEDED"


# --- report / card -----------------------------------------------------------

def write_outputs(state, detail, report_text, counts, rid, notes):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = ["%s DAILY REPORT  %s  run=%s  state=%s"
            % (LANE, ts, rid or "-", state)]
    if detail:
        body.append(detail)
    if notes:
        body.append("NOTES: " + "; ".join(notes))
    if counts:
        body.append("SEARCH COUNTS: " + json.dumps(counts, sort_keys=True))
    body.append(SEP)
    body.append(report_text or "(no report body)")
    if state.startswith("FAILED"):
        # A failed run must never clobber the day's success report.
        # Failures append to a per-day log; the card still shows FAILED.
        rpt_path = os.path.join(CAREER_ROOT, "reports",
                                "failures-%s.log" % day)
        os.makedirs(os.path.dirname(rpt_path), exist_ok=True)
        with open(rpt_path, "a", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n" + SEP + "\n")
    else:
        rpt_path = os.path.join(CAREER_ROOT, "reports", "%s.txt" % day)
        atomic_write(rpt_path, "\n".join(body) + "\n")
    card = ["%s %s state=%s run=%s" % (LANE.replace(" ", "_"), ts, state,
                                        rid or "-"),
            "report=%s" % rpt_path]
    if counts:
        card.append("counts=%s" % json.dumps(counts, sort_keys=True))
    if detail:
        card.append(detail[:200])
    for ln in (report_text or "").splitlines():
        if ln.startswith("WORTHWHILE:"):
            card.insert(1, ln)
            break
    atomic_write(os.path.join(CAREER_ROOT, "status", LANE_SLUG + ".txt"),
                 "\n".join(card) + "\n")
    _board_write(state, detail, counts, notes, rid)
    return rpt_path


def _board_write(state, detail, counts, notes, rid=None):
    """Fail-soft statusboard card for Sun's digest. Never raises; a board
    failure must not kill the run whose outcome it merely reports."""
    if not _BOARD_ENABLED:
        return
    try:
        sys.path.insert(0, f"{_TB_BASE}/shared/lib")
        import statusboard
        board_status = "fail" if state.startswith("FAILED") else "ok"
        summary = "%s: %s%s" % (
            LANE.lower(), state, (" (%s)" % detail[:80]) if detail else "")
        statusboard.write_status(
            LANE_SLUG, board_status, summary,
            detail={"counts": counts or {}, "notes": (notes or [])[:6]},
            stale_after_hours=26, run_id=rid)
    except Exception as e:
        sys.stderr.write("WARN statusboard write failed: %s\n" % e)


# --- main --------------------------------------------------------------------

def main():
    notes = []
    rid, counts, report_text = None, None, ""
    try:
        profile = read(PROFILE)
        docs = {}
        for name in ("adjacent_career_mapper", "fit_scorer",
                     "stretch_detector", "application_priority"):
            docs[name] = read(os.path.join(SKILLS_DIR, name + ".txt"))
    except OSError as e:
        write_outputs("FAILED:load", "cannot read inputs: %s" % e,
                      "", None, None, notes)
        print("FAILED:load %s" % e)
        return 1
    lists, lists_note = load_lists()
    if lists_note:
        notes.append(lists_note)
    seen = load_seen()

    # stage: mapping
    plan = None
    try:
        for attempt, strict in ((1, False), (2, True)):
            ok, status, body = han_turn(
                mapping_prompt(profile, docs["adjacent_career_mapper"],
                               strict=strict))
            if not ok:
                notes.append("mapping turn attempt %d not-ok: %s"
                             % (attempt, status[:160]))
                continue
            try:
                cand = extract_json(body)
            except ValueError as e:
                notes.append("mapping parse fail attempt %d: %s"
                             % (attempt, e))
                continue
            if valid_plan(cand):
                plan = {"queries": cand["queries"]}
                break
            notes.append("mapping plan invalid attempt %d" % attempt)
    except StageError as e:
        notes.append("mapping stage error: %s" % e)
    if plan is None:
        plan = dict(FALLBACK_PLAN)
        notes.append("DEGRADED: static fallback plan used")
    plan.update({k: lists.get(k, []) for k in
                 ("exclude_companies", "flag_companies",
                  "pursued_companies")})
    plan["suppress_urls"] = suppress_list(seen)

    # stage: search
    try:
        rid, result = run_tool(plan)
    except StageError as e:
        write_outputs("FAILED:search", str(e), "", None, rid, notes)
        print("FAILED:search %s" % e)
        return 1
    counts = result.get("counts", {})
    candidates = result.get("candidates", [])
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for c in candidates:
        seen.setdefault("urls", {}).setdefault(c["url"], today)
    save_seen(seen)

    if not candidates:
        write_outputs("NO_WORTHWHILE_JOBS",
                      "search returned zero kept candidates", "",
                      counts, rid, notes)
        print("NO_WORTHWHILE_JOBS (zero kept)")
        return 0

    # stage: scoring
    ranked = sorted(candidates, key=lambda c: c.get("source_tier", 9))
    try:
        ok, status, body = han_turn(
            scoring_prompt(profile, docs, ranked[:SCORE_CAP]))
    except StageError as e:
        write_outputs("FAILED:scoring", str(e), "", counts, rid, notes)
        print("FAILED:scoring %s" % e)
        return 1
    if not ok:
        write_outputs("FAILED:scoring", "scoring turn not-ok: %s"
                      % status[:200], "", counts, rid, notes)
        print("FAILED:scoring %s" % status[:200])
        return 1
    report_text = body
    # B6: the machine-readable result must be present and complete, or the run
    # fails explicitly and visibly -- never a SUCCEEDED with a lost/truncated
    # result (the 2026-10-01 false-green). write_outputs routes any FAILED*
    # state to the status card, the statusboard/Sun digest card, and the
    # failures log.
    ok_result, worthwhile, reason = scoring_result(body)
    state = classify_run(ok_result, worthwhile)
    if state.startswith("FAILED"):
        notes.append("B6 result invalid: %s" % reason)
        write_outputs(state, "%s; scored=%d"
                      % (reason, min(len(ranked), SCORE_CAP)),
                      report_text, counts, rid, notes)
        print("%s %s" % (state, reason))
        return 1
    pk = ""
    if state == "SUCCEEDED" and os.environ.get("CAREER_PACKETS", "1") == "1":
        try:
            sys.path.insert(0, f"{_TB_BASE}/shared/lib")
            import career_packets
            tele = career_packets.run_packets(report_text, han_turn, notes,
                                               verify_urls=True)
            pk = (" packets=%(packets)d blocked=%(blocked)d "
                  "pkt_failed=%(failed)d email=%(email)s" % tele)
        except Exception as e:
            pk = " packets=STAGE-CRASHED"
            notes.append("packets stage crashed: %s" % str(e)[:200])
    path = write_outputs(state, "worthwhile=%s scored=%d%s"
                         % (worthwhile, min(len(ranked), SCORE_CAP), pk),
                         report_text, counts, rid, notes)
    print("%s report=%s" % (state, path))
    return 0


# --- selftest (offline, pure functions only) ---------------------------------

def selftest():
    global _BOARD_ENABLED
    _BOARD_ENABLED = False
    fails = 0

    def check(label, cond):
        nonlocal fails
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        fails += 0 if cond else 1

    # FC04: han_turn binds to the --json contract (ask_contract v1).
    import ask_contract as _ac
    _sv = _ac._RUNNER

    def _ht(out, rc=0, catch=True):
        _ac._RUNNER = lambda argv, inp, to, env: (rc, out, "")
        try:
            return han_turn("p")
        except StageError as e:
            if not catch:
                raise
            return "StageError", str(e), None
        finally:
            _ac._RUNNER = _sv
    _good = _ac.dumps(_ac.build("han", True, '{"queries": [{"q": "x"}]}',
                                None, 0, "han-t"))
    ok, st, body = _ht(_good)
    check("FC04 han_turn: --json success -> ok + reply",
          ok and body.startswith("{") and "turn=han-t" in st)
    seen = {}
    _ac._RUNNER = lambda argv, inp, to, env: seen.update(a=argv) or (
        0, _good, "")
    try:
        han_turn("p")
    finally:
        _ac._RUNNER = _sv
    check("FC04 han_turn passes --json", "--json" in seen.get("a", []))
    for _lbl, _out in (
            ("ok + empty reply", _ac.dumps(_ac.build("han", True, "", None,
                                                     0))),
            ("not ok", _ac.dumps(_ac.build("han", False, "", "boom", 0))),
            ("malformed JSON", "{oops"),
            ("human output", "[agent=han ok=True]\n" + SEP + "\nX\n"),
            ("diagnostics line", _good + "(diagnostics: /x.log)\n"),
            ("empty stdout", "")):
        ok2, _, b2 = _ht(_out)
        check("FC04 han_turn: %s -> FAILED turn" % _lbl,
              ok2 is False and b2 == "")
    # C3b(i): acknowledge once on machine delivery; never on failure.
    _rk = dict((k, None) for k in _ac.RECEIPT_KEYS)
    _rk["id"] = 5
    _acks = []
    _sva = _ac._ACK
    _ac._ACK = lambda a, t, i, p: _acks.append((list(i), p)) or list(i)
    try:
        _ht(_ac.dumps(_ac.build("han", True, "R", None, 0, "t", "th",
                                receipts=[_rk])))
        check("C3b han_turn acknowledges once as machine:career_hunter_daily",
              _acks == [([5], "machine:career_hunter_daily")])
        del _acks[:]
        _ht(_ac.dumps(_ac.build("han", True, " ", None, 0, "t", "th",
                                receipts=[_rk])))
        _ht("{oops")
        check("C3b han_turn: failed/unparseable turn never acknowledges",
              _acks == [])
    finally:
        _ac._ACK = _sva
    try:
        _ht(_ac.dumps(_ac.build("han", False, "", "BLOCKED", 3,
                                blocked=True)), rc=3, catch=False)
        check("FC04 han_turn: blocked rc=3 -> StageError", False)
    except StageError:
        check("FC04 han_turn: blocked rc=3 -> StageError", True)
    j = extract_json("noise ```json\n{\"queries\": [{\"q\": \"a\"}]}\n``` t")
    check("json extracted through fences", j["queries"][0]["q"] == "a")
    check("valid plan accepted", valid_plan({"queries": [{"q": "a"}]}))
    check("empty plan rejected", not valid_plan({"queries": []}))
    check("oversize plan rejected",
          not valid_plan({"queries": [{"q": "a"}] * 15}))
    check("non-str q rejected", not valid_plan({"queries": [{"q": 3}]}))
    check("fallback plan is itself valid", valid_plan(FALLBACK_PLAN))
    check("lane default derivations equal legacy literals",
          _LANE_DEFAULT.replace(" ", "_") == "CAREER_HUNTER"
          and _LANE_DEFAULT.lower() == "career hunter"
          and _LANE_DEFAULT.title() == "Career Hunter")
    check("lane env unset -> legacy names (skip if env set)",
          "CAREER_LANE" in os.environ or "CAREER_BOARD_AGENT" in os.environ
          or (LANE == "CAREER HUNTER" and LANE_SLUG == "career_hunter"))
    _base = [VENV_PY, ASK_PY, "--agent", AGENT]
    check("capsule argv absent when empty", _ask_argv("") == _base)
    check("capsule argv present when set",
          _ask_argv("bill-payer") == _base + ["--routing-capsule", "bill-payer"])
    check("mission argv present when set",
          _ask_argv("", "m-x") == _base + ["--mission", "m-x"])
    s = {"urls": {"u%d" % i: "2026-08-%02d" % (i % 28 + 1)
                  for i in range(500)}}
    check("suppress capped", len(suppress_list(s)) == SUPPRESS_CAP)
    m = re.search(r"^WORTHWHILE:\s*(\d+)",
                  "WORTHWHILE: 0\nrest", re.MULTILINE)
    check("worthwhile-0 parsed", m and m.group(1) == "0")
    import tempfile
    global CAREER_ROOT
    saved_root = CAREER_ROOT
    tmp = tempfile.mkdtemp(prefix="chd-selftest-")
    try:
        CAREER_ROOT = tmp
        day = time.strftime("%Y-%m-%d", time.gmtime())
        day_rpt = os.path.join(tmp, "reports", "%s.txt" % day)
        write_outputs("SUCCEEDED", "d", "GOOD REPORT", {"kept": 1}, "r1", [])
        write_outputs("FAILED:search", "boom", "", None, None, [])
        good_intact = read(day_rpt).find("GOOD REPORT") >= 0
        flog = os.path.join(tmp, "reports", "failures-%s.log" % day)
        check("failure does not clobber day report", good_intact)
        check("failure appended to failures log",
              os.path.exists(flog) and "FAILED:search" in read(flog))
        write_outputs("FAILED:load", "boom2", "", None, None, [])
        check("failures log append-only (both present)",
              read(flog).count("FAILED:") == 2)
    finally:
        CAREER_ROOT = saved_root
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- B6: result-first + no false-green --------------------------------
    # scoring_result(): completeness/well-formedness of the machine result.
    _res = ('WORTHWHILE: 3\nSELECTED_JSON: [{"url": "u", "company": "c", '
            '"role": "r"}]\n\nNarrative follows for the operator...')
    _ok, _w, _ = scoring_result(_res)
    check("B6 T1 result-first complete -> ok, worthwhile parsed",
          _ok is True and _w == 3)
    # T1: a very long narrative tail cut off mid-sentence at the cap; the
    # result sits in front, far from the tail, so it survives and parses.
    # (The long tail also kills a "parser reads only the tail" mutant: the
    # front result is thousands of chars from the end.)
    _cut = _res + "\n\n" + ("very long narrative sentence block. " * 400)
    _cut = _cut[:len(_res) + 4000]
    _ok, _w, _ = scoring_result(_cut)
    check("B6 T1 narrative tail truncated, front result intact -> ok",
          _ok is True and _w == 3 and len(_cut) > 3000)
    # T2: today's 2026-10-01 specimen shape -- narrative only, result missing,
    # cut off mid-sentence.
    _t2 = ("Mission selection did not resolve -- proceeding from charter.\n\n"
           "Some Role @ Some Co\nFamily: program_management | Remote\n\n"
           "stretch_detector: STRETCH approaching FICTION on one element. "
           "Program planning is core. Insurance")
    _ok, _w, _ = scoring_result(_t2)
    check("B6 T2 result missing (tail-only specimen) -> FAILED",
          _ok is False and _w == -1)
    # T3: SELECTED_JSON truncated mid-block (unterminated array).
    _t3 = ('WORTHWHILE: 2\nSELECTED_JSON: [{"url": "u", "company": "c", '
           '"role": "r"}, {"url": "u2"')
    _ok, _w, _ = scoring_result(_t3)
    check("B6 T3 SELECTED_JSON truncated mid-block -> FAILED", _ok is False)
    # T4: WORTHWHILE malformed (no integer), and SELECTED_JSON invalid JSON.
    _ok, _, _ = scoring_result("WORTHWHILE: many\nSELECTED_JSON: []")
    check("B6 T4 WORTHWHILE malformed -> FAILED", _ok is False)
    _ok, _, _ = scoring_result("WORTHWHILE: 1\nSELECTED_JSON: [not json]")
    check("B6 T4 SELECTED_JSON invalid JSON -> FAILED", _ok is False)
    # T5: complete result, zero selections -> green, not false-red.
    _ok, _w, _ = scoring_result("WORTHWHILE: 0\nSELECTED_JSON: []\n\nNone "
                                "today.")
    check("B6 T5 complete zero-selection -> ok, worthwhile 0",
          _ok is True and _w == 0)
    # T6: a complete result placed at the END is accepted (position-
    # independent), but a result at the end that is cut off FAILS.
    _t6 = ('Narrative first, model ignored the order...\n\nWORTHWHILE: 1\n'
           'SELECTED_JSON: [{"url": "u", "company": "c", "role": "r"}]')
    _ok, _w, _ = scoring_result(_t6)
    check("B6 T6 complete result at end -> accepted", _ok is True and _w == 1)
    _ok, _, _ = scoring_result("Narrative first...\n\nWORTH")
    check("B6 T6 result at end and cut off -> FAILED", _ok is False)

    # classify_run(): the state mapping -- no false-green, no false-red.
    check("B6 classify invalid result -> FAILED:scoring-result",
          classify_run(False, -1) == "FAILED:scoring-result")
    check("B6 classify complete zero -> NO_WORTHWHILE_JOBS",
          classify_run(True, 0) == "NO_WORTHWHILE_JOBS")
    check("B6 classify complete positive -> SUCCEEDED",
          classify_run(True, 5) == "SUCCEEDED")
    # mutant: a missing result must NOT be treated as success.
    _mo, _mw = scoring_result(_t2)[:2]
    check("B6 mutant-guard: missing result is not SUCCEEDED",
          classify_run(_mo, _mw).startswith("FAILED"))
    # mutant: the zero-selection case must NOT be marked failed.
    check("B6 mutant-guard: zero-selection not failed",
          not classify_run(True, 0).startswith("FAILED"))

    # Surface visibility: a FAILED:scoring-result reaches the status card, the
    # failures log, AND the statusboard / Sun digest card -- not only a note.
    import tempfile as _tf
    _tmp2 = _tf.mkdtemp(prefix="chd-b6-")
    _board = []

    class _FakeSB(object):
        @staticmethod
        def write_status(agent, status, summary, detail=None,
                         stale_after_hours=0, run_id=None):
            _board.append((agent, status, summary, run_id))
    _sv_board = _BOARD_ENABLED
    _had_sb = sys.modules.get("statusboard")
    try:
        CAREER_ROOT = _tmp2
        _BOARD_ENABLED = True
        sys.modules["statusboard"] = _FakeSB
        _day = time.strftime("%Y-%m-%d", time.gmtime())
        write_outputs("FAILED:scoring-result",
                      "WORTHWHILE line missing (result lost or truncated); "
                      "scored=40", "truncated narrative only, no result",
                      {"kept": 70}, "rX", ["B6 result invalid: missing"])
        _card = read(os.path.join(_tmp2, "status", LANE_SLUG + ".txt"))
        _flog = os.path.join(_tmp2, "reports", "failures-%s.log" % _day)
        check("B6 T2 failure on status card",
              "state=FAILED:scoring-result" in _card)
        check("B6 T2 failure in failures log",
              os.path.exists(_flog) and "FAILED:scoring-result" in read(_flog))
        check("B6 T2 failure on statusboard/digest card as 'fail'",
              bool(_board) and _board[-1][1] == "fail")
        check("BT-S5 the run id reaches the statusboard card",
              bool(_board) and _board[-1][3] == "rX")
        check("B6 T2 no success day-report written on failure",
              not os.path.exists(os.path.join(_tmp2, "reports",
                                              "%s.txt" % _day)))
    finally:
        CAREER_ROOT = saved_root
        _BOARD_ENABLED = _sv_board
        if _had_sb is not None:
            sys.modules["statusboard"] = _had_sb
        else:
            sys.modules.pop("statusboard", None)
        import shutil as _sh2
        _sh2.rmtree(_tmp2, ignore_errors=True)

    print("selftest: %d failures" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit(main())
