#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
clark_forward_extractor.py -- turn a stored inbound email into a list
of Candidate dicts for the intake driver.

Input:  resend_id whose raw store dir exists under
        /opt/treebeard/shared/inbound/clark/raw/<resend_id>/
Output: dict {ok, resend_id, forwarder, candidates: [...], reason?}
        Each candidate:
          {url, class, query, company?, location?,
           title?, provenance: {source, raw_url}}

URL classes (extensible: unknown lands in UNKNOWN bucket, surfaces in
email so operator can decide + Moe can add a handler):
  SHARE_GOOGLE       share.google/<id>  -> use body-text-as-query
  EMPLOYER_INDEX     careers.<company>./..., jobs.<company>./...
                     without a specific job id
  EMPLOYER_JOB       employer careers URL with a specific job id/slug
  ATS_JOB            greenhouse.io/<co>/jobs/<id>, lever.co/<co>/<id>,
                     workable.com/<co>/j/<id>, ashbyhq.com,
                     smartrecruiters, myworkdayjobs, icims, bamboohr
  ATS_INDEX          ATS company index page
  AGGREGATOR_JOB     linkedin.com/jobs/view/<id>, indeed.com/viewjob,
                     ziprecruiter.com/jobs/<id>, glassdoor.com/job,
                     builtin.com/job, monster.com/job
  AGGREGATOR_INDEX   linkedin.com/jobs/search, indeed.com/q-...,
                     ziprecruiter.com/candidate/search
  SLUDGE             jobilize, jobzmall, bebee, jobleads, simplify.jobs,
                     lensa, jooble, adzuna, careerbuilder, snagajob
  UNKNOWN            URL shape no handler recognizes
  TEXT_ONLY_QUERY    no URL in email, subject/body text used as query

Driver (not this file) turns each candidate.query into a SerpApi
google_jobs call (Path Y): even for direct employer/ATS URLs, we run
the query to get consistent structured job data, and the direct URL
becomes the apply link in the packet. If SerpApi returns nothing,
driver's Path X fallback scrapes the direct URL for job links.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DEFAULT_BASE_DIR = f"{_TB_BASE}/shared/inbound"
FWD_PREAMBLE_RE = re.compile(
    r"^-{5,}\s*Forwarded message\s*-{5,}\s*$", re.MULTILINE)
SUBJECT_FWD_RE = re.compile(r"^\s*(?:Fwd|FWD|Fw|FW):\s*", re.IGNORECASE)
SUBJECT_GOOGLE_SUFFIX_RE = re.compile(
    r"\s*-\s*Google\s+(?:Search|Jobs)\s*$", re.IGNORECASE)
SHARE_GOOGLE_RE = re.compile(r"https:?//share\.google/[A-Za-z0-9_-]+")
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)

_STRIP_CATS = frozenset((
    "So", "Sm", "Sk", "Sc",
    "Po", "Pd", "Pe", "Pf", "Pi", "Ps", "Pc",
    "Zs", "Zl", "Zp",
    "Cf", "Cc", "Co", "Cn", "Mn", "Me", "Mc",
))

ATS_HOSTS = (
    "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com",
    "smartrecruiters.com", "myworkdayjobs.com", "icims.com",
    "bamboohr.com", "jobvite.com", "recruitee.com", "personio.com",
    "teamtailor.com", "breezy.hr",
)
AGG_HOSTS = (
    "linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com",
    "builtin.com", "monster.com", "dice.com", "wellfound.com",
    "angel.co",
)
SLUDGE_HOSTS = (
    "jobilize.com", "jobzmall.com", "bebee.com", "jobleads.com",
    "simplify.jobs", "lensa.com", "jooble.org", "adzuna.com",
    "careerbuilder.com", "snagajob.com", "learn4good.com",
    "pursuitci.com", "jobsora.com", "talent.com",
)
AGG_JOB_PATH_HINTS = (
    "/jobs/view/", "/jobs/collections/", "/viewjob", "/jobs/",
    "/job/", "/j/", "/candidate/",
)
AGG_INDEX_PATH_HINTS = (
    "/jobs/search", "/q-", "/search?", "/candidate/search",
)


