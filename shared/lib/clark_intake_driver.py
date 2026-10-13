#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
clark_intake_driver.py -- Clark's forwarded-job-email intake driver.

Runs one pass per invocation (systemd timer, every 15 min):
  1. Poll Resend inbound (via agent_inbound.poll)
  2. For each RECEIVED ledger row: extract candidates from raw email
  3. For each candidate: dispatch by URL class
       - SLUDGE -> drop with reason
       - EMPLOYER_INDEX/JOB, ATS_JOB/INDEX, AGGREGATOR_JOB,
         AGGREGATOR_INDEX, SHARE_GOOGLE, TEXT_ONLY_QUERY:
             SerpApi google_jobs(query) -> individual job list
             then Serper /search per job -> employer/ATS URL
       - UNKNOWN, rubbish (unreachable), no-jobs-found:
             surfaced in the output email as "Julia sent, couldn't
             process" so operator can decide
  4. Filter surviving jobs against Clark's applications.json AND the
     shared career/applications.json (READ-ONLY on shared)
  5. Build SELECTED_JSON report body compatible with
     career_packets.parse_selected
  6. Reroute CAREER_ROOT + CAREER_LOCKBOX to Clark's tree
     (/opt/treebeard/shared/career_clark)
  7. Call career_packets.run_packets(report_body, clark_turn_fn, notes)
     -- generates tailored resumes + PDFs + lockbox + one email
  8. Mark each processed resend_id as PROCESSED with packet count,
     or FAILED with reason

