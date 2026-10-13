#!/usr/bin/env python3
# -*- coding: ascii -*-
# Tests for career_packets. Maps to operator DoD items:
#   A fixture pipeline  B exclusion control  C applied-suppression control
#   D factuality control  E lockbox no-overwrite  F email render
# No network: send=False everywhere; PDF via real reportlab.
import json
import os
import shutil
import tempfile
import unittest

import career_packets as cp


def W(text, before="", after=""):
    """A tailoring reply that follows the output contract."""
    return "%s%s\n%s\n%s\n%s" % (before, cp.RESUME_BEGIN, text, cp.RESUME_END, after)

CORPUS_RESUME = """Sample Person
Senior Technical Product Owner, Acme Insurance 2019-2024
Led cross-functional delivery of 3 platform programs.
Improved claims cycle time 22%.
MS Project Management, State University
BS Molecular Biology, Tech Institute
"""
CORPUS_PROFILE = "Target: Product Manager. Remote or Boston. Floor $70K.\n"
GOOD_RESUME = """Sample Person
SUMMARY: Program leader; led cross-functional delivery of 3 programs.
Improved claims cycle time 22%.
EXPERIENCE: Senior Technical Product Owner, Acme Insurance 2019-2024
EDUCATION: MS Project Management, State University
"""
REPORT = """WORTHWHILE: 2
prose...
SELECTED_JSON: [
 {"url": "https://a.com/careers/1", "company": "GoodCo", "role": "Program Manager",
  "description": "run programs", "fit": "28/35", "comp": "UNKNOWN", "why": "fits"},
 {"url": "https://b.com/careers/2", "company": "Flock Safety", "role": "PM",
  "description": "x", "fit": "30/35", "comp": "UNKNOWN", "why": "no"},
 {"url": "https://c.com/careers/3", "company": "OldCo", "role": "PM",
  "description": "y", "fit": "27/35", "comp": "UNKNOWN", "why": "seen"}
]
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cptest-")
        cp.CAREER_ROOT = os.path.join(self.tmp, "career")
        cp.LOCKBOX = os.path.join(self.tmp, "lockbox")
        cp.RESUME_PATH = os.path.join(self.tmp, "resume.txt")
        cp.PROFILE_PATH = os.path.join(self.tmp, "profile.txt")
        cp.PERSONA_TOKENS = ("acme alias", "acme-handle", "acmenet")
        os.makedirs(cp.CAREER_ROOT)
        open(cp.RESUME_PATH, "w").write(CORPUS_RESUME)
        open(cp.PROFILE_PATH, "w").write(CORPUS_PROFILE)
        cp.atomic_write(os.path.join(cp.CAREER_ROOT, "plan_lists.json"),
                        json.dumps({"exclude_companies": ["Flock Safety"]}))
        cp.atomic_write(os.path.join(cp.CAREER_ROOT, "applications.json"),
                        json.dumps({"https://c.com/careers/3":
                                    {"state": "APPLIED"}}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestFactuality(Base):
    def test_good_resume_passes(self):
        ok, v, w = cp.verify_factuality(GOOD_RESUME,
                                        CORPUS_RESUME + CORPUS_PROFILE, "")
        self.assertTrue(ok, v)

    def test_invented_metric_fails_closed(self):
        # DoD D: a number absent from the corpus blocks the packet.
        bad = GOOD_RESUME + "Cut costs 47% across 9 teams.\n"
        ok, v, _ = cp.verify_factuality(bad, CORPUS_RESUME, "")
        self.assertFalse(ok)
        self.assertTrue(any("47" in x for x in v))

    def test_invented_credential_fails_closed(self):
        bad = GOOD_RESUME + "PMP certified. TS/SCI clearance.\n"
        ok, v, _ = cp.verify_factuality(bad, CORPUS_RESUME, "")
        self.assertFalse(ok)
        self.assertTrue(any("PMP" in x for x in v))

    def test_credential_in_corpus_allowed(self):
        ok, v, _ = cp.verify_factuality("MS Project Management holder",
                                        CORPUS_RESUME, "")
        self.assertTrue(ok, v)

    def test_job_numbers_allowed(self):
        ok, v, _ = cp.verify_factuality(
            GOOD_RESUME + "Ready to own the 12-state rollout.\n",
            CORPUS_RESUME, "manage a 12-state rollout")
        self.assertTrue(ok, v)

    def test_unknown_proper_noun_warns_not_blocks(self):
        ok, v, w = cp.verify_factuality(
            GOOD_RESUME + "Deep Quantum Basketweaving Institute alum-adjacent.\n"
            .replace("alum-adjacent", "collaborator"),
            CORPUS_RESUME, "")
        self.assertTrue(ok, v)
        self.assertTrue(any("Quantum" in x for x in w))


class TestSelection(Base):
    def test_exclusion_and_suppression_controls(self):
        # DoD B: excluded company cannot produce a packet.
        # DoD C: previously APPLIED url cannot be regenerated.
        mem = {"https://c.com/careers/3": {"state": "APPLIED"}}
        jobs, notes = cp.parse_selected(REPORT, ["Flock Safety"], mem, 5)
        urls = [j["url"] for j in jobs]
        self.assertEqual(urls, ["https://a.com/careers/1"])
        self.assertTrue(any("excluded company refused" in n for n in notes))
        self.assertTrue(any("already APPLIED" in n for n in notes))

    def test_missing_block_is_visible_not_fatal(self):
        jobs, notes = cp.parse_selected("WORTHWHILE: 3\nno block", [], {}, 5)
        self.assertEqual(jobs, [])
        self.assertTrue(any("no SELECTED_JSON" in n for n in notes))

    def test_cap_respected(self):
        many = "SELECTED_JSON: " + json.dumps(
            [{"url": "https://x.com/%d" % i, "company": "C%d" % i,
              "role": "R"} for i in range(9)])
        jobs, _ = cp.parse_selected(many, [], {}, 5)
        self.assertEqual(len(jobs), 5)

    def test_brackets_in_strings_do_not_truncate(self):
        # Regression: 2026-08-18 live run r7e0cdb5d6a9 - a ']' inside a
        # title ("[Hiring] ...") made the old non-greedy regex hand
        # json.loads a truncated array -> "Unterminated string" ->
        # packets=0. raw_decode must bound the array itself.
        body = ("WORTHWHILE: 2\nreport text\nSELECTED_JSON: " + json.dumps(
            [{"url": "https://a.com/1", "company": "A", "role": "R1",
              "title": "[Hiring] Implementation Manager I @HMA"},
             {"url": "https://b.com/2", "company": "B", "role": "R2",
              "title": "plain"}]) + "\ntrailing text after the array")
        jobs, notes = cp.parse_selected(body, [], {}, 5)
        self.assertEqual([j["url"] for j in jobs],
                         ["https://a.com/1", "https://b.com/2"])
        self.assertFalse(any("unparseable" in n for n in notes))


class TestLockboxAndPdf(Base):
    def test_no_silent_overwrite(self):
        # DoD E: second packet same company/role/day gets a new dir.
        d1 = cp.packet_dir("2026-08-17", "GoodCo", "Program Manager")
        open(os.path.join(d1, "resume.txt"), "w").write("x")
        d2 = cp.packet_dir("2026-08-17", "GoodCo", "Program Manager")
        self.assertNotEqual(d1, d2)
        self.assertTrue(d2.endswith("-2"))

    def test_sanitize(self):
        self.assertEqual(cp.sanitize("Acme / Sub: PM (Remote!)"),
                         "Acme_Sub_PM_Remote")

    def test_pdf_renders_and_contains_pages(self):
        p = os.path.join(self.tmp, "r.pdf")
        cp.render_pdf(GOOD_RESUME * 20, p)
        from pypdf import PdfReader
        r = PdfReader(p)
        self.assertGreaterEqual(len(r.pages), 2)


class TestPipelineAndEmail(Base):
    def _fake_turn(self, prompt):
        return True, "[ok=True]", W(GOOD_RESUME)

    def test_fixture_pipeline_to_email_payload(self):
        # DoD A + F: fake job -> packet -> lockbox+metadata -> email render
        # with apply URL and full resume text surviving.
        notes = []
        tele = cp.run_packets(REPORT, self._fake_turn, notes, send=False)
        self.assertEqual(tele["packets"], 1)
        self.assertEqual(tele["selected"], 1)
        mem = cp.load_app_mem()
        rec = mem["https://a.com/careers/1"]
        self.assertEqual(rec["state"], "PACKET_READY")
        meta = json.loads(open(os.path.join(
            rec["packet_dir"], "metadata.json")).read())
        self.assertEqual(meta["apply_url"], "https://a.com/careers/1")
        self.assertTrue(os.path.exists(meta["artifacts"]["resume_pdf"]))
        day = meta["date_discovered"]
        email = open(os.path.join(cp.CAREER_ROOT, "reports",
                                  "email-%s.txt" % day)).read()
        self.assertIn("APPLY: https://a.com/careers/1", email)
        self.assertIn("claims cycle time 22%", email)

    def test_blocked_job_visible_never_dropped(self):
        # DoD D via pipeline + requirement 10. v2 (operator 2026-10-13): a
        # fabricating turn is rejected on every attempt and never published;
        # the base resume is delivered and the violation is named in the email.
        def lying_turn(prompt):
            return True, "[ok=True]", W(GOOD_RESUME + "PMP. Cut costs 47%.\n")
        notes = []
        tele = cp.run_packets(REPORT, lying_turn, notes, send=False)
        self.assertEqual((tele["blocked"], tele["packets"], tele["fallback"]), (0, 1, 1))
        mem = cp.load_app_mem()
        rec = mem["https://a.com/careers/1"]
        self.assertEqual(rec["state"], "PACKET_READY")
        self.assertTrue(any("47" in v for v in rec["tailor_blocked"]))
        txt = open(os.path.join(rec["packet_dir"], "resume.txt")).read()
        self.assertNotIn("47", txt)
        email = _email()
        self.assertIn("Tailored attempt 1 rejected", email)
        self.assertIn("47", email)

    def test_persona_scrub_unit(self):
        t = ("SAMPLE PERSON (Acme Alias)\n"
             "AcmeNet: @sample.acme-handle.example\n"
             "Led programs.\n")
        s = cp.scrub_persona(t)
        self.assertIn("SAMPLE PERSON", s)
        self.assertNotIn("Acme Alias", s)
        self.assertNotIn("acme-handle", s)
        self.assertIn("Led programs.", s)

    def test_pipeline_output_persona_free(self):
        def alias_turn(prompt):
            return True, "[ok=True]", W("NAME (Acme Alias)\n"
                                         "AcmeNet: @sample.acme-handle.example\n"
                                         + GOOD_RESUME)
        notes = []
        tele = cp.run_packets(REPORT, alias_turn, notes, send=False)
        self.assertEqual(tele["packets"], 1)
        mem = cp.load_app_mem()
        pdir = mem["https://a.com/careers/1"]["packet_dir"]
        txt = open(os.path.join(pdir, "resume.txt")).read().lower()
        self.assertNotIn("acme alias", txt)
        self.assertNotIn("acmenet", txt)
        self.assertNotIn("acme-handle", txt)
    def test_persona_tokens_missing_is_visible(self):
        self.assertEqual(cp._load_persona_tokens(os.path.join(self.tmp, "nope.txt")), ())
        saved = cp.PERSONA_TOKENS
        cp.PERSONA_TOKENS = ()
        try:
            notes = []
            tele = cp.run_packets(REPORT, lambda p: (True, "[ok=True]", W(GOOD_RESUME)), notes, send=False)
            self.assertEqual(tele["packets"], 1)
            self.assertTrue(any(n.startswith("persona scrub inactive") for n in notes), notes)
        finally:
            cp.PERSONA_TOKENS = saved
    def test_persona_tokens_file_loads(self):
        p = os.path.join(self.tmp, "tok.txt")
        open(p, "w").write("# c\nAlpha\n\n beta \n")
        self.assertEqual(cp._load_persona_tokens(p), ("alpha", "beta"))

    def test_state_machine_guard(self):
        mem = {}
        with self.assertRaises(ValueError):
            cp.set_state(mem, "u", "NOT_A_STATE")




class TestEmailOrdering(Base):
    def _pkt(self, co, role, txt):
        return {"meta": {"company": co, "role": role, "fit": "good",
                         "salary": "UNKNOWN",
                         "apply_url": "https://%s.com/j" % co.lower(),
                         "artifacts": {"resume_pdf": "/nonexistent.pdf"},
                         "review_warnings": []},
                "job": {"why": "w"}, "resume_text": txt}

    def test_links_cluster_before_resume_texts(self):
        # CR 2026-08-18: summary blocks with APPLY links first, then all
        # resume texts under a single FULL RESUME TEXTS section.
        pkts = [self._pkt("Alpha", "R1", "RESUME-ALPHA"),
                self._pkt("Beta", "R2", "RESUME-BETA")]
        _, body, _ = cp.build_email(pkts, [], "2026-08-18")
        marker = body.index("FULL RESUME TEXTS")
        for u in ("https://alpha.com/j", "https://beta.com/j"):
            self.assertLess(body.index("APPLY: " + u), marker)
        for t in ("RESUME-ALPHA", "RESUME-BETA"):
            self.assertGreater(body.index(t), marker)
        self.assertEqual(body.count("RESUME-ALPHA"), 1)


# ---- Han outbound-link verification gate (2026-08-27) ----
def _stub_verify(mapping, default=("LIVE", 200)):
    """Return a verify_apply_url stub driven by an explicit url->status map."""
    def _v(url, **_kw):
        return mapping.get(url, default)
    return _v


VERIFY_REPORT = """WORTHWHILE: 3
SELECTED_JSON: [
 {"url": "https://live.com/careers/1", "company": "LiveCo", "role": "Program Manager",
  "description": "run programs", "fit": "28/35", "comp": "UNKNOWN", "why": "fits"},
 {"url": "https://dead.com/careers/2", "company": "DeadCo", "role": "PM",
  "description": "x", "fit": "27/35", "comp": "UNKNOWN", "why": "gone"},
 {"url": "https://blocked.com/careers/3", "company": "BlockedCo", "role": "PM",
  "description": "y", "fit": "26/35", "comp": "UNKNOWN", "why": "cloudflare"}
]
"""


def _good_turn(prompt):
    return (True, "[agent=han] ok", W(GOOD_RESUME))


class _FakeResp:
    def __init__(self, body, url, ctype="text/html; charset=utf-8",
                 status=200):
        self._b = body.encode("utf-8") if isinstance(body, str) else body
        self._u = url
        self.status = status
        self.headers = {"Content-Type": ctype}

    def geturl(self):
        return self._u

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def open(self, req, timeout=None):
        if self.exc:
            raise self.exc
        return self.resp


GOOD_URL = "https://boards.greenhouse.io/goodco/jobs/123"
GOOD_TITLE = "Program Manager"
FILLER = "<p>" + ("lorem ipsum " * 200) + "</p>"


def _good_page(extra="", apply_tag='<a href="/apply">Apply now</a>'):
    return ("<html><head><title>Program Manager - GoodCo</title></head>"
            "<body><h1>Program Manager</h1>" + FILLER + extra + apply_tag +
            "</body></html>")


class TestOptionBVerify(unittest.TestCase):
    """verify_apply_url classifier. VERIFIED must mean an actionable
    posting; HTTP 200 alone is UNVERIFIED; closed evidence is DEAD."""

    def _v(self, body, url=GOOD_URL, final=None, title=GOOD_TITLE,
           ctype="text/html; charset=utf-8"):
        op = _FakeOpener(_FakeResp(body, final or url, ctype=ctype))
        return cp.verify_apply_url(url, _opener=op, title=title)

    def test_bare_200_is_unverified(self):
        st, code, why = self._v("ok", url="https://anywhere.example/x")
        self.assertEqual((st, code), ("UNVERIFIED", 200))

    def test_full_posting_is_verified(self):
        st, code, why = self._v(_good_page())
        self.assertEqual(st, "VERIFIED", why)

    def test_closed_marker_is_dead_even_on_200(self):
        st, code, why = self._v(_good_page(
            extra="<p>This position has been filled.</p>"))
        self.assertEqual((st, code), ("DEAD", 200))

    def test_challenge_page_unverified(self):
        st, _, why = self._v(_good_page(
            extra="Enable JavaScript and cookies to continue"))
        self.assertEqual(st, "UNVERIFIED")
        self.assertIn("challenge", why)

    def test_aggregator_host_unverified(self):
        u = "https://www.ziprecruiter.com/c/GoodCo/Job/Program-Manager/-in-x"
        st, _, why = self._v(_good_page(), url=u)
        self.assertEqual(st, "UNVERIFIED")
        self.assertIn("host", why)

    def test_employer_careers_path_ok(self):
        st, _, why = self._v(_good_page(),
                             url="https://goodco.com/careers/123")
        self.assertEqual(st, "VERIFIED", why)

    def test_title_mismatch_unverified(self):
        st, _, why = self._v(_good_page(), title="Data Scientist")
        self.assertEqual(st, "UNVERIFIED")
        self.assertIn("title", why)

    def test_missing_title_unverified(self):
        st, _, why = self._v(_good_page(), title="")
        self.assertEqual(st, "UNVERIFIED")

    def test_no_apply_affordance_unverified(self):
        st, _, why = self._v(_good_page(apply_tag=""))
        self.assertEqual(st, "UNVERIFIED")
        self.assertIn("apply", why)

    def test_non_html_unverified(self):
        st, _, why = self._v(_good_page(), ctype="application/pdf")
        self.assertEqual(st, "UNVERIFIED")

    def test_redirect_to_index_dead(self):
        st, _, why = self._v(_good_page(),
                             url="https://goodco.com/careers/123",
                             final="https://goodco.com/careers")
        self.assertEqual(st, "DEAD")

    def test_http_404_410_dead_403_429_unverified(self):
        import urllib.error
        for code, want in ((404, "DEAD"), (410, "DEAD"),
                           (403, "UNVERIFIED"), (429, "UNVERIFIED")):
            exc = urllib.error.HTTPError(GOOD_URL, code, "x", {}, None)
            st, got, _ = cp.verify_apply_url(
                GOOD_URL, _opener=_FakeOpener(exc=exc), title=GOOD_TITLE)
            self.assertEqual((st, got), (want, code))

    def test_apply_tag_past_512kb_still_seen(self):
        big = "<style>" + ("x" * (600 * 1024)) + "</style>"
        st, _, why = self._v(_good_page(extra=big))
        self.assertEqual(st, "VERIFIED", why)

    def test_fetch_error_unverified(self):
        st, code, why = cp.verify_apply_url(
            GOOD_URL, _opener=_FakeOpener(exc=OSError("boom")),
            title=GOOD_TITLE)
        self.assertEqual((st, code), ("UNVERIFIED", 0))


class TestOptionBGate(Base):
    """run_packets consumes the 3-tuple: counts VERIFIED, surfaces reason."""

    def test_three_tuple_reason_reaches_notes(self):
        notes = []
        orig = cp.verify_apply_url
        cp.verify_apply_url = lambda url, **kw: (
            ("UNVERIFIED", 200, "title tokens 0/2")
            if "blocked.com" in url else ("VERIFIED", 200, "posting actionable"))
        try:
            tele = cp.run_packets(VERIFY_REPORT, _good_turn, notes,
                                  send=False, verify_urls=True)
        finally:
            cp.verify_apply_url = orig
        self.assertEqual(tele["verified_ok"], 2)
        self.assertEqual(tele["verified_unverified"], 1)
        self.assertTrue(any("title tokens 0/2" in n for n in notes), notes)


class TestLaneParams(unittest.TestCase):
    """CAREER_LANE reaches subject and body; default strings unchanged."""

    def test_default_lane_strings_unchanged(self):
        if os.environ.get("CAREER_LANE"):
            self.skipTest("CAREER_LANE set in environment")
        subj, body, att = cp.build_email([], [], "2026-09-04")
        self.assertTrue(subj.startswith("Career Hunter packets 2026-09-04"), subj)
        self.assertTrue(body.startswith(
            "CAREER HUNTER APPLICATION PACKETS -- 2026-09-04"), body[:60])

    def test_lane_override_reaches_subject_and_body(self):
        orig = cp.LANE
        cp.LANE = "BILL-PAYER PATROL"
        try:
            subj, body, att = cp.build_email([], [], "2026-09-04")
        finally:
            cp.LANE = orig
        self.assertTrue(subj.startswith("Bill-Payer Patrol packets"), subj)
        self.assertIn("BILL-PAYER PATROL APPLICATION PACKETS", body)


class TestVerifyGate(Base):
    """Han-only outbound apply-URL liveness gate."""

    def _run(self, mapping, verify_urls):
        import time as _t
        notes = []
        orig = cp.verify_apply_url
        cp.verify_apply_url = _stub_verify(mapping)
        try:
            tele = cp.run_packets(VERIFY_REPORT, _good_turn, notes,
                                  send=False, verify_urls=verify_urls)
        finally:
            cp.verify_apply_url = orig
        day = _t.strftime("%Y-%m-%d", _t.gmtime())
        with open(os.path.join(cp.CAREER_ROOT, "reports",
                               "email-%s.txt" % day)) as f:
            email = f.read()
        return tele, email, notes

    def test_dead_link_omitted_and_counted(self):
        m = {"https://dead.com/careers/2": ("DEAD", 404)}
        tele, email, _ = self._run(m, verify_urls=True)
        self.assertEqual(tele["verified_dead"], 1)
        self.assertNotIn("dead.com/careers/2", email)
        self.assertIn("live.com/careers/1", email)

    def test_redirect_to_index_is_dead(self):
        m = {"https://dead.com/careers/2": ("DEAD", 200)}
        tele, email, _ = self._run(m, verify_urls=True)
        self.assertEqual(tele["verified_dead"], 1)
        self.assertNotIn("dead.com/careers/2", email)

    def test_unverified_shown_with_marker(self):
        m = {"https://blocked.com/careers/3": ("UNVERIFIED", 403)}
        tele, email, _ = self._run(m, verify_urls=True)
        self.assertEqual(tele["verified_unverified"], 1)
        self.assertIn("blocked.com/careers/3", email)
        self.assertIn("link not verified", email)

    def test_live_shown_unmarked(self):
        tele, email, _ = self._run({}, verify_urls=True)
        self.assertEqual(tele["verified_dead"], 0)
        self.assertEqual(tele["verified_unverified"], 0)
        self.assertIn("live.com/careers/1", email)
        self.assertNotIn("link not verified", email)

    def test_verify_off_does_nothing(self):
        m = {"https://dead.com/careers/2": ("DEAD", 404)}
        tele, email, _ = self._run(m, verify_urls=False)
        self.assertEqual(tele["verified_dead"], 0)
        self.assertIn("dead.com/careers/2", email)



# --- Clark resume output defect (operator ruling 2026-10-13): R1-R12 --------
# Synthetic applicant only (CORPUS_RESUME above). Each pass case is rendered
# to PDF and the PDF text is extracted and checked.

def _email():
    import time as _t
    return open(os.path.join(cp.CAREER_ROOT, "reports", "email-%s.txt"
                             % _t.strftime("%Y-%m-%d", _t.gmtime()))).read()


def _pdf_text(path):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


class TestResumeOutputContract(Base):
    def _run(self, reply):
        notes = []
        tele = cp.run_packets(REPORT, lambda p: (True, "[ok=True]", reply),
                              notes, send=False)
        rec = cp.load_app_mem()["https://a.com/careers/1"]
        return tele, rec, notes

    def _assert_clean_packet(self, reply, banned=()):
        tele, rec, notes = self._run(reply)
        self.assertEqual((tele["packets"], tele["blocked"]), (1, 0), rec)
        self.assertEqual(rec["state"], "PACKET_READY")
        txt = open(os.path.join(rec["packet_dir"], "resume.txt")).read()
        self.assertEqual(txt.strip(), GOOD_RESUME.strip())
        pdf = " ".join(_pdf_text(os.path.join(rec["packet_dir"],
                                              "resume.pdf")).split())
        self.assertIn("Senior Technical Product Owner, Acme Insurance 2019-2024", pdf)
        self.assertIn("claims cycle time 22%", pdf)
        for b in banned:
            self.assertNotIn(b.lower(), (txt + pdf).lower())
        return notes

    def _assert_blocked(self, reply, needle):
        """v2 (operator 2026-10-13: a usable resume every time): every tailored
        attempt is rejected and never published; the operator's base resume is
        delivered instead; the rule and matched text reach the email notes."""
        tele, rec, notes = self._run(reply)
        self.assertEqual((tele["packets"], tele["fallback"], tele["blocked"]), (1, 1, 0), rec)
        self.assertEqual(tele["tailor_rejected"], cp.TAILOR_ATTEMPTS)
        self.assertEqual(rec["state"], "PACKET_READY")
        self.assertTrue(rec.get("base_resume_fallback"))
        self.assertTrue(any(needle in v for v in rec["tailor_blocked"]), rec["tailor_blocked"])
        txt = open(os.path.join(rec["packet_dir"], "resume.txt")).read()
        self.assertEqual(txt.strip(), cp.scrub_persona(CORPUS_RESUME).strip())
        pdf = " ".join(_pdf_text(os.path.join(rec["packet_dir"], "resume.pdf")).split())
        self.assertIn("Senior Technical Product Owner, Acme Insurance 2019-2024", pdf)
        email = _email()
        self.assertIn("'s notes", email)
        self.assertIn(needle, email)
        self.assertIn("base resume is attached instead", email)
        return notes

    def test_R1_clean_output_pdf(self):
        notes = self._assert_clean_packet(W(GOOD_RESUME))
        self.assertFalse([n for n in notes if "tailor notes" in n])

    def test_R2_commentary_top_routed_to_notes(self):
        notes = self._assert_clean_packet(
            W(GOOD_RESUME, before="Honest flag before the resume: the role "
              "wants 10 years of biotech. Fit is weak.\n---\n"),
            banned=("honest flag", "biotech"))
        self.assertTrue(any("tailor notes" in n and "Honest flag" in n
                            for n in notes), notes)

    def test_R3_commentary_bottom_routed_to_notes(self):
        notes = self._assert_clean_packet(
            W(GOOD_RESUME, after="Note: I emphasized program delivery. "
              "Mission selection did not resolve; which mission applies?\n"),
            banned=("mission selection", "emphasized"))
        self.assertTrue(any("Mission selection" in n for n in notes), notes)

    def test_R4_commentary_middle_blocks(self):
        mid = GOOD_RESUME.replace(
            "EXPERIENCE:", "Note: I kept the dates exactly as the corpus states.\nEXPERIENCE:")
        self._assert_blocked(W(mid), "note-label")

    def test_R5_ai_disclosure_blocks(self):
        self._assert_blocked(W(GOOD_RESUME + "This resume is AI-generated.\n"),
                             "ai-disclosure")

    def test_R6_placeholder_blocks(self):
        self._assert_blocked(W(GOOD_RESUME.replace(
            "Sample Person", "Sample Person\n[Phone] | [Email]")), "placeholder")

    def test_R7_drafting_instruction_blocks(self):
        self._assert_blocked(W(GOOD_RESUME + "Insert your portfolio link "
                               "(optional).\n"), "drafting-instruction")

    def test_R8_invented_employer_blocks(self):
        self._assert_blocked(W(GOOD_RESUME + "Program Lead, Globex Dynamics "
                               "2019-2024\n"), "history fact not in corpus: Globex")

    def test_R9_invented_degree_blocks(self):
        self._assert_blocked(W(GOOD_RESUME + "MBA, State University\n"),
                             "degree not in corpus: mba")

    def test_R10_missing_markers_blocks_and_routes(self):
        notes = self._assert_blocked(GOOD_RESUME, "output contract")
        self.assertTrue(any("tailor notes" in n for n in notes), notes)

    def test_R11_declined_blocks_with_reason_routed(self):
        notes = self._assert_blocked(
            W("", after="I can't produce an honest resume for this role: "
              "nothing in the corpus matches its requirements.\n"),
            "no resume produced")
        self.assertTrue(any("can't produce" in n for n in notes), notes)

    def test_R13_preamble_inside_markers_blocks(self):
        self._assert_blocked(W("Here is the tailored version:\n" + GOOD_RESUME),
                             "preamble")

    def test_R14_code_fence_blocks(self):
        self._assert_blocked(W("```\n" + GOOD_RESUME + "```\n"), "code-fence")

    def test_R15_meta_commentary_without_label_blocks(self):
        self._assert_blocked(W(GOOD_RESUME + "I emphasized delivery for the role.\n"),
                             "meta-commentary")

    def test_R17_invented_title_blocks(self):
        self._assert_blocked(W(GOOD_RESUME.replace(
            "Senior Technical Product Owner, Acme Insurance",
            "Program Director, Acme Insurance")), "history fact not in corpus: Program Director")

    def test_R16_applicant_ai_vocabulary_from_corpus_passes(self):
        # 'as an AI' inside the applicant's own corpus line is not a disclosure
        line = "Reviewed vendor models as an AI governance lead; large language model triage.\n"
        with open(cp.RESUME_PATH, "a") as fh:
            fh.write(line)
        tele, rec, notes = self._run(W(GOOD_RESUME + line))
        self.assertEqual((tele["packets"], tele["blocked"]), (1, 0), rec)
        self.assertEqual(cp.lint_resume("I am an AI. " + line, CORPUS_RESUME + line),
                         ["non-resume content (ai-disclosure): I am an AI"])

    def test_R12_unit_rules_and_no_content_added(self):
        body, notes, prob = cp.extract_resume(W(GOOD_RESUME, "pre\n", "post\n"))
        self.assertEqual((body, notes, prob), (GOOD_RESUME.strip("\n"), "pre\npost", []))
        self.assertEqual(cp.lint_resume(GOOD_RESUME), [])
        self.assertEqual(cp.verify_history(GOOD_RESUME, CORPUS_RESUME + CORPUS_PROFILE), [])
        for name, rx in cp.LINT_RULES:
            self.assertTrue(name)
        # dates exactly as the corpus states them pass; a changed range blocks
        # (the number check), and a history phrase from the job text does not
        # count as corpus.
        bad = GOOD_RESUME.replace("Acme Insurance 2019-2024", "GoodCo 2019-2024")
        self.assertTrue(cp.verify_history(bad, CORPUS_RESUME + CORPUS_PROFILE))
        self.assertIn("RESUME BEGIN", cp.tailor_prompt(
            {"resume": "r", "profile": "p"},
            {"company": "c", "role": "r", "url": "u", "description": "d"}))

# --- Clark resume contamination v2 (operator ruling 2026-10-13, priority override) ---
# Synthetic applicant only. OPERATOR_EXAMPLE reproduces the SHAPE of the operator's
# contaminated example (box, masked: evidence box/example-masked.txt): name+contact,
# an "One honest flag" paragraph about the corpus, "Resume follows", a separator, the
# name+contact block repeated, then the resume. No operator data is in it.
CONTACT = "Boston | (555) 010-0100 | sample.person@example.com"
CLEAN_V2 = "Sample Person\n" + CONTACT + "\n\nSUMMARY\nProgram leader; led cross-functional delivery of 3 programs.\n" \
    "Improved claims cycle time 22%.\n\nEXPERIENCE\nSenior Technical Product Owner, Acme Insurance 2019-2024\n\n" \
    "EDUCATION\nMS Project Management, State University\n"
OPERATOR_EXAMPLE = ("Sample Person\n" + CONTACT + "\n\nOne honest flag before the resume: this role has two hard "
    "requirements the corpus cannot meet. The corpus documents program delivery, not the platform work the role "
    "centres on. A hiring manager screening for hands-on platform experience will notice the gap.\n\n"
    "Resume follows, limited to corpus facts.\n\n---\n\n" + CLEAN_V2)
PREFACE = "Here is your tailored resume. Honest flag before the resume, because the hard rules require it: " \
    "fit is weak.\n---\n" + CLEAN_V2
REFUSAL = "I can't produce an honest resume for this role. The corpus has no platform engineering, and " \
    "inventing it would break the hard rules. Recommendation: skip this posting.\n"
MISSION = "Mission selection did not resolve -- which mission applies? Proceeding with the resume.\n" + CLEAN_V2
DUP_HEADER = CLEAN_V2 + "\nSample Person\nSUMMARY\nProgram leader.\n"
DUP_CONTACT = CLEAN_V2.replace("EDUCATION", "Contact: sample.person@example.com\nEDUCATION")


class TestDeliverableV2(Base):
    def setUp(self):
        super().setUp()
        with open(cp.RESUME_PATH, "a") as fh:
            fh.write(CONTACT + "\n")      # the synthetic applicant's own contact line

    def _run(self, replies):
        seen = []
        def turn(prompt):
            seen.append(prompt)
            r = replies[min(len(seen), len(replies)) - 1]
            return True, "[ok=True]", r
        notes = []
        tele = cp.run_packets(REPORT, turn, notes, send=False)
        return tele, cp.load_app_mem()["https://a.com/careers/1"], notes, seen

    def _rendered(self, text):
        """Write a GenerationResult carrying `text` straight through the rendered
        gate (bypassing every pre-render check): returns (violations, pdir)."""
        pdir = cp.packet_dir("2026-10-13", "GoodCo", "Program Manager")
        job = {"company": "GoodCo", "role": "Program Manager", "url": "u", "description": "d"}
        meta, bad = cp.write_packet(pdir, job, cp.GenerationResult(resume_text=text), [], "t",
                                    CORPUS_RESUME + CONTACT + "\n" + CORPUS_PROFILE)
        return bad, pdir

    def _assert_rendered_block(self, text, needle):
        bad, pdir = self._rendered(text)
        self.assertTrue(any(v.startswith("rendered resume.txt") and needle in v for v in bad), bad)
        self.assertTrue(any(v.startswith("rendered resume.pdf") and needle in v for v in bad), bad)
        self.assertFalse(os.path.exists(pdir), "nothing deliverable may remain")

    # rule 1: structure
    def test_V1_result_object_and_writer_contract(self):
        r = cp.build_result(W(CLEAN_V2, "Fit is weak for this one.\n", "Dates kept as listed.\n"),
                            None, CORPUS_RESUME + CONTACT + "\n" + CORPUS_PROFILE,
                            {"description": "run programs", "title": "PM"})
        self.assertIsInstance(r, cp.GenerationResult)
        self.assertEqual(r.resume_text.strip(), CLEAN_V2.strip())
        self.assertEqual(r.problems, [])
        self.assertTrue(r.fit_assessment and r.notes, r)
        with self.assertRaises(TypeError):
            cp.write_packet(self.tmp, {}, CLEAN_V2, [], "t")

    # rule 2: notes go to the email, never the deliverable
    def test_V2_notes_in_email_never_in_deliverable(self):
        reply = W(CLEAN_V2, "Fit assessment: strong match on delivery, thin on platform.\n\n",
                  "\nHonest flag: the corpus has no platform work.\n\nMission note: none selected.\n")
        tele, rec, notes, _ = self._run([reply])
        self.assertEqual((tele["packets"], tele["fallback"]), (1, 0), rec)
        dl = (open(os.path.join(rec["packet_dir"], "resume.txt")).read() + " " +
              _pdf_text(os.path.join(rec["packet_dir"], "resume.pdf"))).lower()
        for frag in ("strong match", "honest flag", "mission note", "platform"):
            self.assertNotIn(frag, dl)
        email = _email()
        sect = email[email.index("'s notes"):]
        self.assertIn("Fit assessment: Fit assessment: strong match on delivery", sect)
        self.assertIn("Warnings: Honest flag: the corpus has no platform work.", sect)
        self.assertIn("Notes: Mission note: none selected.", sect)
        meta = json.load(open(os.path.join(rec["packet_dir"], "metadata.json")))
        subj, body, att = cp.build_email([{"job": {"why": ""}, "meta": meta, "resume_text": "",
                                           "gen": cp.GenerationResult(notes=["N-ONLY-IN-EMAIL"])}], [], "d")
        self.assertEqual(len(att), 1)
        self.assertIn("N-ONLY-IN-EMAIL", body)
        import base64
        self.assertNotIn(b"N-ONLY-IN-EMAIL", base64.b64decode(att[0]["content"]))

    def test_V3_agent_label_clark(self):
        old = cp.CAREER_ROOT
        try:
            cp.CAREER_ROOT = "/x/shared/career_clark"
            self.assertEqual(cp.notes_block(cp.GenerationResult(notes=["n"]))[0][:13], "Clark's notes")
        finally:
            cp.CAREER_ROOT = old

    # rule 4: blocklist phrases, case-insensitive, no corpus allowance
    def test_V4_blocklist_phrases_each(self):
        for ph in ("One honest flag", "HONEST FLAG", "the corpus", "The Operator", "Resume follows",
                   "resume follows", "R\u00e9sum\u00e9 follows"):
            v = cp.lint_resume(CLEAN_V2 + ph + " here.\n", CORPUS_RESUME)
            self.assertTrue(any("(blocklist-phrase)" in x for x in v), (ph, v))

    def test_V5_blocklist_no_corpus_allowance(self):
        corpus = CORPUS_RESUME + "Worked with the operator team; the corpus of claims.\n"
        v = cp.lint_resume(CLEAN_V2 + "Worked with the operator team.\n", corpus)
        self.assertTrue(any("(blocklist-phrase)" in x for x in v), v)
        # the AI-vocabulary allowance (the 292 false positives) is kept
        line = "Reviewed vendor models as an AI governance lead.\n"
        self.assertEqual(cp.lint_resume(CLEAN_V2 + line, CORPUS_RESUME + line), [])

    def test_V6_warning_fit_editorial_instruction_rules(self):
        cases = (("Warning: dates approximate.", "warning"), ("Fit score: 7/10", "fit-score"),
                 ("Strong match for this posting.", "fit-score"), ("I would lead with delivery.", "editorial"),
                 ("To be honest, the platform work is thin.", "editorial"),
                 ("This role requires Kubernetes.", "editorial"),
                 ("Before you send this, double-check the dates.", "operator-instruction"),
                 ("You should mention the certification.", "operator-instruction"))
        for line, rule in cases:
            v = cp.lint_resume(CLEAN_V2 + line + "\n", CORPUS_RESUME)
            self.assertTrue(any("(%s)" % rule in x for x in v), (line, v))

    # rule 5: duplicates
    def test_V7_duplicate_name_contact_heading(self):
        self.assertEqual(cp.check_duplicates(CLEAN_V2), [])
        self.assertTrue(any("name line" in v for v in cp.check_duplicates(DUP_HEADER)))
        self.assertTrue(any("duplicate section heading: SUMMARY" in v for v in cp.check_duplicates(DUP_HEADER)))
        self.assertTrue(any("duplicate contact block (token" in v for v in cp.check_duplicates(DUP_CONTACT)))
        self.assertTrue(any("duplicate contact block (line" in v for v in cp.check_duplicates(
            CLEAN_V2 + CONTACT + "\n")))
        two_roles = CLEAN_V2.replace("EDUCATION", "Acme Insurance\nProduct Owner 2019\nAcme Insurance\nAnalyst 2019\nEDUCATION")
        self.assertEqual(cp.check_duplicates(two_roles), [])   # a repeated employer is legitimate

    # rule 6: rendered validation, both artifacts, each pattern
    def test_V8_rendered_operator_example_blocked_pdf_and_txt(self):
        self._assert_rendered_block(OPERATOR_EXAMPLE, "blocklist-phrase")
        self._assert_rendered_block(OPERATOR_EXAMPLE, "duplicate header (name line)")

    def test_V9_rendered_preface_blocked_pdf_and_txt(self):
        self._assert_rendered_block(PREFACE, "blocklist-phrase")

    def test_V10_rendered_refusal_blocked_pdf_and_txt(self):
        self._assert_rendered_block(REFUSAL, "meta-commentary")

    def test_V11_rendered_mission_narration_blocked_pdf_and_txt(self):
        self._assert_rendered_block(MISSION, "Mission selection")

    def test_V12_rendered_duplicate_header_blocked_pdf_and_txt(self):
        self._assert_rendered_block(DUP_HEADER, "duplicate header (name line)")

    def test_V13_rendered_duplicate_contact_blocked_pdf_and_txt(self):
        self._assert_rendered_block(DUP_CONTACT, "duplicate contact block")

    def test_V14_rendered_clean_passes_and_files_final(self):
        bad, pdir = self._rendered(CLEAN_V2)
        self.assertEqual(bad, [])
        self.assertEqual(sorted(os.listdir(pdir)), ["job.txt", "metadata.json", "resume.pdf", "resume.txt"])

    def test_V15_unreadable_pdf_fails_closed(self):
        orig = cp.pdf_text
        def broken(path):
            raise ValueError("broken pdf")
        try:
            cp.pdf_text = broken
            bad, pdir = self._rendered(CLEAN_V2)
        finally:
            cp.pdf_text = orig
        self.assertTrue(any("resume.pdf unreadable" in v for v in bad), bad)
        self.assertFalse(os.path.exists(pdir))

    # rules 7 + usability: retry with feedback, then the base resume; full block only if that fails
    def test_V16_retry_succeeds_with_feedback(self):
        tele, rec, notes, prompts = self._run([OPERATOR_EXAMPLE, W(CLEAN_V2)])
        self.assertEqual((tele["packets"], tele["fallback"], tele["tailor_rejected"]), (1, 0, 1), rec)
        self.assertNotIn("base_resume_fallback", rec)
        self.assertEqual(open(os.path.join(rec["packet_dir"], "resume.txt")).read().strip(), CLEAN_V2.strip())
        self.assertIn("PREVIOUS ATTEMPT WAS REJECTED", prompts[1])
        self.assertIn("output contract", prompts[1])
        self.assertIn("Tailored attempt 1 rejected", _email())

    def test_V17_operator_example_pipeline_never_published(self):
        tele, rec, notes, _ = self._run([OPERATOR_EXAMPLE])
        self.assertEqual((tele["packets"], tele["fallback"]), (1, 1))
        dl = (open(os.path.join(rec["packet_dir"], "resume.txt")).read() + " " +
              _pdf_text(os.path.join(rec["packet_dir"], "resume.pdf"))).lower()
        for frag in ("honest flag", "corpus", "resume follows", "hiring manager"):
            self.assertNotIn(frag, dl)
        self.assertTrue(any("REJECTED" in n for n in notes))

    def test_V18_base_resume_also_bad_is_generation_blocked(self):
        open(cp.RESUME_PATH, "w").write(CORPUS_RESUME + "Sample Person\n")   # base has a duplicate name line
        tele, rec, notes, _ = self._run([REFUSAL])
        self.assertEqual((tele["packets"], tele["blocked"]), (0, 1))
        self.assertEqual(rec["state"], "GENERATION_BLOCKED")
        self.assertFalse(os.path.isdir(cp.LOCKBOX) and any(
            f.startswith("resume") for _r, _d, fs in os.walk(cp.LOCKBOX) for f in fs))
        email = _email()
        self.assertIn("GENERATION_BLOCKED", email)
        sect = email[email.index("'s notes"):]
        self.assertIn("Base resume rejected: duplicate header (name line)", sect)
        self.assertIn("Warnings: I can't produce an honest resume", email)

    # rule 9: factual accuracy unchanged; nothing added
    def test_V19_fallback_adds_nothing(self):
        tele, rec, notes, _ = self._run([REFUSAL])
        txt = open(os.path.join(rec["packet_dir"], "resume.txt")).read()
        self.assertEqual(txt.strip(), cp.scrub_persona(open(cp.RESUME_PATH).read()).strip())
        self.assertEqual(cp.verify_history(GOOD_RESUME.replace("Acme Insurance", "Globex Dynamics"),
                                           CORPUS_RESUME), ["history fact not in corpus: Globex Dynamics"])

    # operator ruling 2026-10-13 (v2 fallback condition): UNTAILORED label; operator email only
    def test_V21_fallback_labelled_untailored_tailored_not(self):
        tele, rec, notes, _ = self._run([REFUSAL])
        self.assertEqual(tele["fallback"], 1)
        meta = json.load(open(os.path.join(rec["packet_dir"], "metadata.json")))
        pk = {"job": {"why": ""}, "meta": meta, "resume_text": "x", "gen": cp.GenerationResult(), "fallback": True}
        subj, body, att = cp.build_email([pk], [], "d")
        self.assertTrue(att[0]["filename"].startswith("UNTAILORED-"), att[0]["filename"])
        self.assertIn("UNTAILORED (base resume, not tailored", body.split("\n")[4])   # the job's section header
        self.assertIn("'s notes (for you only; not in the resume) -- UNTAILORED resume attached:", body)
        self.assertIn("has not been sent to the employer", body)
        self.assertEqual(body.count("UNTAILORED (base resume"), 2)                    # section header + full-text header
        pk["fallback"] = False
        subj, body, att = cp.build_email([pk], [], "d")
        self.assertNotIn("UNTAILORED", body + att[0]["filename"])

    def test_V22_only_destination_is_the_operator_email(self):
        import urllib.request as ur
        sent = []
        class _R:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b'{"id": "stub"}'
        def fake_urlopen(req, timeout=None):
            sent.append((req.full_url, json.loads(req.data.decode())))
            return _R()
        env = {"RESEND_API_KEY": "k", "RESEND_FROM": "clark@example.invalid", "DIGEST_TO": "operator@example.invalid"}
        old_env = {k: os.environ.get(k) for k in env}
        old = ur.urlopen
        try:
            os.environ.update(env)
            ur.urlopen = fake_urlopen
            notes = []
            tele = cp.run_packets(REPORT, lambda p_: (True, "[ok=True]", REFUSAL), notes, send=True)
        finally:
            ur.urlopen = old
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual((tele["fallback"], tele["email"]), (1, "sent:stub"))
        self.assertEqual(len(sent), 1, sent)
        url, payload = sent[0]
        self.assertEqual(url, cp.RESEND_URL)
        self.assertEqual(payload["to"], ["operator@example.invalid"])
        self.assertEqual([a["filename"][:11] for a in payload["attachments"]], ["UNTAILORED-"])

    def test_V20_prompt_forbids_phrases(self):
        pr = cp.tailor_prompt({"resume": "r", "profile": "p"},
                              {"company": "c", "role": "r", "url": "u", "description": "d"})
        for ph in ("honest flag", "the corpus", "the operator", "resume follows"):
            self.assertIn(ph, pr)
        self.assertNotIn("PREVIOUS ATTEMPT", pr)


if __name__ == "__main__":
    unittest.main()
