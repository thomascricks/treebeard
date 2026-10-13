#!/usr/bin/env python3
# -*- coding: ascii -*-
"""clark_patrol.py -- Clark Patrol P1: roster, weekday scheduler,
persistent patrol state (spec 2026-08-19).

Two discovery paths, one downstream: Patrol is ENQUEUE-ONLY. It never
drains the queue, never builds digests, never sends email; the intake
tick remains the sole drainer/sender.

P1 scope: roster loading/validation, deterministic weekday batches
(40 employers/day, id partition (id-1)//40), intra-day chunk checklist
(ruling: spread the day's 40 across the day), patrol.sqlite schema for
job memory + day checklists. Discovery, gates, and enqueue wiring are
P2/P3 and are NOT in this file yet.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

PATROL_ROOT = f"{_TB_BASE}/shared/career_clark/patrol"
ROSTER_PATH = os.environ.get("PATROL_ROSTER",
                             os.path.join(PATROL_ROOT, "roster.json"))
DB_PATH = os.environ.get("PATROL_DB",
                         os.path.join(PATROL_ROOT, "patrol.sqlite"))
PLAN_LISTS = f"{_TB_BASE}/shared/career/plan_lists.json"
BATCH_SIZE = 40
ROSTER_SIZE = 200
VALID_LANES = ("clark", "han")
CHECK_STATES = ("PENDING", "DONE", "FAILED")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employer_id      INTEGER NOT NULL,
    employer         TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url              TEXT NOT NULL,
    normalized_url   TEXT NOT NULL,
    ext_id           TEXT,
    location         TEXT,
    remote_mode      TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    discovered_at    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'ACTIVE',
    source           TEXT,
    lane             TEXT,
    disposition      TEXT,
    content_hash     TEXT,
    UNIQUE(employer_id, normalized_url)
);
CREATE INDEX IF NOT EXISTS idx_jobs_employer ON jobs(employer_id, status);
CREATE TABLE IF NOT EXISTS day_checklist (
    day         TEXT NOT NULL,
    employer_id INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (day, employer_id)
);
"""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_roster(path=ROSTER_PATH):
    """Load + validate the roster. Raises ValueError on any defect --
    a broken roster must never half-run."""
    data = json.load(open(path))
    emps = data.get("employers")
    if not isinstance(emps, list) or len(emps) != ROSTER_SIZE:
        raise ValueError("roster must contain exactly %d employers, got %s"
                         % (ROSTER_SIZE,
                            len(emps) if isinstance(emps, list) else "?"))
    ids = set()
    names = set()
    for e in emps:
        eid = e.get("id")
        name = (e.get("name") or "").strip()
        lanes = e.get("lanes")
        if not isinstance(eid, int) or eid < 1 or eid > ROSTER_SIZE:
            raise ValueError("bad employer id: %r" % (eid,))
        if eid in ids:
            raise ValueError("duplicate employer id: %d" % eid)
        ids.add(eid)
        if not name:
            raise ValueError("empty name for id %d" % eid)
        if name.lower() in names:
            raise ValueError("duplicate employer name: %s" % name)
        names.add(name.lower())
        if (not isinstance(lanes, list) or not lanes or
                any(l not in VALID_LANES for l in lanes)):
            raise ValueError("bad lanes for %s: %r" % (name, lanes))
        if not isinstance(e.get("wildcard"), bool):
            raise ValueError("wildcard must be bool for %s" % name)
    return emps


def day_index_for_id(eid):
    """0=Monday ... 4=Friday partition."""
    return (eid - 1) // BATCH_SIZE


def batch_for_day(roster, date_str):
    """Employers scheduled for date_str (YYYY-MM-DD). Weekend -> []."""
    wd = time.strptime(date_str, "%Y-%m-%d").tm_wday  # 0=Mon
    if wd > 4:
        return []
    return [e for e in roster if day_index_for_id(e["id"]) == wd]