Silent when: nothing to process, or nothing survives filtering. This
is by operator ruling ("if I dont send clark something clark doesnt
act") -- no email if nothing changes.
"""
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

# --- reroute career_packets state to Clark's tree BEFORE import ---
os.environ["CAREER_ROOT"] = os.environ.get(
    "CAREER_ROOT_CLARK", f"{_TB_BASE}/shared/career_clark")
os.environ["CAREER_LOCKBOX"] = os.environ.get(
    "CAREER_LOCKBOX_CLARK",
    f"{_TB_BASE}/shared/career_clark/job_lockbox")

sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import agent_inbound
import ask_contract
import clark_forward_extractor
import career_packets
import clark_queue
import clark_fit_review
import sqlite3

CLARK_ROOT = f"{_TB_BASE}/shared/career_clark"
SHARED_APPS = f"{_TB_BASE}/shared/career/applications.json"
CLARK_APPS = os.path.join(CLARK_ROOT, "applications.json")
LEDGER_PATH = f"{_TB_BASE}/shared/inbound/clark/ledger.sqlite"

SERPAPI_URL = "https://serpapi.com/search"
SERPER_URL = "https://google.serper.dev/search"
UA = "treebeard-clark-intake/1"
HTTP_TIMEOUT = 30

# Source tiers for filtering (higher = better)
TIER_EMPLOYER = 4
TIER_ATS = 3
TIER_AGGREGATOR = 2
TIER_UNKNOWN = 1
TIER_SLUDGE = 0

ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com",
    "smartrecruiters.com", "myworkdayjobs.com", "icims.com",
    "bamboohr.com", "jobvite.com", "recruitee.com", "personio.com",
    "teamtailor.com", "breezy.hr",
)
AGGREGATOR_DOMAINS = (
    "linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com",
    "builtin.com", "monster.com", "dice.com", "wellfound.com",
    "angel.co",
)
SLUDGE_DOMAINS = (
    "jobilize.com", "jobzmall.com", "bebee.com", "jobleads.com",
    "simplify.jobs", "lensa.com", "jooble.org", "adzuna.com",
    "careerbuilder.com", "snagajob.com", "learn4good.com",
    "pursuitci.com", "jobsora.com", "talent.com",
)

VENV_PY = f"{_TB_BASE}/shared/venv/bin/python3"
ASK_PY = f"{_TB_BASE}/treebeard/ask.py"
CLARK_ASK_TIMEOUT = 420


def _load_env():
    """Load providers.env into os.environ. Runs as root so file is
    readable. Idempotent."""
    path = f"{_TB_ETC}/credentials/providers.env"
    try:
        fh = open(path)
    except (PermissionError, FileNotFoundError):
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def _open_ledger():
    """Open ledger.sqlite with a sane busy_timeout for concurrent safety."""
    conn = sqlite3.connect(LEDGER_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _tier_from_url(url):
    """Classify a URL into a source tier for filtering."""
    if not url:
        return TIER_UNKNOWN
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return TIER_UNKNOWN
    if not host:
        return TIER_UNKNOWN
    for s in SLUDGE_DOMAINS:
        if host == s or host.endswith("." + s):
            return TIER_SLUDGE
    for a in ATS_DOMAINS:
        if host == a or host.endswith("." + a):
            return TIER_ATS
    for a in AGGREGATOR_DOMAINS:
        if host == a or host.endswith("." + a):
            return TIER_AGGREGATOR
    # Employer heuristic must be HOST-based, not path-based.
    # Any aggregator can expose /jobs/... or /careers/...; treating the
    # path alone as employer authority let intermediary links (e.g. Refolk)
    # bypass canonical employer/ATS resolution in Patrol.
    if host.startswith("careers.") or host.startswith("jobs."):
        return TIER_EMPLOYER
    return TIER_UNKNOWN


def _post_json(url, headers, payload):
    """POST JSON, return parsed response or raise."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _get_json(url):
    """GET URL, return parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def serpapi_google_jobs(query, api_key, location=None):
    """Query SerpApi's google_jobs engine, optionally geo-constrained
    via the engine's location parameter. If a located call fails,
    retries once without location (location support cannot be assumed
    for every account/param combination). Same return contract:
    (job list, err) with [] on error."""
    def _call(loc):
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
        }
        if loc:
            params["location"] = loc
        url = SERPAPI_URL + "?" + urllib.parse.urlencode(params)
        try:
            d = _get_json(url)
        except Exception as e:
            return None, "serpapi error: %s" % str(e)[:200]
        if d.get("search_metadata", {}).get("status") != "Success":
            return None, "serpapi status: %s" % d.get(
                "search_metadata", {}).get("status", "unknown")
        return d.get("jobs_results", []) or [], None
    jobs, err = _call(location)
    if err is not None and location:
        jobs, err = _call(None)
    return (jobs if jobs is not None else []), err


def serper_employer_lookup(company, title, api_key):
    """Serper /search for '<company> careers <title>'. Returns the
    highest-tier URL found in top 5 organic results, or None."""
    query = "%s careers %s" % (company, title)
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    try:
        d = _post_json(SERPER_URL, headers,
                       {"q": query, "num": 5})
    except Exception:
        return None
    best = None
    best_tier = -1
    for item in (d.get("organic") or [])[:5]:
        link = item.get("link", "")
        if not link:
            continue
        tier = _tier_from_url(link)
        if tier > best_tier and tier >= TIER_ATS:
            # Only accept employer or ATS results for enrichment
            best = link
            best_tier = tier
    return best


def _load_seen_urls(conn=None):
    """Union of Clark's and shared applications.json URLs, plus queue URLs
    if a ledger connection is supplied."""
    seen = set()
    for path in (CLARK_APPS, SHARED_APPS):
        try:
            d = json.load(open(path))
            if isinstance(d, dict):
                seen.update(d.keys())
        except (OSError, ValueError):
            pass
    if conn is not None:
        try:
            seen.update(clark_queue.seen_urls_in_queue(conn))
        except Exception:
            pass  # queue lookup failure must not block processing
    return seen


def _pick_apply_link(job):
    """Return the best apply URL from a SerpApi job dict, prefer
    apply_options[0].link, fall back to share_link."""
    opts = job.get("apply_options") or []
    if opts:
        for opt in opts:
            link = opt.get("link")
            if link:
                return link, opt.get("title", "apply")
    return job.get("share_link"), "view"


def _enrich_and_filter(jobs, seen_urls, serper_key, notes):
    """For each SerpApi job: pick apply link, try to find employer URL
    via Serper. Filter sludge and already-seen. Return list of dicts
    matching career_packets.parse_selected input shape."""
    selected = []
    for job in jobs:
        title = job.get("title", "").strip()
        company = job.get("company_name", "").strip()
        if not title or not company:
            continue
        apply_url, apply_label = _pick_apply_link(job)
        if not apply_url:
            continue
        # Try to upgrade to an employer or ATS URL
        upgraded = serper_employer_lookup(company, title, serper_key)
        final_url = upgraded or apply_url
        tier = _tier_from_url(final_url)
        if tier == TIER_SLUDGE:
            notes.append("dropped %s @ %s: sludge source (%s)"
                         % (title[:40], company, final_url))
            continue
        if final_url in seen_urls:
            notes.append("dropped %s @ %s: already packeted"
                         % (title[:40], company))
            continue
        # Compose SELECTED_JSON entry (matches parse_selected shape)
        selected.append({
            "url": final_url,
            "title": title,
            "company": company,
            "location": job.get("location", ""),
            "description": _job_description_text(job),
            "family": "forwarded_by_family",
            "source_tier": tier,
        })
        seen_urls.add(final_url)
    return selected


def _job_description_text(job):
    """Concatenate SerpApi job_highlights into a single description
    string for career_packets tailoring."""
    parts = []
    desc = job.get("description")
    if desc:
        parts.append(desc)
    for section in (job.get("job_highlights") or []):
        title = section.get("title", "")
        items = section.get("items") or []
        if items:
            parts.append("%s: %s" % (title, "; ".join(items)))
    return "\n".join(parts).strip()


def clark_turn(prompt):
    """Governed Clark turn via ask.py. Returns (ok, status, body).
    Same shape as career_hunter_daily.han_turn -- required by
    career_packets.run_packets contract.

    FC04 (2026-10-01): bound to ask.py's machine contract (--json, v1),
    never to its human output. A contract violation, ok=false, a blocked
    assignment or an empty reply is a FAILED turn (False, reason, ""):
    the fit review then marks the message FAILED and drained queue rows
    stay QUEUED -- never an empty success."""
    r = ask_contract.run([VENV_PY, ASK_PY, "--agent", "clark"],
                         input=prompt, timeout=CLARK_ASK_TIMEOUT)
    if r.timed_out:
        return False, "clark turn timed out", ""
    if r.returncode not in (None, 0):
        return False, ("ask.py rc=%d: %s %s"
                       % (r.returncode, r.error[:160],
                          (r.stderr or "")[-200:])), ""
    # C3b(i): receipts acknowledged on machine delivery, once, recorded
    # (no-op for a failed turn).
    r.acknowledge("machine:clark_intake_driver")
    return r.triple()


def process_one_message(resend_id, secrets, notes, conn=None):
    """Extract candidates from one RECEIVED message, run each through
    SerpApi + Serper, return list of selected job dicts."""
    try:
        extraction = clark_forward_extractor.extract(resend_id)
    except FileNotFoundError as e:
        return None, "extract: %s" % e
    if not extraction.get("ok"):
        return [], extraction.get("reason", "extraction failed")
    seen = _load_seen_urls(conn)
    all_selected = []
    for cand in extraction["candidates"]:
        cls = cand["class"]
        query = cand.get("query")
        if cls == "SLUDGE":
            notes.append("candidate SLUDGE: dropped %s"
                         % (cand.get("url", ""))[:100])
            continue
        if not query:
            notes.append("candidate %s: no query, url=%s"
                         % (cls, cand.get("url", "")[:80]))
            continue
        jobs, err = serpapi_google_jobs(query,
                                        secrets["SERPAPI_API_KEY"],
                                        location=cand.get("location"))
        if err:
            notes.append("serpapi failed for %s: %s" % (query[:60], err))
            continue
        if not jobs:
            notes.append("no jobs for query: %s (from url=%s)"
                         % (query[:60], (cand.get("url") or "")[:80]))
            continue
        selected = _enrich_and_filter(jobs, seen,
                                      secrets["SERPER_API_KEY"], notes)
        selected = clark_fit_review.tag_origin(selected, cand, notes)
        all_selected.extend(selected)
    return all_selected, None


def build_report_body(selected):
    """career_packets.parse_selected expects a 'SELECTED_JSON: [...]'
    block in the report body."""
    return "SELECTED_JSON: " + json.dumps(selected)


def run(dry_run=False):
    """One driver invocation."""
    _load_env()
    conn = _open_ledger()
    clark_queue.init_schema(conn)
    expired_count = clark_queue.expire_old(conn, days=14)
    secrets = {
        "RESEND_INBOUND_API_KEY": os.environ["RESEND_INBOUND_API_KEY"],
        "SERPAPI_API_KEY": os.environ["SERPAPI_API_KEY"],
        "SERPER_API_KEY": os.environ["SERPER_API_KEY"],
    }
    telemetry = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "polled": 0,
        "processed_ids": [],
        "failed_ids": [],
        "packets_generated": 0,
        "email_sent": False,
        "notes": [],
    }
    # 1. Poll inbound
    poll_result = agent_inbound.poll(
        "clark", api_key=secrets["RESEND_INBOUND_API_KEY"])
    telemetry["polled"] = poll_result.new
    if poll_result.transport_error:
        telemetry["notes"].append("poll transport error: %s"
                                  % poll_result.transport_error)
    # 2. Union of newly-received + prior-RECEIVED (crash recovery)
    pending_ids = list(poll_result.received)
    for row in agent_inbound.list_pending("clark"):
        if row["resend_id"] not in pending_ids:
            pending_ids.append(row["resend_id"])
    if not pending_ids:
        telemetry["notes"].append("no pending messages")
    # 3. Process each
    all_selected = []
    for rid in pending_ids:
        notes = []
        try:
            selected, err = process_one_message(rid, secrets, notes, conn=conn)
        except Exception as e:  # noqa: BLE001
            agent_inbound.mark("clark", rid, "FAILED",
                               reason="driver crash: %s" % str(e)[:200])
            telemetry["failed_ids"].append(rid)
            telemetry["notes"].extend(notes)
            telemetry["notes"].append("CRASH on %s: %s\n%s"
                                      % (rid, e, traceback.format_exc()[:400]))
            continue
        telemetry["notes"].extend(notes)
        if err:
            # Extraction failed -- NOT_JOB_RELATED or other
            state = ("NOT_JOB_RELATED"
                     if "no URLs" in err or "no extractable" in err
                     else "FAILED")
            agent_inbound.mark("clark", rid, state, reason=err)
            (telemetry["processed_ids"] if state != "FAILED"
             else telemetry["failed_ids"]).append(rid)
            continue
        if selected:
            rev_notes = []
            selected, rev_err = clark_fit_review.review(
                selected, clark_turn, rev_notes)
            telemetry["notes"].extend(rev_notes)
            if rev_err:
                agent_inbound.mark("clark", rid, "FAILED",
                                   reason="fit review: %s" % rev_err[:180])
                telemetry["failed_ids"].append(rid)
                continue
        if selected:
            for cand in selected:
                qid, note = clark_queue.enqueue(conn, cand, rid)
                if qid is None:
                    telemetry["notes"].append(
                        "enqueue skip %s: %s" % (rid[:8], note[:80]))
            all_selected.extend(selected)
            telemetry["notes"].append("resend_id %s -> %d selected"
                                      % (rid[:8], len(selected)))
        # Mark PROCESSED regardless of whether jobs survived; the message
        # was handled. Packet count tracked separately.
        agent_inbound.mark("clark", rid, "PROCESSED",
                           reason="%d selected" % len(selected or []))
        telemetry["processed_ids"].append(rid)

    # 4. All new selections enqueued; drain up to 5 for this tick's digest
    telemetry["enqueued_from_new"] = len(all_selected)
    telemetry["expired_this_tick"] = expired_count
    drained = clark_queue.drain(conn, limit=5)
    telemetry["drained_count"] = len(drained)
    if not drained:
        telemetry["notes"].append("queue empty after enqueue; no digest")
        telemetry["queue_stats"] = clark_queue.queue_stats(conn)
        return telemetry
    telemetry["packets_generated"] = len(drained)
    if dry_run:
        telemetry["notes"].append("DRY RUN: would send digest of %d packets"
                                  % len(drained))
        telemetry["dry_run_drained"] = [
            {"queue_id": r["queue_id"], "url": r["url"],
             "provenance": r["provenance"]} for r in drained]
        telemetry["queue_stats"] = clark_queue.queue_stats(conn)
        return telemetry

    # 5. Build report body from drained payloads, hand to career_packets
    drained_payloads = []
    for r in drained:
        p = dict(r["payload"])
        if not p.get("role"):
            p["role"] = p.get("title", "")
        drained_payloads.append(p)
    report_body = build_report_body(drained_payloads)
    packet_notes = []
    try:
        pkt_tele = career_packets.run_packets(
            report_body, clark_turn, packet_notes, send=True)
    except Exception as e:  # noqa: BLE001
        telemetry["notes"].append("career_packets crash: %s\n%s"
                                  % (e, traceback.format_exc()[:400]))
        telemetry["queue_stats"] = clark_queue.queue_stats(conn)
        return telemetry
    telemetry["packet_telemetry"] = pkt_tele
    telemetry["notes"].extend(packet_notes)
    telemetry["email_sent"] = bool(pkt_tele.get("email", "").startswith(
        "sent"))

    # 6. Reconcile app_mem state back to queue state (transactional SENT)
    #    SENT_TO_OPERATOR -> mark_sent (packet was in successfully-sent digest)
    #    GENERATION_BLOCKED -> mark_failed with violations (permanent)
    #    otherwise -> leave QUEUED for next tick to retry
    reconciled = {"SENT": 0, "FAILED": 0, "STAYED_QUEUED": 0}
    try:
        mem = career_packets.load_app_mem()
        for row in drained:
            url = row["payload"].get("url")
            if not url:
                reconciled["STAYED_QUEUED"] += 1
                continue
            rec = mem.get(url, {})
            state = rec.get("state")
            if state == "SENT_TO_OPERATOR":
                clark_queue.mark_sent(conn, row["queue_id"], "digest sent")
                reconciled["SENT"] += 1
            elif state == "GENERATION_BLOCKED":
                violations = rec.get("violations", "unspecified")
                clark_queue.mark_failed(
                    conn, row["queue_id"],
                    "factuality: %s" % str(violations)[:200])
                reconciled["FAILED"] += 1
            else:
                # PACKET_READY (email failed) or SELECTED (tailor crash)
                # or no state change: leave QUEUED for retry
                reconciled["STAYED_QUEUED"] += 1
    except Exception as e:  # noqa: BLE001
        telemetry["notes"].append("reconcile crash: %s" % str(e)[:200])
    telemetry["reconciled"] = reconciled
    telemetry["queue_stats"] = clark_queue.queue_stats(conn)
    return telemetry


def _write_telemetry(telemetry):
    """Write a status card to /opt/treebeard/shared/career_clark/status."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    status_dir = os.path.join(CLARK_ROOT, "status")
    os.makedirs(status_dir, exist_ok=True)
    path = os.path.join(status_dir, "clark-intake-%s.log" % day)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(telemetry, indent=2) + "\n")


