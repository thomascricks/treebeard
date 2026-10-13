#!/usr/bin/env python3
# -*- coding: ascii -*-
"""clark_patrol_driver.py -- Clark Patrol tick driver (P3).

ENQUEUE-ONLY by architectural ruling: discovers, hard-filters,
fit-reviews, and enqueues into the existing Clark queue. Never drains,
never digests, never emails -- the 15-min intake tick remains the sole
drainer/sender. Two discovery paths, one downstream.

Tick model (ruling: spread the day over the day): each invocation
processes PATROL_CHUNK pending employers from today's checklist, then
exits. The timer fires every 30 min; the checklist resumes across
ticks and across restarts.
"""
import json
import os
import sqlite3
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import clark_patrol
import clark_queue
import clark_fit_review
import clark_intake_driver as intake  # reuse env, serpapi, tiers, turn

PLAN_LISTS = f"{_TB_BASE}/shared/career/plan_lists.json"
LEDGER_PATH = f"{_TB_BASE}/shared/inbound/clark/ledger.sqlite"
STATUS_DIR = f"{_TB_BASE}/shared/career_clark/patrol/status"
PATROL_CHUNK = int(os.environ.get("PATROL_CHUNK", "3"))
PATROL_MAX_KEEP = int(os.environ.get("PATROL_MAX_KEEP", "50"))


def best_apply_link(job):
    """Highest-tier link from apply_options (employer > ATS > agg),
    else share_link. Spec: prefer direct employer URLs."""
    best, bt = None, -1
    for o in (job.get("apply_options") or []):
        link = o.get("link")
        if not link:
            continue
        t = intake._tier_from_url(link)
        if t > bt:
            best, bt = link, t
    return best or job.get("share_link")


def make_fetch(api_key):
    def fetch(emp, lane, query):
        jobs, err = intake.serpapi_google_jobs(query, api_key,
                                               location="United States")
        if err:
            raise RuntimeError("serpapi: %s" % err)
        out = []
        for j in jobs:
            out.append({
                "title": j.get("title", ""),
                "company": j.get("company_name", "") or emp["name"],
                "location": j.get("location", ""),
                "description": intake._job_description_text(j),
                "url": best_apply_link(j),
                "share_link": j.get("share_link"),
                "source": "serpapi",
            })
        return out
    return fetch


def make_upgrade(serper_key):
    """Post-review apply-link quality: upgrade aggregator/sludge links
    to employer/ATS via Serper (kept jobs only). Sludge links that
    can't be upgraded fall back to the Google share link; a sludge
    link with no alternative is kept but loudly noted."""
    def upgrade(cand):
        cur = cand.get("url") or ""
        tier = intake._tier_from_url(cur)
        if tier >= intake.TIER_ATS:
            return cand, None
        up = None
        if serper_key:
            up = intake.serper_employer_lookup(
                str(cand.get("company") or ""),
                str(cand.get("title") or ""), serper_key)
        if up:
            cand["url"] = up
            return cand, "upgraded to employer/ATS"
        if tier == intake.TIER_SLUDGE and cand.get("share_link"):
            cand["url"] = cand["share_link"]
            return cand, "sludge link -> share link"
        if tier == intake.TIER_SLUDGE:
            return cand, "WARNING sludge apply link kept (no alternative)"
        return cand, None
    return upgrade


def record_disposition(pdb, employer_id, url, disp):
    nurl = clark_queue.normalize_url(url or "")
    pdb.execute("UPDATE jobs SET disposition = ? "
                "WHERE employer_id = ? AND normalized_url = ?",
                (disp, employer_id, nurl))
    pdb.commit()