def open_db(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def ensure_checklist(conn, roster, date_str):
    """Create PENDING checklist rows for date_str's batch. Idempotent:
    reruns never reset DONE/FAILED progress. Returns rows inserted."""
    batch = batch_for_day(roster, date_str)
    n = 0
    for e in batch:
        cur = conn.execute(
            "INSERT OR IGNORE INTO day_checklist "
            "(day, employer_id, status, attempts, updated_at) "
            "VALUES (?, ?, 'PENDING', 0, ?)",
            (date_str, e["id"], _now()))
        n += cur.rowcount
    conn.commit()
    return n


def next_pending(conn, date_str, limit):
    """Next chunk of PENDING employer ids for this day, id order."""
    rows = conn.execute(
        "SELECT employer_id FROM day_checklist "
        "WHERE day = ? AND status = 'PENDING' "
        "ORDER BY employer_id LIMIT ?", (date_str, int(limit))).fetchall()
    return [r[0] for r in rows]


def mark_employer(conn, date_str, employer_id, status, reason=None):
    """Record an employer's patrol outcome for the day. FAILED is
    distinguishable from no-jobs-found (which is DONE + reason)."""
    if status not in ("DONE", "FAILED"):
        raise ValueError("status must be DONE or FAILED")
    conn.execute(
        "UPDATE day_checklist SET status = ?, reason = ?, "
        "attempts = attempts + 1, updated_at = ? "
        "WHERE day = ? AND employer_id = ?",
        (status, reason, _now(), date_str, employer_id))
    conn.commit()


def checklist_stats(conn, date_str):
    out = {s: 0 for s in CHECK_STATES}
    for s, c in conn.execute(
            "SELECT status, COUNT(*) FROM day_checklist "
            "WHERE day = ? GROUP BY status", (date_str,)):
        out[s] = c
    return out


# ===================== P2: gates + job memory =====================

COMP_FLOOR = int(os.environ.get("PATROL_COMP_FLOOR", "130000"))

LANE_QUERIES = {
    "clark": ("product manager OR product owner OR technical program "
              "manager OR portfolio manager"),
    "han": ("research program manager OR AI governance OR technology "
            "policy OR research operations OR program director"),
}

_REJECT_TITLE_RE = re.compile(
    r"\b(junior|intern(ship)?|entry[- ]level|co-?op)\b", re.IGNORECASE)
_BENCH_TITLE_RE = re.compile(
    r"\b(lab(oratory)? )?technician\b|\bbench (scientist|work)\b",
    re.IGNORECASE)
_CLINICAL_TITLE_RE = re.compile(
    r"\b(nurse|physician|clinician|therapist|phlebotomist|"
    r"medical assistant)\b", re.IGNORECASE)
_COMMISSION_RE = re.compile(
    r"commission[- ](only|based)\b|100% commission", re.IGNORECASE)
_RELOCATION_RE = re.compile(
    r"relocat(e|ion)( is)?( required| assistance required)|"
    r"must (be willing to )?relocate", re.IGNORECASE)
_MA_RE = re.compile(r"massachusetts|,\s*ma\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b", re.IGNORECASE)
_COMP_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?k|\d{5,6})",
    re.IGNORECASE)


def parse_comp(text):
    """Extract salary figures from text. Returns (lo, hi) ints or None.
    Handles $130,000 / $95k / $130,000 - $150,000 USD forms. Amounts
    under $20,000 are ignored as noise (fees, bonuses)."""
    vals = []
    for m in _COMP_RE.finditer(str(text or "")):
        raw = m.group(1).lower().replace(",", "").replace(" ", "")
        if raw.endswith("k"):
            try:
                vals.append(int(float(raw[:-1]) * 1000))
            except ValueError:
                continue
        else:
            try:
                vals.append(int(raw))
            except ValueError:
                continue
    vals = [v for v in vals if v >= 20000]
    if not vals:
        return None
    return (min(vals), max(vals))


