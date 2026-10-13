#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
career_packets.py -- application-packet stage for the career hunter.

Vertical slice (operator spec 2026-08-17):
  selected job -> factually tailored resume text (governed Han turn)
  -> deterministic FACTUALITY VERIFICATION (fail-closed)
  -> PDF (reportlab) -> lockbox packet dir -> one digest email (Resend,
  apply link + full resume text + PDF attachments) -> telemetry.

FACTUALITY LAYER (designed first, per operator ruling):
  The tailoring prompt permits only selection/ordering/emphasis/truthful
  reframing of facts already in the operator corpus (resume.txt +
  career_profile.txt). After generation, verify_factuality() runs a
  deterministic check; a resume that fails is BLOCKED: no PDF, no
  lockbox resume, and the failure is named in email + telemetry.
    FAIL-CLOSED checks:
      - numbers: every numeric token in the resume must appear in the
        corpus (comparative metrics, dollar figures, team sizes, years).
        The current year and the job's own posted numbers are allowed.
      - credentials: watchlist tokens (PMP, MBA, PhD, certs, clearances)
        appearing in the resume but not the corpus.
    SURFACED-FOR-REVIEW (never blocks): capitalized proper-noun phrases
      absent from both corpus and job text -> review_warnings in
      metadata and email.

APPLICATION MEMORY states:
  DISCOVERED SELECTED PACKET_READY SENT_TO_OPERATOR APPLIED REJECTED
  CLOSED SUPPRESSED GENERATION_BLOCKED
  Packet generation never marks APPLIED. Regeneration is suppressed for
  urls in PACKET_READY/SENT_TO_OPERATOR/APPLIED/REJECTED/CLOSED/
  SUPPRESSED. Operator transitions (APPLIED etc.) are via the CLI:
    career_packets.py mark <url> <state>

OUTPUT CONTRACT (Clark resume output defect, operator ruling 2026-10-13):
  The tailoring turn must return the resume between RESUME_BEGIN and
  RESUME_END. Anything outside the markers (fit concerns, honest flags,
  mission notes, questions) is ROUTED to the run's notes -- Clark's log --
  and never reaches the deliverable. A reply without exactly one marker
  pair, an empty resume (the model declined), or a resume that fails
  lint_resume() (commentary, AI disclosure, placeholders, drafting
  instructions) or verify_history() (an employer, title or degree on a
  history line that is not in the corpus) is GENERATION_BLOCKED: no PDF,
  no lockbox resume. Nothing is added to the resume; text is only removed
  or routed.

DELIVERABLE CONTRACT v2 (Clark resume contamination, operator ruling
2026-10-13, priority override; "a resume I can use each time, the first
time, no placeholders, no comments"):
  - The generation result is a GenerationResult with separate fields
    resume_text / fit_assessment / warnings / notes / problems. Only
    resume_text reaches a renderer: write_packet() takes the result object,
    never a reply string.
  - Every fit assessment, warning, flag, note or remark goes to the
    operator in that job's section of the email, labelled "<Agent>'s
    notes", never into the resume.
  - The deliverable is validated AFTER rendering: the written resume.txt and
    the text extracted from the written resume.pdf both pass the blocklist
    (LINT_RULES, including the literal phrases, which no corpus allowance
    excuses) and the duplicate name/contact/section-heading check. Only then
    are the files moved to their final names and the packet marked ready.
  - A rejected attempt is never published. The turn is retried with the
    rejection reasons (CAREER_TAILOR_ATTEMPTS, default 3). If every attempt
    is rejected, the operator's own base resume (resume.txt, persona
    scrubbed) is rendered and validated by the same gate and delivered
    instead, and the email says so, with each attempt's rule and matched
    text. Only if that also fails is the job GENERATION_BLOCKED (no PDF).
  - Factuality (numbers, credentials, history, degrees) is unchanged;
    nothing is ever added to the resume.