def _strip_leading_decoration(s):
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace() or unicodedata.category(ch) in _STRIP_CATS:
            i += 1
        else:
            break
    return s[i:]


def _load_message(resend_id, base_dir):
    d = os.path.join(base_dir, "clark", "raw", resend_id)
    if not os.path.isdir(d):
        raise FileNotFoundError("no raw store dir for %s" % resend_id)
    headers = json.load(open(os.path.join(d, "headers.json")))
    text = open(os.path.join(d, "text.txt")).read()
    return headers, text


def _forwarder_from_headers(headers):
    raw = headers.get("from") or headers.get("From") or ""
    m = re.search(r"<([^>]+)>", raw)
    if m:
        return m.group(1).strip().lower()
    return raw.strip().lower()


def _content_after_preamble(text):
    m = FWD_PREAMBLE_RE.search(text)
    if not m:
        return text.strip()
    tail = text[m.end():]
    parts = tail.split("\n\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return tail.strip()


def _clean_subject_query(subject):
    s = subject or ""
    for _ in range(4):
        new = SUBJECT_FWD_RE.sub("", s)
        if new == s:
            break
        s = new
    s = SUBJECT_GOOGLE_SUFFIX_RE.sub("", s)
    s = _strip_leading_decoration(s)
    s = s.strip()
    return s or None


def _text_query_from_body(content, exclude_url=None):
    """Find the last non-blank text line in body content that isn't
    just a URL. Used when we need description text near a URL."""
    lines = content.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        if URL_RE.fullmatch(line):
            continue
        cleaned = _strip_leading_decoration(line)
        cleaned = SUBJECT_GOOGLE_SUFFIX_RE.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            if exclude_url and exclude_url in cleaned:
                cleaned = cleaned.replace(exclude_url, "").strip()
                if not cleaned:
                    continue
            return cleaned
    return None


def _classify_url(url):
    """Return (class, host, path). Class is one of SHARE_GOOGLE,
    EMPLOYER_INDEX, EMPLOYER_JOB, ATS_JOB, ATS_INDEX, AGGREGATOR_JOB,
    AGGREGATOR_INDEX, SLUDGE, UNKNOWN."""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return "UNKNOWN", "", ""
    host = (p.hostname or "").lower()
    path = p.path or ""
    if not host:
        return "UNKNOWN", host, path
    if host == "share.google":
        return "SHARE_GOOGLE", host, path
    for s in SLUDGE_HOSTS:
        if host == s or host.endswith("." + s):
            return "SLUDGE", host, path
    for a in ATS_HOSTS:
        if host == a or host.endswith("." + a):
            if re.search(r"/(jobs?|j|opening|posting)/[^/]+", path,
                         re.IGNORECASE):
                return "ATS_JOB", host, path
            return "ATS_INDEX", host, path
    for a in AGG_HOSTS:
        if host == a or host.endswith("." + a):
            lp = path.lower()
            for hint in AGG_INDEX_PATH_HINTS:
                if hint in lp:
                    return "AGGREGATOR_INDEX", host, path
            for hint in AGG_JOB_PATH_HINTS:
                if hint in lp:
                    return "AGGREGATOR_JOB", host, path
            return "AGGREGATOR_INDEX", host, path
    if (host.startswith("careers.") or host.startswith("jobs.") or
        re.search(r"/(careers?|jobs?|opening|position)(/|$)", path,
                  re.IGNORECASE)):
        if re.search(r"/(job|jobs|opening|position)/[^/]+/[0-9]{3,}",
                     path, re.IGNORECASE):
            return "EMPLOYER_JOB", host, path
        if re.search(r"/(job|jobs|opening|position)/[^/]+/[^/]+",
                     path, re.IGNORECASE):
            return "EMPLOYER_JOB", host, path
        return "EMPLOYER_INDEX", host, path
    return "UNKNOWN", host, path


def _company_from_employer_host(host):
    """careers.sharkninja.com -> sharkninja."""
    parts = host.split(".")
    if len(parts) < 2:
        return None
    while parts and parts[0].lower() in ("careers", "jobs", "www",
                                          "careers-us", "apply"):
        parts.pop(0)
    if not parts:
        return None
    return parts[0]


def _company_from_ats_path(host, path):
    """greenhouse.io -> path[0] is company. Best-effort per ATS."""
    segs = [s for s in path.split("/") if s]
    if not segs:
        return None
    if ("greenhouse.io" in host or "lever.co" in host or
        "workable.com" in host or "ashbyhq.com" in host or
        "myworkdayjobs.com" in host or "smartrecruiters.com" in host):
        return segs[0]
    return None


def _location_hints_from_path(path):
    """/location/example-state-united-states-jobs/ -> 'example state'."""
    lp = path.lower()
    m = re.search(r"/location/([a-z\-]+?)(?:-united-states|-jobs|/|$)", lp)
    if m:
        return m.group(1).replace("-", " ").strip()
    m = re.search(r"/(?:jobs?|opening|position)/([a-z\-]{3,30})/", lp)
    if m:
        candidate = m.group(1).replace("-", " ").strip()
        if candidate not in ("all", "search", "view", "apply"):
            return candidate
    return None


def _build_candidate(url, url_class, host, path):
    """Turn a classified URL into a Candidate dict."""
    cand = {
        "url": url,
        "class": url_class,
        "query": None,
        "company": None,
        "location": None,
        "title": None,
        "provenance": {"source": "email_url", "raw_url": url},
    }
    if url_class in ("EMPLOYER_INDEX", "EMPLOYER_JOB"):
        company = _company_from_employer_host(host)
        if company:
            cand["company"] = company
        loc = _location_hints_from_path(path)
        if loc:
            cand["location"] = loc
        if company and loc:
            cand["query"] = "%s jobs %s" % (company, loc)
        elif company:
            cand["query"] = "%s careers" % company
    elif url_class in ("ATS_JOB", "ATS_INDEX"):
        company = _company_from_ats_path(host, path)
        if company:
            cand["company"] = company
            cand["query"] = "%s careers" % company
    elif url_class == "AGGREGATOR_JOB":
        segs = [s for s in path.split("/") if s]
        for s in segs:
            if "-" in s and len(s) > 10 and not s.isdigit():
                cand["title"] = s.replace("-", " ")
                cand["query"] = cand["title"]
                break
    return cand


def extract(resend_id, base_dir=DEFAULT_BASE_DIR):
    """Public API. Returns dict with candidates list. Never raises
    for expected shapes. Raises only for missing raw store dir or
    unreadable required files."""
    headers, text = _load_message(resend_id, base_dir)
    forwarder = _forwarder_from_headers(headers)
    subject = headers.get("subject") or headers.get("Subject") or ""
    content = _content_after_preamble(text)
    subj_query = _clean_subject_query(subject)

    urls_in_body = []
    seen_urls = set()
    for line in content.splitlines():
        for m in URL_RE.finditer(line):
            u = m.group(0).rstrip(".,;:)")
            if u not in seen_urls:
                seen_urls.add(u)
                urls_in_body.append(u)

    candidates = []
    if urls_in_body:
        for u in urls_in_body:
            url_class, host, path = _classify_url(u)
            cand = _build_candidate(u, url_class, host, path)
            if cand["query"] is None:
                q = _text_query_from_body(content, exclude_url=u)
                if q:
                    cand["query"] = q
                elif subj_query:
                    cand["query"] = subj_query
            candidates.append(cand)
    else:
        q = _text_query_from_body(content) or subj_query
        if q:
            candidates.append({
                "url": None,
                "class": "TEXT_ONLY_QUERY",
                "query": q,
                "company": None,
                "location": None,
                "title": None,
                "provenance": {"source": "email_text", "raw_url": None},
            })

    if not candidates:
        return {
            "ok": False,
            "resend_id": resend_id,
            "forwarder": forwarder,
            "reason": "no URLs and no extractable query text",
        }
    return {
        "ok": True,
        "resend_id": resend_id,
        "forwarder": forwarder,
        "candidates": candidates,
        "original_subject": subject,
    }


if __name__ == "__main__":
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        result = extract(sys.argv[1])
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 2)
    sys.exit("usage: clark_forward_extractor.py <resend_id>")