def hard_gate(job, excludes):
    """Deterministic REJECT-only gate (ruling: no title allowlist;
    fit review is the semantic selector). Returns (True, None) or
    (False, reason)."""
    company = str(job.get("company") or "").lower()
    for x in excludes:
        xl = str(x).lower()
        if xl and (xl in company or company in xl) and company:
            return False, "excluded company: %s" % job.get("company")
    title = str(job.get("title") or "")
    desc = str(job.get("description") or "")
    if not desc.strip():
        return False, "empty description"
    if _REJECT_TITLE_RE.search(title):
        return False, "junior/entry title"
    if _BENCH_TITLE_RE.search(title):
        return False, "lab bench role"
    if _CLINICAL_TITLE_RE.search(title):
        return False, "clinical role"
    if _COMMISSION_RE.search(title + " " + desc):
        return False, "commission sales"
    if _RELOCATION_RE.search(desc):
        return False, "relocation required"
    loc = str(job.get("location") or "")
    if not (_MA_RE.search(loc) or _REMOTE_RE.search(loc)
            or _REMOTE_RE.search(desc[:300])):
        return False, "geo: %s" % (loc or "unknown")
    comp = parse_comp(desc)
    if comp and comp[1] < COMP_FLOOR:
        return False, "comp below floor: $%d max" % comp[1]
    return True, None