# BOARD-TRUTH S4 (2026-10-01): every run ends with ONE truthful card on the
# generic statusboard (shared/status/clark_intake.json), so Treebeard's
# box.status can see this driver's outcome. Before S4 it was journal-only.
# The state comes from the run's own telemetry, nothing else:
#   fail = the run crashed, or any message FAILED (the exit-1 condition);
#   warn = packets drained but no digest email, or a poll transport error;
#   ok   = otherwise.
STATUS_CARD = "clark_intake"
STATUS_STALE_H = 1        # timer fires every 15 min: 4 missed runs = stale


def _final_status(result, exc=None, dry_run=False):
    """(status, summary) for this run's card."""
    if exc is not None:
        return "fail", "clark intake: CRASHED: %s: %s" % (
            type(exc).__name__, str(exc)[:160])
    counts = ("polled=%d processed=%d failed=%d packets=%d email_sent=%s"
              % (result["polled"], len(result["processed_ids"]),
                 len(result["failed_ids"]), result["packets_generated"],
                 result["email_sent"]))
    pre = "clark intake%s: " % (" DRY RUN" if dry_run else "")
    if result["failed_ids"]:
        return "fail", pre + "FAILED message(s) (%s)" % counts
    if result["packets_generated"] and not result["email_sent"] \
            and not dry_run:
        return "warn", pre + "packets drained, digest NOT emailed (%s)" % counts
    if any(str(n).startswith("poll transport error")
           for n in result["notes"]):
        return "warn", pre + "poll transport error (%s)" % counts
    return "ok", pre + "completed (%s)" % counts