def run_tick(pdb, qconn, roster, excludes, fetch_fn, turn_fn, day,
             chunk=PATROL_CHUNK, dry_run=False,
             max_keep=PATROL_MAX_KEEP, upgrade_fn=None):
    """One patrol tick. Fully injectable for offline testing."""
    tele = {"day": day, "started_at": intake and time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "attempted": [], "succeeded": 0, "failed": {},
            "examined": 0, "new_found": 0, "rejected": 0,
            "dupes_suppressed": 0, "closed": 0,
            "admitted_pre_review": 0, "review_kept": 0,
            "enqueued": 0, "enqueue_deduped": 0,
            "link_unresolved": 0,
            "dry_run": bool(dry_run), "notes": []}
    clark_patrol.ensure_checklist(pdb, roster, day)
    ids = clark_patrol.next_pending(pdb, day, chunk)
    tele["attempted"] = ids
    if not ids:
        tele["notes"].append("day checklist complete; nothing pending")
        tele["checklist"] = clark_patrol.checklist_stats(pdb, day)
        return tele
    by_id = dict((e["id"], e) for e in roster)
    admitted = []
    notes = tele["notes"]
    for eid in ids:
        emp = by_id[eid]
        try:
            t = clark_patrol.patrol_employer(pdb, emp, fetch_fn,
                                             excludes, notes)
        except Exception as e:  # noqa: BLE001
            clark_patrol.mark_employer(pdb, day, eid, "FAILED",
                                       str(e)[:180])
            tele["failed"][str(eid)] = str(e)[:120]
            continue
        clark_patrol.mark_employer(
            pdb, day, eid, "DONE",
            "examined=%d new=%d changed=%d rejected=%d dupes=%d "
            "closed=%d" % (t["examined"], t["new"], t["changed"],
                           t["rejected"], t["dupes_suppressed"],
                           t["closed"]))
        tele["succeeded"] += 1
        tele["examined"] += t["examined"]
        tele["new_found"] += t["new"] + t["changed"]
        tele["rejected"] += t["rejected"]
        tele["dupes_suppressed"] += t["dupes_suppressed"]
        tele["closed"] += t["closed"]
        admitted.extend(t["admitted"])
    tele["admitted_pre_review"] = len(admitted)
    kept = []
    if admitted and dry_run:
        notes.append("DRY RUN: %d candidates would go to fit review"
                     % len(admitted))
    elif admitted:
        kept, err = clark_fit_review.review(admitted, turn_fn, notes,
                                            max_keep=max_keep)
        if err:
            notes.append("PATROL REVIEW FAILED: %s -- candidates "
                         "marked REVIEW_FAILED for requeue" % err)
            for c in admitted:
                record_disposition(pdb, c["employer_id"], c["url"],
                                   "REVIEW_FAILED")
            kept = []
        else:
            kept_urls = set(k.get("url") for k in kept)
            for c in admitted:
                if c["url"] not in kept_urls:
                    record_disposition(pdb, c["employer_id"], c["url"],
                                       "REVIEW_DROPPED")
    tele["review_kept"] = len(kept)
    for cand in kept:
        # Preserve discovery identity for patrol-memory disposition. An
        # upgraded canonical URL is intentionally different from the URL
        # stored in patrol.jobs, so dispositions must key off the original.
        source_url = cand.get("url") or ""

        if upgrade_fn is not None:
            try:
                cand, unote = upgrade_fn(cand)
                if unote:
                    notes.append("apply-link %s for %s @ %s" % (
                        unote, str(cand.get("title") or "")[:40],
                        str(cand.get("company") or "")[:30]))
            except Exception as e:  # noqa: BLE001
                notes.append("apply-link upgrade failed: %s"
                             % str(e)[:80])

        # CANONICAL-LINK GATE: Patrol discovery URLs are evidence, not
        # application authority. A kept candidate may enter Clark's queue
        # only with a verified employer/ATS-quality URL. Aggregators,
        # sludge, Google share links and unknown intermediaries fail closed.
        final_url = cand.get("url") or ""
        final_tier = intake._tier_from_url(final_url)
        if final_tier < intake.TIER_ATS:
            tele["link_unresolved"] += 1
            notes.append(
                "apply-link UNRESOLVED; candidate blocked before enqueue: "
                "%s @ %s url=%s" % (
                    str(cand.get("title") or "")[:40],
                    str(cand.get("company") or "")[:30],
                    final_url[:160]))
            record_disposition(pdb, cand["employer_id"], source_url,
                               "LINK_UNRESOLVED")
            continue

        cand.pop("share_link", None)
        prov = "patrol:%s:emp%d" % (day, cand["employer_id"])
        qid, note = clark_queue.enqueue(qconn, cand, prov)
        if qid is None:
            tele["enqueue_deduped"] += 1
            record_disposition(pdb, cand["employer_id"], source_url,
                               "QUEUE_DUPLICATE")
        else:
            tele["enqueued"] += 1
            record_disposition(pdb, cand["employer_id"], source_url,
                               "ENQUEUED")
    tele["checklist"] = clark_patrol.checklist_stats(pdb, day)
    return tele