Known gap, surfaced not hidden: v1 tailors from the search result
(title/snippet/family), not a fetched full job description -- no page-
fetch capability exists on the box. metadata.jd_source says so.
"""
import base64
import dataclasses
import json
import shutil
import os
import re
import subprocess
import sys
import time
import urllib.request
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

CAREER_ROOT = os.environ.get("CAREER_ROOT", f"{_TB_BASE}/shared/career")
OPERATOR_DIR = os.environ.get(
    "OPERATOR_DIR", f"{_TB_BASE}/shared/operator")
LOCKBOX = os.environ.get(
    "CAREER_LOCKBOX", os.path.join(OPERATOR_DIR, "job_lockbox"))
RESUME_PATH = os.path.join(OPERATOR_DIR, "resume.txt")
PROFILE_PATH = os.environ.get(
    "CAREER_PROFILE", os.path.join(OPERATOR_DIR, "career_profile.txt"))
MAX_PACKETS = int(os.environ.get("CAREER_MAX_PACKETS", "5"))
# Lane label (Bill-Payer Patrol seam); default reproduces legacy strings.
LANE = os.environ.get("CAREER_LANE", "CAREER HUNTER").strip() or "CAREER HUNTER"
RESEND_URL = "https://api.resend.com/emails"
APP_STATES = ("DISCOVERED", "SELECTED", "PACKET_READY", "SENT_TO_OPERATOR",
              "APPLIED", "REJECTED", "CLOSED", "SUPPRESSED",
              "GENERATION_BLOCKED")
NO_REGEN_STATES = ("PACKET_READY", "SENT_TO_OPERATOR", "APPLIED",
                   "REJECTED", "CLOSED", "SUPPRESSED")
CRED_WATCHLIST = (
    "PMP", "MBA", "PHD", "CSM", "CSPO", "PSM", "SAFE", "CISSP", "CISA",
    "CPA", "PE", "RN", "MD", "JD", "SECURITY CLEARANCE", "TS/SCI",
    "SECRET CLEARANCE", "AWS CERTIFIED", "AZURE CERTIFIED", "GCP CERTIFIED",
    "SIX SIGMA", "PROSCI", "ITIL", "CFA", "SPHR", "SHRM",
)
GENERIC_WORDS = set("""
the a an and or of for to in on with at by from as is are led drove built
managed delivered senior junior director manager lead product program
project owner analyst experience summary skills education professional
remote boston hybrid team teams cross functional stakeholder stakeholders
""".split())


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write(path, text, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    mode = "wb" if binary else "w"
    with open(tmp, mode) as f:
        f.write(text)
    os.replace(tmp, path)


# --- persona scrub (operator ruling 2026-08-17: employer-facing packets ------
# --- carry NO internal persona references: alias, Bluesky handle, etc.) ------

PERSONA_TOKENS_PATH = os.path.join(OPERATOR_DIR, "persona_tokens.txt")
def _load_persona_tokens(path=None):
    """Operator-owned token list (never published). Missing/empty -> ()
    and run_packets emits a visible note; the scrub never fails silently."""
    path = path or PERSONA_TOKENS_PATH
    try:
        with open(path) as fh:
            toks = [ln.strip().lower() for ln in fh
                    if ln.strip() and not ln.lstrip().startswith("#")]
    except (FileNotFoundError, PermissionError):
        return ()
    return tuple(toks)
PERSONA_TOKENS = _load_persona_tokens()


def scrub_persona(text):
    """Deterministically remove operator-internal persona references from
    employer-facing resume text. An inline parenthetical alias is stripped
    from an otherwise-real line; a line that is persona content is dropped."""
    toks = PERSONA_TOKENS
    def _strip_paren(m):
        return "" if any(t in m.group(0).lower() for t in toks) else m.group(0)
    out = []
    for ln in text.splitlines():
        cleaned = re.sub(r"\s*\([^()]*\)", _strip_paren, ln)
        if any(t in cleaned.lower() for t in toks):
            continue
        out.append(cleaned)
    return "\n".join(out)


# --- factuality verification (fail-closed core) ------------------------------

_NUM_RE = re.compile(r"\d[\d,.]*%?")


def _norm_nums(text):
    out = set()
    for m in _NUM_RE.findall(text):
        out.add(m.rstrip(".,").replace(",", ""))
    return out


def verify_factuality(resume_text, corpus_text, job_text):
    """Return (ok, violations, review_warnings). ok=False blocks the packet.
    Deterministic; no model involvement."""
    violations, warnings = [], []
    allowed = _norm_nums(corpus_text) | _norm_nums(job_text)
    allowed.add(time.strftime("%Y", time.gmtime()))
    for tok in sorted(_norm_nums(resume_text) - allowed):
        violations.append("number not in corpus/job: %s" % tok)
    up_resume = resume_text.upper()
    up_corpus = corpus_text.upper()
    for cred in CRED_WATCHLIST:
        # word-boundary match so 'PE' does not fire inside 'OPERATIONS'
        pat = r"(?<![A-Z0-9])" + re.escape(cred) + r"(?![A-Z0-9])"
        if re.search(pat, up_resume) and not re.search(pat, up_corpus):
            violations.append("credential not in corpus: %s" % cred)
    corpus_up = corpus_text.upper()
    job_up = (job_text or "").upper()
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9+#.]+(?: [A-Z][a-zA-Z0-9+#.]+)+)",
                         resume_text):
        phrase = m.group(1)
        words = set(w.lower() for w in phrase.split())
        if words <= GENERIC_WORDS:
            continue
        if phrase.upper() not in corpus_up and phrase.upper() not in job_up:
            warnings.append("unverified proper noun: %s" % phrase[:60])
    return (not violations), violations, warnings[:12]


# --- output contract and resume-only lint (operator ruling 2026-10-13) --------

RESUME_BEGIN = "=== RESUME BEGIN ==="
RESUME_END = "=== RESUME END ==="
# Each rule: (name, compiled pattern). A match anywhere in the resume blocks it.
LINT_RULES = (
    ("ai-disclosure", re.compile(
        r"\bas an ai\b|\bi(?: am|'m) an ai\b|"
        r"\b(?:this|the) (?:resume|document|text|draft) (?:was|is|has been) (?:an? )?(?:ai[- ]?(?:generated|written|assisted)|"
        r"(?:generated|written|drafted|produced|created) (?:by|with|using) (?:an? )?(?:ai|language model))\b|"
        r"\b(?:generated|written|drafted) by (?:an? )?(?:ai|language model)\b", re.I)),
    ("meta-commentary", re.compile(
        r"\bI(?:'ve| have)? (?:tailored|emphasi[sz]ed|highlighted|reframed|removed|kept|added|omitted|"
        r"included|focused|adjusted|rewrote|reordered)\b|\bI (?:can(?:no|')t|could not|won't|will not)\b|"
        r"\b(?:this|the) resume\b|\bthis draft\b|\bthe corpus\b|\bthe operator\b|\bhard rules\b|"
        r"\bmission selection\b", re.I)),
    ("note-label", re.compile(
        r"^\s*[*_#>]*\s*(?:[A-Za-z][\w'\u2019]*\s+)?"
        r"(?:notes?|n\.b\.|disclaimer|caveat|commentary|explanation|rationale|reminder|"
        r"(?:honest|fit) (?:flag|note)|tailoring notes?|changes made|key changes|unverified|gaps?|"
        r"summary of changes)\b[*_]*\s*[:\-]",
        re.I | re.M)),
    # Post-1.0 CLARK-LINT-GAP-1: the assistant talking to its reader inside the
    # markers. Corpus wording is still excused (not in NO_CORPUS_ALLOWANCE).
    ("assistant-voice", re.compile(
        r"\blet me know\b|\bhope (?:this|it) helps\b|\bfeel free to\b|"
        r"\bthis version (?:highlights|leads|emphasi[sz]es|focuses|is)\b|"
        r"^\s*p\.\s?s\.|<!--|[(\[]\s*tailored (?:for|to)\b|^\s*tailored (?:for|to)\b|"
        r"\bwhy (?:this|the) (?:role|position|job) (?:fits|is a (?:good |strong )?fit)\b", re.I | re.M)),
    ("preamble", re.compile(
        r"^\s*(?:here(?:'s| is) (?:the|your|a)\b|below is\b|sure[,!]|certainly[,!]|of course[,!])", re.I | re.M)),
    ("placeholder", re.compile(
        r"\[[^\]\n]{1,80}\]|\{\{[^}\n]*\}\}|<[A-Za-z][A-Za-z _]{1,40}>|\bTBD\b|\bTODO\b|\bX{3,}\b")),
    ("drafting-instruction", re.compile(
        r"\b(?:insert|add|replace|customi[sz]e|fill in|update) (?:your|here|this)\b|\(optional\)|"
        r"\bif applicable\b|\bconsider (?:adding|including|mentioning)\b", re.I)),
    ("code-fence", re.compile(r"^\s*```", re.M)),
    # v2 (operator ruling 2026-10-13, priority override): literal phrases from
    # the recurring contamination. No corpus allowance (NO_CORPUS_ALLOWANCE).
    ("blocklist-phrase", re.compile(
        "\\b(?:one honest flag|honest flag|the corpus|the operator|r[e\\u00e9]sum[e\\u00e9] follows)\\b", re.I)),
    ("warning", re.compile(
        r"^\s*[*_#>]*\s*(?:warning|warnings|caution|important|attention)\b[*_]*\s*[:!\-]|\bwarning:", re.I | re.M)),
    ("fit-score", re.compile(
        r"\bfit\s*(?:score|rating|assessment|level|check)\b|\bmatch\s*(?:score|rating|percentage)\b|"
        r"\b(?:strong|weak|poor|good|moderate|partial|low|high)\s+(?:fit|match)\b|"
        r"\b\d{1,3}\s*(?:/\s*(?:10|35|100)|%)\s*(?:fit|match)\b", re.I)),
    ("editorial", re.compile(
        r"\bI (?:would|recommend|suggest|think|believe|noticed|wanted|want|need to|should)\b|"
        r"\b(?:note that|keep in mind|to be honest|honestly|frankly|candidly|it(?:'s| is) worth noting)\b|"
        r"\bthis (?:role|position|job|posting) (?:is|requires|asks|wants|has|needs)\b", re.I)),
    ("operator-instruction", re.compile(
        r"\b(?:before (?:you )?(?:send|submit|apply)|you (?:should|may want to|might want to|could|will need to)|"
        r"make sure (?:to|you)|remember to|double[- ]check|be prepared to|"
        r"consider (?:removing|softening|emphasi[sz]ing|cutting))\b", re.I)),
)
# Rules whose matches are never excused by the corpus (the operator's own
# profile text is prompt material, and these phrases are never resume content).
NO_CORPUS_ALLOWANCE = frozenset(("blocklist-phrase",))
SECTION_HEADINGS = frozenset("""summary|professional summary|profile|professional profile|skills|core skills|
technical skills|core competencies|competencies|experience|professional experience|work experience|
employment|employment history|work history|education|certifications|certificates|projects|selected projects|
achievements|selected achievements|additional|additional experience|additional information|objective|
qualifications|contact|awards|publications|volunteer|volunteer experience|languages""".replace("\n", "").split("|"))
_CONTACT_TOKEN_RE = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|"
    r"(?:linkedin\.com|github\.com)/[\w/-]+", re.I)
# Degree families: a degree in the resume must have SOME spelling of its family in
# the corpus (dots are ignored, so B.S. = BS). A generic level word (Bachelor,
# Master) needs some degree of that level in the corpus.
DEGREE_FAMILIES = (
    ("bachelor of arts", ("BA", "BACHELOR OF ARTS", "BACHELORS OF ARTS")),
    ("bachelor of science", ("BS", "BSC", "BACHELOR OF SCIENCE", "BACHELORS OF SCIENCE")),
    ("bachelor", ("BACHELOR", "BACHELORS", "BA", "BS", "BSC", "BFA", "BBA")),
    ("master of arts", ("MA", "MASTER OF ARTS", "MASTERS OF ARTS")),
    ("master of science", ("MS", "MSC", "MASTER OF SCIENCE", "MASTERS OF SCIENCE")),
    ("mba", ("MBA", "MASTER OF BUSINESS ADMINISTRATION")),
    ("mpa/mph", ("MPA", "MPH", "MASTER OF PUBLIC")),
    ("master", ("MASTER", "MASTERS", "MA", "MS", "MSC", "MBA", "MPA", "MPH", "MFA", "MED")),
    ("doctorate", ("PHD", "DOCTOR OF PHILOSOPHY", "DOCTORATE", "DOCTORAL")),
    ("associate", ("ASSOCIATE OF", "ASSOCIATE DEGREE", "ASSOCIATES DEGREE")),
)
# Tokens that by themselves name a degree in the resume (MA is excluded: it is
# also a state abbreviation; Master of Arts is still caught by its long form).
_DEGREE_TRIGGERS = {"bachelor of arts": ("BA", "BACHELOR OF ARTS"),
                    "bachelor of science": ("BS", "BSC", "BACHELOR OF SCIENCE"),
                    "bachelor": ("BACHELOR", "BACHELORS"),
                    "master of arts": ("MASTER OF ARTS",),
                    "master of science": ("MS", "MSC", "MASTER OF SCIENCE"),
                    "mba": ("MBA", "MASTER OF BUSINESS ADMINISTRATION"),
                    "mpa/mph": ("MPA", "MPH"),
                    "master": ("MASTER", "MASTERS"),
                    "doctorate": ("PHD", "DOCTOR OF PHILOSOPHY", "DOCTORATE"),
                    "associate": ("ASSOCIATE OF", "ASSOCIATE DEGREE")}
_YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
_HIST_LABEL_RE = re.compile(r"^\s*(?:experience|employment|work history|education)\s*:", re.I)
_BULLET_RE = re.compile("^\\s*(?:[-*\\u2022]|\\d+[.)])\\s")
_CAP_RUN_RE = re.compile(r"[A-Z][A-Za-z0-9&.'+#/-]*(?: [A-Z][A-Za-z0-9&.'+#/-]*)*")
# Single words on a history line that carry no employer/title/school fact. Title
# words (Senior, Lead, Director ...) are deliberately NOT here: they must be in the corpus.
_HIST_GENERIC = set(
    "experience employment education work history present current remote hybrid onsite to and of the "
    "jan feb mar apr may jun jul aug sep sept oct "
    "nov dec january february march april june july august september october november december".split())


def extract_resume(reply):
    """Split a tailoring reply by the output contract. Returns
    (resume_text, operator_notes, problems). problems non-empty -> block.
    Text outside the markers is returned as operator_notes (routed to the
    run's notes), never as resume text."""
    reply = reply or ""
    nb, ne = reply.count(RESUME_BEGIN), reply.count(RESUME_END)
    if nb != 1 or ne != 1:
        return "", reply.strip(), ["output contract: expected exactly one %s/%s pair, found %d/%d"
                                   % (RESUME_BEGIN, RESUME_END, nb, ne)]
    head, rest = reply.split(RESUME_BEGIN, 1)
    if RESUME_END not in rest:
        return "", reply.strip(), ["output contract: %s precedes %s" % (RESUME_END, RESUME_BEGIN)]
    body, tail = rest.split(RESUME_END, 1)
    notes = "\n".join(x.strip() for x in (head, tail) if x.strip())
    body = body.strip("\n")
    if not body.strip():
        return "", notes, ["no resume produced (the model declined; its reason is in the operator notes)"]
    return body, notes, []


def lint_resume(resume_text, corpus_text=""):
    """Deterministic resume-only check. Returns a list of violations, one per
    rule that matched (rule name + the matched text, truncated). A match
    whose text appears verbatim in the corpus is the applicant's own wording
    (e.g. an AI-engineering skill line), not commentary, and is ignored."""
    corpus_up = " ".join(corpus_text.upper().split())
    out = []
    for name, rx in LINT_RULES:
        for m in rx.finditer(resume_text):
            hit = " ".join(m.group(0).split())
            if hit and name not in NO_CORPUS_ALLOWANCE and hit.upper() in corpus_up:
                continue
            out.append("non-resume content (%s): %s" % (name, hit[:50]))
            break
    return out


def check_duplicates(resume_text):
    """Rule 5 (operator ruling 2026-10-13): a repeated name line, a repeated
    contact block (the same e-mail/phone/profile URL on two lines, or two
    identical contact lines) or a repeated section heading fails the
    deliverable. A repeated employer line is legitimate (two roles at one
    employer) and is not checked."""
    out = []
    lines = [ln for ln in resume_text.splitlines() if ln.strip()]
    if not lines:
        return out
    norm = lambda s: " ".join(s.lower().replace(":", " ").split())
    name = norm(lines[0])
    if name and sum(1 for ln in lines if norm(ln) == name) > 1:
        out.append("duplicate header (name line): %s" % lines[0].strip()[:50])
    seen_tok, contact_lines = {}, collections_counter(
        norm(ln) for ln in lines if _CONTACT_TOKEN_RE.search(ln))
    for ln, n in contact_lines.items():
        if n > 1:
            out.append("duplicate contact block (line repeated): %s" % ln[:50])
            break
    else:
        for i, ln in enumerate(lines):
            for tok in set(t.lower() for t in _CONTACT_TOKEN_RE.findall(ln)):
                if tok in seen_tok and seen_tok[tok] != i:
                    out.append("duplicate contact block (token repeated): %s" % tok[:50])
                    break
                seen_tok[tok] = i
            if out and out[-1].startswith("duplicate contact"):
                break
    heads = collections_counter(norm(ln).strip(" -=*#") for ln in lines
                                if norm(ln).strip(" -=*#") in SECTION_HEADINGS)
    for h, n in sorted(heads.items()):
        if n > 1:
            out.append("duplicate section heading: %s" % h.upper())
    return out


def collections_counter(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def validate_deliverable_text(text, corpus_text):
    """Rules 3-5 on deliverable text: blocklist/lint and duplicates."""
    return lint_resume(text, corpus_text) + check_duplicates(text)


@dataclasses.dataclass
class GenerationResult:
    """One generation, split by destination. Only resume_text may reach a
    renderer; everything else is for the operator's email."""
    resume_text: str = ""
    fit_assessment: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)
    notes: list = dataclasses.field(default_factory=list)
    problems: list = dataclasses.field(default_factory=list)
    source: str = "tailored"          # tailored | base-resume


_FIT_RE = re.compile(r"\bfit\b|\bmatch\b|\bgap\b|\bstrong\b|\bweak\b|\bqualif", re.I)
_WARN_RE = re.compile(r"flag|warn|caution|concern|risk|cannot|can't|not in the corpus|unverified|missing|honest", re.I)


def classify_operator_text(text):
    """Split the model's operator-facing text into (fit_assessment, warnings,
    notes), line by line. Deterministic; nothing is dropped."""
    fit, warn, notes = [], [], []
    for para in (text or "").splitlines():
        p = " ".join(para.split()).strip(" -")
        if not p or set(p) <= set("-=*_#"):
            continue
        (warn if _WARN_RE.search(p) else fit if _FIT_RE.search(p) else notes).append(p)
    return fit, warn, notes


def build_result(reply, corpus, corpus_all, job):
    """Parse a tailoring reply into a GenerationResult and run every pre-render
    check (contract, factuality, lint/blocklist, history, duplicates)."""
    resume_text, op_text, problems = extract_resume(reply)
    fit, warn, notes = classify_operator_text(op_text)
    r = GenerationResult(fit_assessment=fit, warnings=warn, notes=notes)
    if problems:
        r.problems = problems
        return r
    r.resume_text = scrub_persona(resume_text)
    _ok, fviol, review = verify_factuality(
        r.resume_text, corpus_all, job["description"] + " " + job["title"])
    r.warnings += review
    r.problems = (fviol + validate_deliverable_text(r.resume_text, corpus_all)
                  + verify_history(r.resume_text, corpus_all))
    return r


def base_result(corpus, corpus_all, job):
    """The operator's own resume, persona-scrubbed, as a deliverable of last
    resort. It is checked like a tailored one; nothing is added."""
    r = GenerationResult(resume_text=scrub_persona(corpus["resume"]).strip("\n"), source="base-resume")
    _ok, fviol, _rev = verify_factuality(r.resume_text, corpus_all, job["description"] + " " + job["title"])
    r.problems = fviol + validate_deliverable_text(r.resume_text, corpus_all) + verify_history(r.resume_text, corpus_all)
    return r


def verify_history(resume_text, corpus_text):
    """Employment and education facts must come from the corpus. On every
    history line -- a non-bullet line carrying a year, or a line labelled
    EXPERIENCE/EMPLOYMENT/WORK HISTORY/EDUCATION -- each capitalised phrase
    (employer, title, school, degree) must appear in the corpus. Degree
    tokens anywhere in the resume must appear in the corpus. Returns
    violations (fail-closed)."""
    out = []
    corpus_up = _norm_up(corpus_text)
    up = _norm_up(resume_text)
    for fam, triggers in _DEGREE_TRIGGERS.items():
        if any(_has_word(up, t) for t in triggers) and not any(
                _has_word(corpus_up, s) for f, spell in DEGREE_FAMILIES if f == fam for s in spell):
            out.append("degree not in corpus: %s" % fam)
    for line in resume_text.splitlines():
        if _BULLET_RE.match(line) or not (_YEAR_RE.search(line) or _HIST_LABEL_RE.match(line)):
            continue
        text = _HIST_LABEL_RE.sub("", line)
        for m in _CAP_RUN_RE.finditer(text):
            piece = m.group(0).strip(".,;:")
            if not _segments_in_corpus(piece.split(), corpus_up):
                out.append("history fact not in corpus: %s" % piece[:60])
    return out


def _norm_up(text):
    """Upper case, dots dropped (B.S. = BS), whitespace collapsed."""
    return " ".join(text.upper().replace(".", "").split())


def _has_word(hay, phrase):
    return re.search(r"(?<![A-Z0-9])" + re.escape(phrase) + r"(?![A-Z0-9])", hay) is not None


def _segments_in_corpus(words, corpus_up):
    """True if the words split into consecutive segments that each occur in the
    corpus as whole words (so 'Acme Insurance Senior Technical Product Owner'
    passes when both parts are in the corpus). A single word that is a month,
    'Present', a section label or another generic word is not a fact. An
    invented employer, title or school fails."""
    n = len(words)
    ok = [True] + [False] * n
    for i in range(1, n + 1):
        for j in range(i):
            if not ok[j]:
                continue
            seg = _norm_up(" ".join(words[j:i]))
            if (i - j == 1 and words[j].lower().strip(".,") in _HIST_GENERIC) or (seg and _has_word(corpus_up, seg)):
                ok[i] = True
                break
    return ok[n]


# --- selection ---------------------------------------------------------------

def parse_selected(report_body, exclude_companies, app_mem, cap):
    """Pull the SELECTED_JSON block from Han's report. Deterministic gates
    re-applied here: exclusions and no-regeneration are ENFORCED in code,
    never trusted to the model."""
    idx = report_body.find("SELECTED_JSON:")
    start = report_body.find("[", idx) if idx >= 0 else -1
    if idx < 0 or start < 0:
        return [], ["no SELECTED_JSON block in report"]
    try:
        raw, _ = json.JSONDecoder().raw_decode(report_body[start:])
    except ValueError as e:
        return [], ["SELECTED_JSON unparseable: %s" % e]
    picked, notes = [], []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        company = str(item.get("company", "")).strip()
        role = str(item.get("role", "")).strip()
        if not url or not company or not role:
            notes.append("selected item missing url/company/role, dropped")
            continue
        blob = (company + " " + role + " " + url).lower()
        hit = [n for n in exclude_companies
               if re.search(r"\b" + re.escape(n.lower()) + r"\b", blob)]
        if hit:
            notes.append("NEGATIVE CONTROL: excluded company refused a "
                         "packet: %s" % ",".join(hit))
            continue
        st = app_mem.get(url, {}).get("state", "")
        if st in NO_REGEN_STATES:
            notes.append("NEGATIVE CONTROL: %s already %s, not regenerated"
                         % (url[:60], st))
            continue
        picked.append({"url": url, "company": company, "role": role,
                       "title": str(item.get("title", role)),
                       "description": str(item.get("description", "")),
                       "fit": item.get("fit", ""),
                       "comp": str(item.get("comp", "UNKNOWN")),
                       "why": str(item.get("why", ""))})
        if len(picked) >= cap:
            break
    return picked, notes


# --- application memory ------------------------------------------------------

def load_app_mem():
    path = os.path.join(CAREER_ROOT, "applications.json")
    try:
        return json.loads(read(path))
    except (OSError, ValueError):
        return {}


def save_app_mem(mem):
    atomic_write(os.path.join(CAREER_ROOT, "applications.json"),
                 json.dumps(mem, indent=1, sort_keys=True) + "\n")


def set_state(mem, url, state, **extra):
    if state not in APP_STATES:
        raise ValueError("bad state %r" % state)
    rec = mem.setdefault(url, {})
    rec["state"] = state
    rec["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec.update(extra)


# --- tailoring (governed turn is injected by caller) -------------------------

TAILOR_ATTEMPTS = max(1, int(os.environ.get("CAREER_TAILOR_ATTEMPTS", "3")))


def tailor_prompt(corpus, job, feedback=None):
    """The tailoring prompt. feedback: the previous attempt's rejection
    reasons; the retry is told exactly what to leave out."""
    base = _tailor_prompt_core(corpus, job)
    tail = ("FORBIDDEN inside the markers (the resume is sent to an employer "
            "as-is): any note, flag, warning, fit or match assessment, score, "
            "opinion, advice to the reader, AI mention, placeholder or "
            "bracket, a repeated name/contact block or a repeated section "
            "heading, and the words 'honest flag', 'the corpus', 'the "
            "operator', 'resume follows'. Write the name and contact block "
            "once, at the top.")
    if feedback:
        tail += ("\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED and was not sent. "
                 "Reasons:\n- " + "\n- ".join(str(f)[:160] for f in feedback[:8]) +
                 "\nReturn the resume again, between the markers, without "
                 "that content. Put anything for the operator after %s."
                 % RESUME_END)
    return base + "\n\n" + tail


def _tailor_prompt_core(corpus, job):
    return "\n\n".join((
        "Tailor the operator's resume for ONE specific job. HARD RULES:",
        "- Use ONLY facts present in the OPERATOR CORPUS below. You may "
        "select, reorder, emphasize, summarize, and truthfully reframe. "
        "You may NOT invent credentials, technologies, employers, titles, "
        "dates, metrics, numbers, certifications, or accomplishments.",
        "- Keep employment history factually intact (employers, titles, "
        "dates exactly as the corpus states them).",
        "- Every number in your output must already exist in the corpus.",
        "- Plain text only, ready to paste: name/contact block first if "
        "present in corpus, summary, skills, experience, education.",
        "- 55 lines maximum.",
        "=== OPERATOR CORPUS: RESUME ===", corpus["resume"],
        "=== OPERATOR CORPUS: CAREER PROFILE ===", corpus["profile"],
        "=== TARGET JOB ===",
        "Company: %s\nRole: %s\nSource URL: %s\nDescription: %s"
        % (job["company"], job["role"], job["url"], job["description"]),
        "OUTPUT CONTRACT (strict): write the resume, and nothing else, "
        "between a line %s and a line %s. Inside the markers: only "
        "applicant-facing resume content -- no notes, flags, caveats, "
        "explanations, questions, placeholders, brackets or drafting "
        "instructions. Anything you need to tell the operator (fit concerns, "
        "an honest flag, a mission note) goes AFTER %s; it is routed to the "
        "operator's log and never sent to an employer. If no honest resume "
        "is possible for this job, leave the markers empty and say why after "
        "them." % (RESUME_BEGIN, RESUME_END, RESUME_END)))


# --- pdf ---------------------------------------------------------------------

def render_pdf(text, out_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    tmp = out_path + ".tmp"
    c = canvas.Canvas(tmp, pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 750
    for line in text.splitlines():
        for chunk in [line[i:i + 95] for i in range(0, max(len(line), 1), 95)]:
            c.drawString(54, y, chunk)
            y -= 13
            if y < 54:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = 750
    c.save()
    os.replace(tmp, out_path)


# --- lockbox -----------------------------------------------------------------

def sanitize(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())[:60].strip("._")
    return s or "unnamed"


def packet_dir(day, company, role):
    """Never silently overwrite: suffix -2, -3... if the dir has content."""
    base = os.path.join(LOCKBOX, day, sanitize(company), sanitize(role))
    path, n = base, 1
    while os.path.isdir(path) and os.listdir(path):
        n += 1
        path = "%s-%d" % (base, n)
    os.makedirs(path, exist_ok=True)
    return path


PENDING = ".unvalidated"


def pdf_text(path):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


def validate_rendered(pdir, corpus_text):
    """Rule 6: the gate. Read the WRITTEN resume.txt and extract the text of
    the WRITTEN resume.pdf (both still under their pending names) and run
    rules 3-5 on each. Returns violations prefixed with the artifact. A PDF
    that cannot be read fails closed."""
    out = []
    try:
        txt = read(os.path.join(pdir, "resume.txt" + PENDING))
    except OSError as e:
        return ["rendered resume.txt unreadable: %s" % e]
    out += ["rendered resume.txt: " + v for v in validate_deliverable_text(txt, corpus_text)]
    try:
        ptxt = pdf_text(os.path.join(pdir, "resume.pdf" + PENDING))
    except Exception as e:  # noqa: BLE001 -- unreadable PDF fails closed
        return out + ["rendered resume.pdf unreadable: %s" % str(e)[:120]]
    if not ptxt.strip():
        return out + ["rendered resume.pdf: no extractable text"]
    out += ["rendered resume.pdf: " + v for v in validate_deliverable_text(ptxt, corpus_text)]
    return out


def write_packet(pdir, job, result, warnings, jd_source, corpus_text="", extra=None):
    """Render a GenerationResult. Only result.resume_text is written; the
    files are written under pending names, validated as rendered, and only
    then moved to resume.txt / resume.pdf. Returns (meta, violations); on
    violations nothing deliverable remains in pdir."""
    if not isinstance(result, GenerationResult):
        raise TypeError("write_packet takes a GenerationResult, not %s" % type(result).__name__)
    resume_text = result.resume_text
    atomic_write(os.path.join(pdir, "resume.txt" + PENDING), resume_text)
    render_pdf(resume_text, os.path.join(pdir, "resume.pdf" + PENDING))
    bad = validate_rendered(pdir, corpus_text)
    if bad:
        shutil.rmtree(pdir, ignore_errors=True)
        return None, bad
    for f in ("resume.txt", "resume.pdf"):
        os.replace(os.path.join(pdir, f + PENDING), os.path.join(pdir, f))
    atomic_write(os.path.join(pdir, "job.txt"),
                 "Company: %s\nRole: %s\nApply: %s\n\n%s\n"
                 % (job["company"], job["role"], job["url"],
                    job["description"]))
    meta = {
        "company": job["company"], "role": job["role"],
        "source_url": job["url"], "apply_url": job["url"],
        "fit": job.get("fit", ""), "salary": job.get("comp", "UNKNOWN"),
        "date_discovered": time.strftime("%Y-%m-%d", time.gmtime()),
        "artifacts": {"resume_pdf": os.path.join(pdir, "resume.pdf"),
                      "resume_txt": os.path.join(pdir, "resume.txt")},
        "generation_status": "PACKET_READY",
        "jd_source": jd_source,
        "review_warnings": warnings,
        "resume_source": result.source,
    }
    meta.update(extra or {})
    atomic_write(os.path.join(pdir, "metadata.json"),
                 json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta, []


def generate_for_job(job, corpus, corpus_all, turn_fn, day, notes_sink):
    """Retry-then-fallback. Returns dict(kind=PACKET|BLOCKED, result, meta,
    attempts, fallback). attempts: [(n, problems)] of rejected tailoring
    attempts. Every rejection is logged with its rule and matched text."""
    attempts, feedback = [], None
    fit, warn, notes = [], [], []
    tag = "%s / %s" % (job["company"][:40], job["role"][:40])
    for n in range(1, TAILOR_ATTEMPTS + 1):
        ok, status, reply = turn_fn(tailor_prompt(corpus, job, feedback))
        if not ok or not (reply or "").strip():
            res = GenerationResult(problems=["tailor turn not-ok: %s" % (status or "")[:120]])
        else:
            res = build_result(reply, corpus, corpus_all, job)
        for src, dst in ((res.fit_assessment, fit), (res.warnings, warn), (res.notes, notes)):
            dst.extend(x for x in src if x not in dst)
        if res.fit_assessment or res.warnings or res.notes:
            notes_sink.append("tailor notes for operator (%s): %s" % (
                tag, " ".join(" ".join(res.fit_assessment + res.warnings + res.notes).split())[:600]))
        if not res.problems:
            pdir = packet_dir(day, job["company"], job["role"])
            meta, bad = write_packet(pdir, job, res, res.warnings, "search_snippet (no page fetch on box)",
                                     corpus_all, {"tailor_attempts": n})
            if not bad:
                res.fit_assessment, res.warnings, res.notes = fit, warn, notes
                return dict(kind="PACKET", result=res, meta=meta, attempts=attempts, fallback=False)
            res.problems = bad
        attempts.append((n, list(res.problems)))
        for p in res.problems:
            notes_sink.append("tailor attempt %d REJECTED (%s): %s" % (n, tag, p[:200]))
        feedback = res.problems
    base = base_result(corpus, corpus_all, job)
    base.fit_assessment, base.warnings, base.notes = fit, warn, notes
    if not base.problems:
        pdir = packet_dir(day, job["company"], job["role"])
        meta, bad = write_packet(pdir, job, base, [], "base resume (tailoring rejected)", corpus_all,
                                 {"tailor_attempts": len(attempts), "tailor_rejections": attempts})
        if not bad:
            notes_sink.append("base resume delivered for %s after %d rejected attempt(s)" % (tag, len(attempts)))
            return dict(kind="PACKET", result=base, meta=meta, attempts=attempts, fallback=True)
        base.problems = bad
    for p in base.problems:
        notes_sink.append("base resume REJECTED (%s): %s" % (tag, p[:200]))
    return dict(kind="BLOCKED", result=base, meta=None, attempts=attempts + [("base", base.problems)],
                fallback=True)


# --- email -------------------------------------------------------------------

# Operator ruling 2026-10-13 (v2 fallback): a base-resume fallback reaches only the operator email,
# labelled UNTAILORED in the attachment name and the section headers. Nothing in this module sends to an employer.
UNTAILORED_TAG = "UNTAILORED (base resume, not tailored; operator approval required before any employer use) -- "


def agent_name():
    """Whose notes these are: Clark's tree (career_clark) or Han's."""
    return (os.environ.get("CAREER_AGENT_NAME")
            or ("Clark" if "career_clark" in CAREER_ROOT else "Han"))


def notes_block(gen, attempts=(), fallback=False, blocked=False):
    """The operator-only section for one job: the model's fit assessment,
    warnings and notes, and why a tailored resume was not used. Never
    attached, never rendered."""
    lines = ["%s's notes (for you only; not in the resume)%s:"
             % (agent_name(), " -- UNTAILORED resume attached" if fallback and not blocked else "")]
    for label, items in (("Fit assessment", gen.fit_assessment if gen else []),
                         ("Warnings", gen.warnings if gen else []),
                         ("Notes", gen.notes if gen else [])):
        for x in items:
            lines.append("  %s: %s" % (label, " ".join(str(x).split())[:600]))
    for n, probs in attempts:
        what = "Base resume rejected" if n == "base" else "Tailored attempt %s rejected" % n
        lines.append("  %s: %s" % (what, "; ".join(p[:160] for p in probs[:4])))
    if blocked:
        lines.append("  GENERATION_BLOCKED: no resume could be produced that passes the checks.")
    elif fallback:
        lines.append("  UNTAILORED: your base resume is attached instead (every tailored attempt was rejected). "
                     "It is not tailored to this job and has not been sent to the employer; nothing is "
                     "sent to an employer without your approval.")
    if len(lines) == 1:
        lines.append("  (none)")
    return lines


def build_email(packets, blocked, day):
    """Return (subject, plain-text body, attachments list). Pure."""
    lines = ["%s APPLICATION PACKETS -- %s" % (LANE, day),
             "%d packet(s) ready, %d blocked" % (len(packets), len(blocked)),
             ""]
    attachments = []
    for p in packets:
        m = p["meta"]
        tag = UNTAILORED_TAG if p.get("fallback") else ""
        lines += ["=" * 60,
                  "%s%s -- %s" % (tag, m["company"], m["role"]),
                  "Fit: %s" % (m["fit"] or "n/a"),
                  "Compensation: %s" % m["salary"],
                  "Why it fits: %s" % p["job"].get("why", ""),
                  "APPLY: %s%s" % (m["apply_url"],
                      "   [link not verified]"
                      if p["job"].get("verify_status") == "UNVERIFIED"
                      else ""),
                  "Resume PDF: %s" % m["artifacts"]["resume_pdf"]]
        if m["review_warnings"]:
            lines.append("REVIEW WARNINGS: " +
                         "; ".join(m["review_warnings"][:4]))
        if "gen" in p:
            lines += notes_block(p["gen"], p.get("attempts", ()), p.get("fallback", False))
        lines.append("")
        try:
            with open(m["artifacts"]["resume_pdf"], "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")
            attachments.append({
                "filename": "%s%s-%s.pdf" % ("UNTAILORED-" if p.get("fallback") else "",
                                             sanitize(m["company"]), sanitize(m["role"])),
                "content": content})
        except OSError:
            lines.append("(pdf attach failed; use lockbox path)")
    for b in blocked:
        lines += ["=" * 60,
                  "BLOCKED (factuality fail-closed): %s -- %s"
                  % (b["job"]["company"], b["job"]["role"]),
                  "APPLY: %s" % b["job"]["url"],
                  "Violations: " + "; ".join(b["violations"][:6]),
                  "No packet was generated for this job. Operator review "
                  "needed."]
        if "gen" in b:
            lines += notes_block(b["gen"], b.get("attempts", ()), True, blocked=True)
        lines.append("")
    if packets:
        lines += ["=" * 60, "FULL RESUME TEXTS", "=" * 60, ""]
        for p in packets:
            m = p["meta"]
            lines += ["%s%s -- %s" % (UNTAILORED_TAG if p.get("fallback") else "", m["company"], m["role"]),
                      "-" * 40, p["resume_text"], ""]
    return ("%s packets %s: %d ready, %d blocked"
            % (LANE.title(), day, len(packets), len(blocked)),
            "\n".join(lines), attachments)


def send_email(subject, body, attachments):
    key = os.environ.get("RESEND_API_KEY", "")
    frm = os.environ.get("RESEND_FROM", "")
    to = os.environ.get("DIGEST_TO", "")
    if not (key and frm and to):
        raise RuntimeError("Resend env incomplete (RESEND_API_KEY/"
                           "RESEND_FROM/DIGEST_TO)")
    payload = {"from": frm, "to": [to], "subject": subject, "text": body}
    if attachments:
        payload["attachments"] = attachments
    req = urllib.request.Request(
        RESEND_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % key,
                 "Content-Type": "application/json",
                 "User-Agent": "Treebeard-CareerHunter/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# --- orchestration entry (called by career_hunter_daily) ----------------------

def _redirected_to_index(orig, final):
    """True only when a redirect collapsed a posting to a generic listing."""
    from urllib.parse import urlparse
    if not final or final == orig:
        return False
    fp = urlparse(final)
    path = (fp.path or "/").rstrip("/").lower()
    generic = ("", "/jobs", "/careers", "/search", "/positions", "/openings")
    op = urlparse(orig).path.rstrip("/").lower()
    return path in generic and path != op


# --- Option B verification (operator ruling 2026-09-04) ----------------------
# VERIFIED means the exact URL that will be emailed is demonstrably an
# actionable posting: reachable, on an employer/ATS host, showing the job
# title and an apply affordance, with no closed marker. HTTP 200 alone is
# UNVERIFIED. Explicit closed evidence is DEAD even on 200. Anything
# ambiguous (bot walls, non-HTML, JS-only shells, 401/403/429, errors) is
# UNVERIFIED, never DEAD.
# _VERIFY_ATS_HOSTS mirrors toolkit/career_search/main.py ATS_HOSTS. That
# directory is Anvil-locked and deliberately not imported; keep in sync.
_VERIFY_ATS_HOSTS = (
    "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
    "myworkdayjobs.com", "icims.com", "ashbyhq.com", "jobs.ashbyhq.com",
    "smartrecruiters.com", "jobvite.com", "bamboohr.com", "breezy.hr",
    "recruitee.com", "workable.com", "applytojob.com", "taleo.net",
    "successfactors.com", "paylocity.com", "workforcenow.adp.com",
    "oraclecloud.com", "eightfold.ai", "phenom.com",
)
_VERIFY_CLOSED_RE = re.compile(
    r"no longer accepting applications"
    r"|position has been filled"
    r"|this (?:job|position|posting|role|opportunity) (?:is no longer available"
    r"|has been (?:filled|closed|removed)|has expired|is closed)"
    r"|(?:job|posting|listing) (?:has )?expired"
    r"|applications? (?:for this (?:job|position|role) )?(?:are|is) (?:now )?closed",
    re.IGNORECASE)
_VERIFY_CHALLENGE_RE = re.compile(
    r"enable javascript and cookies|attention required|cf-chl|cf_chl"
    r"|verify (?:that )?you are (?:a )?human|pardon our interruption"
    r"|incapsula|request blocked|access denied|just a moment\.\.\.",
    re.IGNORECASE)
_VERIFY_APPLY_RE = re.compile(
    r"<(?:a|button|input|form)\b[^>]*\bapply\b[^>]*>"
    r"|>\s*apply(?:\s+(?:now|here|today|online|for this (?:job|position|role)))?\s*<",
    re.IGNORECASE)
_VERIFY_TITLE_STOP = frozenset(
    "the and for with from job jobs role position sr jr".split())
_VERIFY_MIN_HTML = 1500
# 2 MiB: Lever hosted pages front-load ~700 KB of inline CSS before the
# apply anchor; a 512 KB cap truncated it (false UNVERIFIED, 2026-09-04).
_VERIFY_MAX_READ = 2 * 1024 * 1024


def _verify_host_ok(final_url):
    """True for employer/ATS hosts (mirrors career_search tier 1)."""
    from urllib.parse import urlparse
    p = urlparse(final_url)
    host = (p.hostname or "").lower()
    path = (p.path or "").lower()
    if not host:
        return False
    for t in _VERIFY_ATS_HOSTS:
        if host == t or host.endswith("." + t):
            return True
    return ("/careers" in path or "/jobs/" in path
            or path.rstrip("/").endswith("/jobs"))


def _verify_title_tokens(title):
    return set(t for t in re.findall(r"[a-z0-9]+", (title or "").lower())
               if len(t) >= 3 and t not in _VERIFY_TITLE_STOP)


def verify_apply_url(url, _opener=None, title=""):
    """Option B gate on the exact URL that will be emailed.
    Returns (status, code, reason): status is VERIFIED | DEAD | UNVERIFIED.
    Fail-open: anything ambiguous is UNVERIFIED, never DEAD."""
    import urllib.request, urllib.error
    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": ua})
        opener = _opener or urllib.request.build_opener()
        with opener.open(req, timeout=20) as resp:
            code = getattr(resp, "status", 200) or 200
            final = resp.geturl()
            hdrs = getattr(resp, "headers", None)
            ctype = str(hdrs.get("Content-Type", "") if hdrs else "")
            raw = resp.read(_VERIFY_MAX_READ)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "DEAD", e.code, "http %d" % e.code
        return "UNVERIFIED", e.code, "http %d" % e.code
    except Exception as e:
        return "UNVERIFIED", 0, "fetch error: %s" % type(e).__name__
    if _redirected_to_index(url, final):
        return "DEAD", code, "redirected to index"
    text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
            else str(raw or ""))
    if _VERIFY_CHALLENGE_RE.search(text):
        return "UNVERIFIED", code, "bot challenge page"
    m = _VERIFY_CLOSED_RE.search(text)
    if m:
        return "DEAD", code, "closed marker: %s" % m.group(0)[:40]
    if "html" not in ctype.lower():
        return "UNVERIFIED", code, "non-html content-type"
    if len(text) < _VERIFY_MIN_HTML:
        return "UNVERIFIED", code, "body too short (%d)" % len(text)
    if not _verify_host_ok(final):
        return "UNVERIFIED", code, "final host not employer/ATS"
    toks = _verify_title_tokens(title)
    if not toks:
        return "UNVERIFIED", code, "no title to match"
    low = text.lower()
    hits = sum(1 for t in toks if t in low)
    if hits * 2 < len(toks):
        return "UNVERIFIED", code, "title tokens %d/%d" % (hits, len(toks))
    if not _VERIFY_APPLY_RE.search(text):
        return "UNVERIFIED", code, "no apply affordance"
    return "VERIFIED", code, "posting actionable"


def run_packets(report_body, han_turn_fn, notes_sink, send=True,
                *, verify_urls=False):
    """Returns dict telemetry. han_turn_fn(prompt) -> (ok, status, body).
    Partial failure per job is recorded, never silently dropped."""
    tele = {"selected": 0, "packets": 0, "blocked": 0, "failed": 0,
            "verified_dead": 0, "verified_unverified": 0,
            "verified_ok": 0, "tailor_rejected": 0, "fallback": 0,
            "email": "not-sent"}
    day = time.strftime("%Y-%m-%d", time.gmtime())
    corpus = {"resume": read(RESUME_PATH), "profile": read(PROFILE_PATH)}
    corpus_all = corpus["resume"] + "\n" + corpus["profile"]
    try:
        lists = json.loads(read(os.path.join(CAREER_ROOT,
                                             "plan_lists.json")))
    except (OSError, ValueError):
        lists = {}
        notes_sink.append("packets: plan_lists unreadable, exclusions "
                          "EMPTY -- selector gate weakened")
    mem = load_app_mem()
    jobs, sel_notes = parse_selected(
        report_body, lists.get("exclude_companies", []), mem, MAX_PACKETS)
    notes_sink.extend(sel_notes)
    tele["selected"] = len(jobs)
    if verify_urls:
        _live = []
        for _job in jobs:
            # 2- or 3-tuple tolerated: legacy stubs return (status, code).
            _res = verify_apply_url(
                _job["url"], title=_job.get("title") or _job.get("role") or "")
            _status, _code = _res[0], _res[1]
            _reason = _res[2] if len(_res) > 2 else ""
            if _status == "DEAD":
                tele["verified_dead"] += 1
                set_state(mem, _job["url"], "SUPPRESSED")
                notes_sink.append("verify: DROPPED dead link (%s %s) %s"
                                  % (_code, _reason, _job["url"][:70]))
                continue
            if _status == "UNVERIFIED":
                tele["verified_unverified"] += 1
                _job["verify_status"] = "UNVERIFIED"
                notes_sink.append("verify: unverifiable (%s %s) %s"
                                  % (_code, _reason, _job["url"][:70]))
            else:
                tele["verified_ok"] += 1
            _live.append(_job)
        jobs = _live
    packets, blocked = [], []
    if not PERSONA_TOKENS:
        notes_sink.append("persona scrub inactive: no tokens at %s"
                          % PERSONA_TOKENS_PATH)
    for job in jobs:
        set_state(mem, job["url"], "SELECTED", company=job["company"],
                  role=job["role"])
        try:
            g = generate_for_job(job, corpus, corpus_all, han_turn_fn, day, notes_sink)
            tele["tailor_rejected"] += len([a for a in g["attempts"] if a[0] != "base"])
            if g["kind"] == "BLOCKED":
                violations = [p for _n, ps in g["attempts"] for p in ps]
                set_state(mem, job["url"], "GENERATION_BLOCKED",
                          violations=violations)
                blocked.append({"job": job, "violations": violations,
                                "gen": g["result"], "attempts": g["attempts"]})
                tele["blocked"] += 1
                continue
            pdir = os.path.dirname(g["meta"]["artifacts"]["resume_pdf"])
            extra = {"base_resume_fallback": True,
                     "tailor_blocked": [p for _n, ps in g["attempts"] for p in ps]} if g["fallback"] else {}
            set_state(mem, job["url"], "PACKET_READY", packet_dir=pdir, **extra)
            packets.append({"job": job, "meta": g["meta"],
                            "resume_text": g["result"].resume_text,
                            "gen": g["result"], "attempts": g["attempts"],
                            "fallback": g["fallback"]})
            tele["packets"] += 1
            tele["fallback"] += bool(g["fallback"])
        except Exception as e:
            tele["failed"] += 1
            notes_sink.append("packet FAILED for %s: %s"
                              % (job["url"][:60], str(e)[:160]))
    save_app_mem(mem)
    if packets or blocked:
        subject, body, atts = build_email(packets, blocked, day)
        atomic_write(os.path.join(CAREER_ROOT, "reports",
                                  "email-%s.txt" % day),
                     subject + "\n\n" + body)
        if send:
            try:
                r = send_email(subject, body, atts)
                tele["email"] = "sent:%s" % r.get("id", "?")
                for p in packets:
                    set_state(mem, p["job"]["url"], "SENT_TO_OPERATOR")
                save_app_mem(mem)
            except Exception as e:
                tele["email"] = "FAILED: %s" % str(e)[:160]
                notes_sink.append("email send failed; packets remain in "
                                  "lockbox: %s" % str(e)[:160])
    return tele


def cli_mark(url, state):
    mem = load_app_mem()
    if url not in mem:
        print("unknown url (not in application memory)")
        return 1
    set_state(mem, url, state)
    save_app_mem(mem)
    print("%s -> %s" % (url, state))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "mark":
        if len(sys.argv) != 4:
            sys.exit("usage: career_packets.py mark <url> <STATE>")
        sys.exit(cli_mark(sys.argv[2], sys.argv[3]))
    sys.exit("library module; use career_hunter_daily.py or 'mark'")