def _status_write(status, summary, started_utc):
    """Fail-soft: a board failure must not change the run it reports."""
    try:
        import statusboard
        statusboard.write_status(
            STATUS_CARD, status, summary, stale_after_hours=STATUS_STALE_H,
            run_id=statusboard.make_run_id(STATUS_CARD, started_utc))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("WARN statusboard write failed: %s\n" % e)


def main(argv):
    dry = "--dry-run" in argv
    # A run that crashes before its telemetry exists still gets a card;
    # its run id is then this fallback stamp.
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        result = run(dry_run=dry)
        _write_telemetry(result)
    except Exception as e:  # noqa: BLE001
        _status_write(*_final_status(None, e), started_utc=started)
        raise
    _status_write(*_final_status(result, dry_run=dry),
                  started_utc=result.get("started_at") or started)
    print(json.dumps({
        "polled": result["polled"],
        "processed": len(result["processed_ids"]),
        "failed": len(result["failed_ids"]),
        "packets_generated": result["packets_generated"],
        "email_sent": result["email_sent"],
        "notes_count": len(result["notes"]),
    }, indent=2))
    return 0 if not result["failed_ids"] else 1


def selftest():
    """Offline: clark_turn against ask_contract's runner seam only. Never
    calls run(), polls, or opens the ledger. Explicit failure counter."""
    fails = 0
    n = 0

    def check(label, cond):
        nonlocal fails, n
        n += 1
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        fails += 0 if cond else 1

    sv = ask_contract._RUNNER

    def ct(out, rc=0):
        ask_contract._RUNNER = lambda argv, inp, to, env: (rc, out, "")
        try:
            return clark_turn("p")
        finally:
            ask_contract._RUNNER = sv
    b = ask_contract.build
    d = ask_contract.dumps
    good = d(b("clark", True, "DECISIONS", None, 0, "clark-t"))
    ok, st, body = ct(good)
    check("FC04 clark_turn: --json success -> ok + reply",
          ok is True and body == "DECISIONS" and "turn=clark-t" in st)
    seen = {}
    ask_contract._RUNNER = lambda argv, inp, to, env: seen.update(
        a=argv, i=inp) or (0, good, "")
    try:
        clark_turn("PROMPT")
    finally:
        ask_contract._RUNNER = sv
    check("FC04 clark_turn passes --json and the prompt on stdin",
          "--json" in seen.get("a", []) and seen.get("i") == "PROMPT")
    sep = "-" * 60
    for lbl, out, rc in (
            ("ok + empty reply", d(b("clark", True, "", None, 0)), 0),
            ("ok + whitespace reply", d(b("clark", True, "  \n", None, 0)),
             0),
            ("not ok", d(b("clark", False, "", "no route", 0)), 0),
            ("blocked rc=3", d(b("clark", False, "", "BLOCKED", 3,
                                 blocked=True)), 3),
            ("crash rc=1", d(b("clark", False, "", "boom", 1)), 1),
            ("malformed JSON", "{oops", 0),
            ("regfix2 human output", "ANSWER\n(diagnostics: /x.log)\n", 0),
            ("old human output", "[agent=clark ok=True]\n" + sep
             + "\nX\n" + sep + "\nNARRATION\n", 0),
            ("noise + object", "WARN x\n" + good, 0),
            ("empty stdout", "", 0)):
        ok2, st2, b2 = ct(out, rc)
        check("FC04 clark_turn: %s -> FAILED turn" % lbl,
              ok2 is False and b2 == "" and st2)
    # C3b(i): acknowledge once on machine delivery; never on failure.
    rk = dict((k, None) for k in ask_contract.RECEIPT_KEYS)
    rk["id"] = 9
    acks = []
    sva = ask_contract._ACK
    ask_contract._ACK = lambda a, t, i, p: acks.append((list(i), p)) or list(i)
    try:
        ct(d(b("clark", True, "R", None, 0, "t", "th", receipts=[rk])))
        check("C3b clark_turn acknowledges once as machine:clark_intake_driver",
              acks == [([9], "machine:clark_intake_driver")])
        del acks[:]
        ct(d(b("clark", True, "", None, 0, "t", "th", receipts=[rk])))
        ct("{oops")
        check("C3b clark_turn: failed/unparseable turn never acknowledges",
              acks == [])
    finally:
        ask_contract._ACK = sva
    # The consumer that turned an empty success into data loss: the fit
    # review must refuse a failed turn (caller then marks FAILED).
    ask_contract._RUNNER = lambda argv, inp, to, env: (
        0, d(b("clark", True, "", None, 0)), "")
    try:
        kept, err = clark_fit_review.review(
            [{"url": "https://jobs.example.com/1", "title": "t",
              "company": "c", "description": "d"}], clark_turn, [])
    except Exception as e:  # noqa: BLE001
        kept, err = "raised", str(e)
    finally:
        ask_contract._RUNNER = sv
    check("FC04 fit review refuses an empty-reply turn (no empty success)",
          kept is None and bool(err))

    # --- BOARD-TRUTH S4: the run's final card (run() and the telemetry
    # writer are replaced; a fake statusboard records the call) ---------
    global run, _write_telemetry
    cards = []

    class _FakeSB(object):
        @staticmethod
        def write_status(agent, status, summary, detail=None,
                         stale_after_hours=None, run_id=None):
            cards.append((agent, status, summary, stale_after_hours, run_id))

        @staticmethod
        def make_run_id(prefix, started_utc=None):
            return "%s-%s" % (prefix, started_utc.replace("-", "")
                              .replace(":", ""))

    def _tele(**over):
        t = {"started_at": "2026-10-01T20:15:00Z", "polled": 1,
             "processed_ids": ["a"], "failed_ids": [], "packets_generated": 0,
             "email_sent": False, "notes": []}
        t.update(over)
        return t
    sv_run, sv_tw = run, _write_telemetry
    sv_sb = sys.modules.get("statusboard")
    sys.modules["statusboard"] = _FakeSB
    _write_telemetry = lambda t: None
    try:
        import io
        import contextlib
        for label, tele, want_state, want_rc in (
                ("success", _tele(), "ok", 0),
                ("failed message", _tele(failed_ids=["b"]), "fail", 1),
                ("drained, not emailed", _tele(packets_generated=2), "warn",
                 0),
                ("poll transport error",
                 _tele(notes=["poll transport error: timeout"]), "warn", 0)):
            del cards[:]
            run = lambda dry_run=False, _t=tele: _t
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main([])
            check("S4 %s -> one %s card, exit %d unchanged"
                  % (label, want_state, want_rc),
                  len(cards) == 1 and cards[0][1] == want_state
                  and rc == want_rc)
            check("S4 %s -> card id clark_intake, 1h window, run_id = start"
                  % label,
                  cards and cards[0][0] == "clark_intake"
                  and cards[0][3] == 1
                  and cards[0][4] == "clark_intake-20261001T201500Z")
        check("S4 failed run summary says FAILED with counts",
              "FAILED" in _final_status(_tele(failed_ids=["b"]))[1]
              and "failed=1" in _final_status(_tele(failed_ids=["b"]))[1])
        check("S4 dry run is labelled and not a warn for no email",
              _final_status(_tele(packets_generated=2), dry_run=True)
              == ("ok", "clark intake DRY RUN: completed (polled=1 "
                  "processed=1 failed=0 packets=2 email_sent=False)"))
        del cards[:]

        def _boom(dry_run=False):
            raise KeyError("SERPAPI_API_KEY")
        run = _boom
        raised = False
        try:
            main([])
        except KeyError:
            raised = True
        check("S4 crash -> exception still propagates (exit unchanged)",
              raised)
        check("S4 crash -> a fail card naming the crash",
              len(cards) == 1 and cards[0][1] == "fail"
              and cards[0][2].startswith("clark intake: CRASHED: KeyError")
              and cards[0][4].startswith("clark_intake-"))
    finally:
        run, _write_telemetry = sv_run, sv_tw
        if sv_sb is not None:
            sys.modules["statusboard"] = sv_sb
        else:
            sys.modules.pop("statusboard", None)
    print("%d/%d passed" % (n - fails, n))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit(main(sys.argv[1:]))