def run(dry_run=False):
    intake._load_env()
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        sys.exit("SERPAPI_API_KEY missing from providers.env")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    roster = clark_patrol.load_roster()
    if not clark_patrol.batch_for_day(roster, day):
        print(json.dumps({"day": day, "notes": ["weekend; no patrol"]}))
        return 0
    # Dry-run is a pure preview: throwaway in-memory patrol DB so
    # discovery is never consumed (real memory + checklist untouched).
    pdb = clark_patrol.open_db(":memory:" if dry_run
                               else clark_patrol.DB_PATH)
    clark_patrol.init_schema(pdb)
    qconn = sqlite3.connect(LEDGER_PATH, timeout=5.0)
    qconn.execute("PRAGMA busy_timeout = 5000")
    clark_queue.init_schema(qconn)
    excludes = json.load(open(PLAN_LISTS)).get("exclude_companies", [])
    tele = run_tick(pdb, qconn, roster, excludes, make_fetch(key),
                    intake.clark_turn, day, dry_run=dry_run,
                    upgrade_fn=make_upgrade(
                        os.environ.get("SERPER_API_KEY")))
    os.makedirs(STATUS_DIR, exist_ok=True)
    path = os.path.join(STATUS_DIR, "patrol-%s.log" % day)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(tele, indent=2) + "\n")
    print(json.dumps({k: tele[k] for k in (
        "day", "attempted", "succeeded", "failed", "examined",
        "new_found", "rejected", "dupes_suppressed", "review_kept",
        "link_unresolved", "enqueued")}, indent=2))
    return 0 if not tele["failed"] else 1