def job_content_hash(title, location, comp):
    basis = "%s|%s|%s" % (str(title or "").lower().strip(),
                          str(location or "").lower().strip(),
                          comp or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def record_job(conn, emp, job, lane):
    """Upsert into job memory. Returns 'NEW', 'KNOWN', or 'CHANGED'.
    CHANGED = material change (title/location/comp hash differs)."""
    import clark_queue  # sibling lib; reuse normalization (no dupes)
    nurl = clark_queue.normalize_url(job.get("url") or "")
    comp = parse_comp(job.get("description"))
    chash = job_content_hash(job.get("title"), job.get("location"),
                             "%s-%s" % comp if comp else "")
    now = _now()
    row = conn.execute(
        "SELECT job_id, content_hash FROM jobs "
        "WHERE employer_id = ? AND normalized_url = ?",
        (emp["id"], nurl)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO jobs (employer_id, employer, normalized_title, "
            "url, normalized_url, location, remote_mode, first_seen, "
            "last_seen, discovered_at, status, source, lane, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
            (emp["id"], emp["name"],
             str(job.get("title") or "").lower().strip(),
             job.get("url"), nurl, job.get("location"),
             "remote" if _REMOTE_RE.search(
                 str(job.get("location") or "")) else "onsite/hybrid",
             now, now, now, job.get("source", "patrol"), lane, chash))
        conn.commit()
        return "NEW"
    if row[1] == chash:
        conn.execute("UPDATE jobs SET last_seen = ? WHERE job_id = ?",
                     (now, row[0]))
        conn.commit()
        return "KNOWN"
    conn.execute(
        "UPDATE jobs SET content_hash = ?, last_seen = ?, "
        "status = 'CHANGED', normalized_title = ? WHERE job_id = ?",
        (chash, now, str(job.get("title") or "").lower().strip(), row[0]))
    conn.commit()
    return "CHANGED"


def close_missing(conn, employer_id, seen_norm_urls):
    """Mark jobs not seen in a SUCCESSFUL patrol of this employer as
    CLOSED. Never called on patrol failure. Returns count closed."""
    rows = conn.execute(
        "SELECT job_id, normalized_url FROM jobs WHERE employer_id = ? "
        "AND status IN ('ACTIVE','CHANGED')", (employer_id,)).fetchall()
    closed = 0
    now = _now()
    for jid, nurl in rows:
        if nurl not in seen_norm_urls:
            conn.execute("UPDATE jobs SET status = 'CLOSED', "
                         "last_seen = ? WHERE job_id = ?", (now, jid))
            closed += 1
    conn.commit()
    return closed


def build_queries(emp):
    return [(lane, "%s %s jobs" % (emp["name"], LANE_QUERIES[lane]))
            for lane in emp["lanes"]]


def patrol_employer(conn, emp, fetch_fn, excludes, notes):
    """Patrol one employer. fetch_fn(emp, lane, query) -> list of job
    dicts {title, company, location, description, url}; exceptions
    propagate so the caller marks the employer FAILED (spec: failure
    must be distinguishable from no-jobs-found and never recorded as
    success). Returns telemetry dict incl. admitted candidates."""
    import clark_queue
    tele = {"examined": 0, "new": 0, "changed": 0, "rejected": 0,
            "dupes_suppressed": 0, "closed": 0, "admitted": []}
    seen_this_run = set()
    for lane, query in build_queries(emp):
        jobs = fetch_fn(emp, lane, query)  # may raise -> FAILED upstream
        for job in jobs:
            nurl = clark_queue.normalize_url(job.get("url") or "")
            if not nurl or nurl in seen_this_run:
                continue
            seen_this_run.add(nurl)
            tele["examined"] += 1
            ok, reason = hard_gate(job, excludes)
            if not ok:
                tele["rejected"] += 1
                notes.append("patrol reject %s @ %s: %s" % (
                    str(job.get("title") or "")[:40], emp["name"], reason))
                continue
            state = record_job(conn, emp, job, lane)
            if state == "KNOWN":
                tele["dupes_suppressed"] += 1
                continue
            tele["new" if state == "NEW" else "changed"] += 1
            comp = parse_comp(job.get("description"))
            comp_str = ""
            if comp:
                comp_str = ("$%s" % "{:,}".format(comp[0])
                            if comp[0] == comp[1] else
                            "$%s-$%s" % ("{:,}".format(comp[0]),
                                         "{:,}".format(comp[1])))
            cand = {"url": job.get("url"), "title": job.get("title"),
                    "company": job.get("company") or emp["name"],
                    "location": job.get("location", ""),
                    "description": job.get("description", ""),
                    "comp": comp_str,
                    "share_link": job.get("share_link"),
                    "family": "patrol", "origin_class": "PATROL",
                    "lane": lane, "employer_id": emp["id"]}
            tele["admitted"].append(cand)
    tele["closed"] = close_missing(conn, emp["id"], seen_this_run)
    return tele


def _fixture_roster():
    """Synthetic roster satisfying every load_roster rule (200 ids, unique
    names, valid lanes, bool wildcard), lane mix shaped like production's.
    The selftests exercise the patrol CONTRACT on it, so they run on any
    host; the operator's real roster is private state and is only checked
    when it exists (CR-2, 2026-08-28)."""
    lanes = (["clark"], ["clark", "han"], ["han"])
    return [{"id": i, "name": "Fixture Employer %03d" % i,
             "lanes": list(lanes[i % 3]), "wildcard": (i % 97 == 0)}
            for i in range(1, ROSTER_SIZE + 1)]


def _selftest():
    import tempfile as _tf
    fails = []
    ran = []

    def check(name, cond):
        ran.append(name)
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    # --- roster contract on a fixture written through load_roster itself ---
    _fd, _fp = _tf.mkstemp(prefix="patrol-roster-", suffix=".json"); os.close(_fd)
    with open(_fp, "w", encoding="utf-8") as _fh:
        json.dump({"version": 1, "employers": _fixture_roster()}, _fh)
    roster = load_roster(_fp)
    check("p01 fixture roster loads: 200 valid employers", len(roster) == 200)
    _bad = {"version": 1, "employers": _fixture_roster()}
    _bad["employers"][5]["id"] = 7                     # index 5 IS id 6; 7 duplicates the next entry
    with open(_fp, "w", encoding="utf-8") as _fh:
        json.dump(_bad, _fh)
    try:
        load_roster(_fp)
        check("p01b defective roster refused (fail-closed)", False)
    except ValueError:
        check("p01b defective roster refused (fail-closed)", True)
    try:
        load_roster(_fp + ".absent")
        check("p01c absent roster refused (fail-closed)", False)
    except OSError:
        check("p01c absent roster refused (fail-closed)", True)
    os.unlink(_fp)
    # --- the LIVE roster is a fact about this host: validate when present ---
    live = None
    if os.path.isfile(ROSTER_PATH):
        try:
            live = load_roster(ROSTER_PATH)
            check("p01d live roster valid: 200 employers", len(live) == 200)
        except (OSError, ValueError) as e:
            check("p01d live roster valid (%s)" % e, False)
    else:
        print("SKIP p01d live roster: no patrol roster on this host (%s) -- the "
              "operator has not authored one; contract verified on the fixture"
              % ROSTER_PATH)

    # Known week: 2026-08-24 is a Monday.
    mon = batch_for_day(roster, "2026-08-24")
    fri = batch_for_day(roster, "2026-08-28")
    sat = batch_for_day(roster, "2026-08-29")
    check("p02 Monday = ids 1-40 exactly",
          sorted(e["id"] for e in mon) == list(range(1, 41)))
    check("p03 Friday = ids 161-200 exactly",
          sorted(e["id"] for e in fri) == list(range(161, 201)))
    check("p04 weekend = empty batch", sat == [])

    days = ["2026-08-2%d" % d for d in (4, 5, 6, 7, 8)]
    seen = []
    for d in days:
        seen.extend(e["id"] for e in batch_for_day(roster, d))
    check("p05 full week covers all 200 exactly once",
          sorted(seen) == list(range(1, 201)) and len(seen) == 200)

    conn = open_db(":memory:")
    init_schema(conn)
    n1 = ensure_checklist(conn, roster, "2026-08-24")
    n2 = ensure_checklist(conn, roster, "2026-08-24")
    check("p06 checklist idempotent", n1 == 40 and n2 == 0)

    chunk = next_pending(conn, "2026-08-24", 3)
    check("p07 chunking honors limit + id order", chunk == [1, 2, 3])

    mark_employer(conn, "2026-08-24", 1, "DONE", "2 jobs found")
    mark_employer(conn, "2026-08-24", 2, "FAILED", "site parse error")
    mark_employer(conn, "2026-08-24", 3, "DONE", "no jobs found")
    chunk = next_pending(conn, "2026-08-24", 3)
    check("p08 failure does not block batch", chunk == [4, 5, 6])

    stats = checklist_stats(conn, "2026-08-24")
    check("p09 FAILED distinguishable from no-jobs-found",
          stats == {"PENDING": 37, "DONE": 2, "FAILED": 1})

    remaining = next_pending(conn, "2026-08-24", 100)
    for eid in remaining:
        mark_employer(conn, "2026-08-24", eid, "DONE")
    stats = checklist_stats(conn, "2026-08-24")
    check("p10 day drains to zero pending",
          stats["PENDING"] == 0 and stats["DONE"] == 39
          and stats["FAILED"] == 1)

    if live is not None and os.path.isfile(PLAN_LISTS):
        try:
            excl = [str(x).lower() for x in json.load(open(PLAN_LISTS)).get(
                "exclude_companies", [])]
            overlap = [e["name"] for e in live
                       if any(x in e["name"].lower() or e["name"].lower() in x
                              for x in excl)]
            check("p11 no excluded company in live roster", overlap == [])
        except (OSError, ValueError) as e:
            check("p11 no excluded company in live roster (plan_lists "
                  "unreadable: %s)" % e, False)
    else:
        print("SKIP p11 roster/exclusion overlap: needs the operator's live roster "
              "and plan_lists.json; one or both absent on this host")


    # ===================== P2 tests =====================
    excl = ["Walmart", "Philip Morris", "Wells Fargo", "Flock Safety",
            "Oracle"]

    def job(t="Senior Product Manager", c="Acme", l="Boston, MA",
            d="Great role. $140,000 - $160,000 annually.",
            u="https://careers.acme.com/job/1"):
        return {"title": t, "company": c, "location": l,
                "description": d, "url": u}

    check("p12 exclusion gate",
          hard_gate(job(c="Oracle"), excl) == (False,
          "excluded company: Oracle"))
    ok, r = hard_gate(job(d="Must relocate to California. $150,000."),
                      excl)
    check("p13 relocation reject", not ok and "relocation" in r)
    ok, _ = hard_gate(job(l="Remote - US"), excl)
    check("p14 remote passes", ok)
    ok, r = hard_gate(job(l="Austin, TX", d="$150,000"), excl)
    check("p15 non-MA non-remote reject", not ok and r.startswith("geo"))
    ok, r = hard_gate(job(d="Pay: $95,000 - $110,000"), excl)
    check("p16 comp floor reject", not ok and "comp below" in r)
    ok, _ = hard_gate(job(d="Competitive compensation."), excl)
    check("p17 unknown comp passes to review", ok)
    ok, _ = hard_gate(job(t="Junior Product Manager"), excl)
    ok2, _ = hard_gate(job(t="Lab Technician - Returns"), excl)
    check("p18 junior + bench reject", not ok and not ok2)
    ok, r = hard_gate(job(d=""), excl)
    check("p26 empty description reject", not ok and r == "empty description")
    ok, r = hard_gate(job(d="   \n\t "), excl)
    check("p27 whitespace-only description reject",
          not ok and r == "empty description")
    check("p19 parse_comp forms",
          parse_comp("$130,000 - $150,000 USD") == (130000, 150000)
          and parse_comp("about $95k plus equity") == (95000, 95000)
          and parse_comp("no numbers here") is None)

    conn2 = open_db(":memory:")
    init_schema(conn2)
    emp = {"id": 163, "name": "SharkNinja", "lanes": ["clark"],
           "wildcard": False}
    fixture = [job(u="https://careers.acme.com/job/%d" % i,
                   t="Product Manager %d" % i) for i in range(7)]
    notes2 = []
    t1 = patrol_employer(conn2, emp, lambda e, ln, q: fixture, excl,
                         notes2)
    check("p20 seven matches all admitted (no per-employer cap)",
          t1["new"] == 7 and len(t1["admitted"]) == 7)
    t2 = patrol_employer(conn2, emp, lambda e, ln, q: fixture, excl, [])
    check("p21 week-2 unchanged suppressed",
          t2["dupes_suppressed"] == 7 and t2["admitted"] == []
          and t2["new"] == 0)
    changed = [dict(fixture[0], title="Principal Product Manager 0")] + \
        fixture[1:]
    t3 = patrol_employer(conn2, emp, lambda e, ln, q: changed, excl, [])
    check("p22 material change re-admitted",
          t3["changed"] == 1 and len(t3["admitted"]) == 1)
    t4 = patrol_employer(conn2, emp, lambda e, ln, q: fixture[:3], excl,
                         [])
    check("p23 vanished jobs marked CLOSED", t4["closed"] == 4)

    def boom(e, ln, q):
        raise RuntimeError("careers site markup changed")
    try:
        patrol_employer(conn2, emp, boom, excl, [])
        check("p24 fetch failure propagates (never fake success)", False)
    except RuntimeError:
        check("p24 fetch failure propagates (never fake success)", True)

    t5b = patrol_employer(
        conn2, {"id": 2, "name": "Acme", "lanes": ["clark"],
                "wildcard": False},
        lambda e, ln, q: [job(u="https://x.com/comp1")], excl, [])
    check("p25 deterministic comp on candidate",
          t5b["admitted"][0]["comp"] == "$140,000-$160,000")

    print("%d/%d patrol selftests passed" % (len(ran) - len(fails), len(ran)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        _selftest()
    sys.exit("usage: clark_patrol.py --selftest")