def _selftest():
    import re as _re
    fails = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    # The review step reads the operator's career profile -- private, never
    # shipped or installed -- and marks every candidate REVIEW_FAILED without
    # it. review() binds its default path at IMPORT, so the redirect is: set
    # CAREER_PROFILE to a synthetic profile, reload the reviewer, restore both
    # at exit. No operator data is read by this suite (CR-2, 2026-08-28).
    import atexit as _ax, importlib as _il, tempfile as _tf
    _pfd, _pfp = _tf.mkstemp(prefix="career-profile-", suffix=".txt"); os.close(_pfd)
    with open(_pfp, "w", encoding="utf-8") as _fh:
        _fh.write("Fixture profile: senior product manager, Boston MA, remote OK. "
                  "Synthetic; contains no operator data.\n")
    _prev_profile = os.environ.get("CAREER_PROFILE")
    os.environ["CAREER_PROFILE"] = _pfp
    _il.reload(clark_fit_review)

    def _restore_profile():
        if _prev_profile is None:
            os.environ.pop("CAREER_PROFILE", None)
        else:
            os.environ["CAREER_PROFILE"] = _prev_profile
        _il.reload(clark_fit_review)
        try:
            os.unlink(_pfp)
        except OSError:
            pass
    _ax.register(_restore_profile)

    # Synthetic roster: run_tick keys employers by id only, so the contract is
    # host-independent. The live roster is private operator state (CR-2).
    roster = clark_patrol._fixture_roster()
    excludes = ["Oracle", "Flock Safety"]

    def stub_turn(prompt):
        urls = _re.findall(r"^url: (.+)$", prompt, _re.M)
        body = json.dumps([{"url": u, "keep": True, "fit": "GOOD",
                            "comp": "UNKNOWN", "why": "fixture"}
                           for u in urls])
        return True, "[stub ok=True]", body

    def J(u, t, l="Boston, MA", d="Role. $150,000 - $170,000.", c=None):
        return {"title": t, "company": c or "X", "location": l,
                "description": d, "url": u, "source": "fixture"}

    def newdbs():
        p = clark_patrol.open_db(":memory:")
        clark_patrol.init_schema(p)
        q = sqlite3.connect(":memory:")
        q.execute("PRAGMA busy_timeout = 5000")
        clark_queue.init_schema(q)
        return p, q

    # d01 clark fixture e2e -> queue (Friday: ids 161-163 in chunk 3)
    pdb, q = newdbs()
    fx = {163: [J("https://careers.sharkninja.com/job/needham/pm/9",
                  "Senior Product Manager", c="SharkNinja")]}
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: fx.get(e["id"], []), stub_turn,
                 "2026-08-28", chunk=3)
    row = q.execute("SELECT provenance, payload_json FROM queue").fetchone()
    check("d01 clark fixture enqueued with PATROL origin",
          t["enqueued"] == 1 and row and
          row[0] == "patrol:2026-08-28:emp163" and
          '"origin_class": "PATROL"' in row[1])

    # d02 han-title fixture (spec Test 6 e2e)
    pdb, q = newdbs()
    fx = {81: [J("https://careers.mit.edu/job/rpm-ai-policy",
                 "Research Program Manager - AI Policy",
                 l="Cambridge, MA", d="Policy program.", c="MIT")]}
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: fx.get(e["id"], []), stub_turn,
                 "2026-08-26", chunk=1)
    check("d02 Han title discovered and enqueued (Test 6)",
          t["enqueued"] == 1)

    # d03 remote-national pass + relocation reject (spec Test 5 e2e)
    pdb, q = newdbs()
    fx = {1: [J("https://jobs.lever.co/fixtureco/j1", "Product Manager", l="Remote - US"),
              J("https://jobs.lever.co/fixtureco/j2", "Product Manager",
                l="San Francisco, CA",
                d="Strong PM role. Relocation required. $180,000.")]}
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: fx.get(e["id"], []), stub_turn,
                 "2026-08-24", chunk=1)
    check("d03 remote passes, relocation rejected (Test 5)",
          t["enqueued"] == 1 and t["rejected"] == 1)

    # d04 week-over-week duplicate never re-enqueued (Test 3 e2e)
    pdb, q = newdbs()
    fx163 = [J("https://careers.sharkninja.com/job/needham/pm/9",
               "Senior Product Manager", c="SharkNinja")]
    run_tick(pdb, q, roster, excludes, lambda e, ln, qq:
             fx163 if e["id"] == 163 else [], stub_turn,
             "2026-08-28", chunk=3)
    t2 = run_tick(pdb, q, roster, excludes, lambda e, ln, qq:
                  fx163 if e["id"] == 163 else [], stub_turn,
                  "2026-09-04", chunk=3)
    n = q.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    check("d04 duplicate suppressed across weeks (Test 3)",
          t2["dupes_suppressed"] == 1 and t2["enqueued"] == 0 and n == 1)

    # d05 employer failure isolated, others proceed (Test 8 e2e)
    pdb, q = newdbs()

    def flaky(e, ln, qq):
        if e["id"] == 161:
            raise RuntimeError("markup changed")
        return fx163 if e["id"] == 163 else []
    t = run_tick(pdb, q, roster, excludes, flaky, stub_turn,
                 "2026-08-28", chunk=3)
    st = clark_patrol.checklist_stats(pdb, "2026-08-28")
    check("d05 failure isolated: 1 FAILED, others DONE, job enqueued",
          st["FAILED"] == 1 and st["DONE"] == 2 and t["enqueued"] == 1)

    # d06 nonstandard title passes gates to review (Test 7 pipeline)
    pdb, q = newdbs()
    fx = {1: [J("https://jobs.lever.co/fixtureco/j7", "Institutional Coordination Lead",
                d="Governance, technical translation, cross-functional "
                  "program leadership. $155,000.")]}
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: fx.get(e["id"], []), stub_turn,
                 "2026-08-24", chunk=1)
    check("d06 wildcard-style title reaches review and enqueues "
          "(Test 7)", t["enqueued"] == 1)

    # d07 dry_run: no review turn, no enqueue
    pdb, q = newdbs()
    calls = {"n": 0}

    def counting_turn(p):
        calls["n"] += 1
        return stub_turn(p)
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: fx163 if e["id"] == 163 else [],
                 counting_turn, "2026-08-28", chunk=3, dry_run=True)
    n = q.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    check("d07 dry_run makes no turn and no enqueue",
          calls["n"] == 0 and n == 0 and t["admitted_pre_review"] == 1)

    # d08 no arbitrary cap: 7 kept -> 7 enqueued (spec req 9)
    pdb, q = newdbs()
    seven = [J("https://jobs.lever.co/fixtureco/j%d" % i, "Product Manager %d" % i)
             for i in range(7)]
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: seven if e["id"] == 1 else [],
                 stub_turn, "2026-08-24", chunk=1, max_keep=50)
    check("d08 seven legitimate jobs all enqueued (req 9)",
          t["enqueued"] == 7)

    pdb, q = newdbs()
    slj = [dict(J("https://www.jooble.org/jdp/999", "Product Manager X"),
                share_link="https://share.google/abc")]

    def upg(c):
        if "jooble" in (c.get("url") or ""):
            c["url"] = c.get("share_link") or c["url"]
            return c, "sludge link -> share link"
        return c, None
    t = run_tick(pdb, q, roster, excludes,
                 lambda e, ln, qq: slj if e["id"] == 1 else [],
                 stub_turn, "2026-08-24", chunk=1, upgrade_fn=upg)
    row = q.execute("SELECT url, payload_json FROM queue").fetchone()
    disp = q.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    check("d09 unresolved intermediary/share link blocked before enqueue",
          t["enqueued"] == 0 and t["link_unresolved"] == 1
          and row is None and disp == 0)

    # Regression for 2026-08-24 production defect: an unknown aggregator
    # using a /jobs path must not masquerade as an employer site.
    check("d10 unknown /jobs intermediary is below ATS tier",
          intake._tier_from_url(
              "https://www.refolk.ai/jobs/example-company/example-role"
          ) < intake.TIER_ATS)

    print("%d/10 driver selftests passed" % (10 - len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        _selftest()
    sys.exit(run(dry_run="--dry-run" in sys.argv[1:]))
