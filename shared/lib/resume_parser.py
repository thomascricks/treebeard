#!/usr/bin/env python3
"""
resume_parser.py -- fleet-wide resume -> profile parser (Han #6)

v1.1: reads the operator's real resume format in addition to the simple
form. Pure parsing. No network, no storage, no LLM calls, no side effects.

Handles:
  - Section headers wrapped in === banner padding (padding stripped before
    the length check) and matched by keyword, so "EDUCATION & CERTIFICATIONS",
    "CORE PROFESSIONAL SKILLS", "PROFESSIONAL EXPERIENCE" all register.
  - Roles written as an ALL-CAPS title line ABOVE a "Company | dates" line,
    as well as the simple "Title at Company  dates" single-line form.
  - "Company | YYYY" single-year roles (no range).

Output shape mirrors Han's Mission 001 INTAKE REQUIREMENTS. The parser fills
the resume-derived fields; operator-supplied intake fields stay reserved.

Fleet-wide: both Clark and Han consume the operator profile -> shared/lib.

Callers:
    from resume_parser import build_profile
    profile = build_profile(resume_text)

CLI (RUN-invocable):
    python3 resume_parser.py --file /path/to/resume.txt   # prints JSON
    python3 resume_parser.py --selftest
"""

import re
import sys
import json
import argparse
from datetime import datetime, timezone

PARSER_VERSION = "1.2"

# ---- field extractors --------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d().\-\s]{7,}\d)(?!\d)")
DATE_TOKEN = (
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*)?\d{4}"
    r"|Present|Current|Now|\d{1,2}/\d{4}"
)
RANGE_RE = re.compile(
    rf"({DATE_TOKEN})\s*(?:-|\u2013|\u2014|to)\s*({DATE_TOKEN})", re.IGNORECASE
)
YEAR_ONLY_RE = re.compile(r"\|\s*((?:19|20)\d{2})\s*$")
BANNER_RE = re.compile(r"={3,}")
BULLET_RE = re.compile(r"^[\-\u2022*\u00b7]\s+")

# Header keyword -> canonical section. Order matters: first hit wins, so a
# "core competencies summary" resolves to skills, not summary.
HEADING_KEYWORDS = [
    ("experience", ["experience", "employment", "work history", "career"]),
    ("education", ["education", "academic"]),
    ("skills", ["skills", "competenc", "expertise", "proficienc"]),
    ("summary", ["summary", "objective", "professional summary", "about"]),
]


def _strip_banner(line):
    """Remove leading/trailing = padding and surrounding whitespace."""
    return re.sub(r"^[=\s]+|[=\s]+$", "", line.strip())


def _heading_canon(line):
    """Return a canonical section name for a header line, else None. Works
    whether or not the header is wrapped in === banner padding. A header must
    LOOK like one -- banner-wrapped or mostly-uppercase -- so ordinary
    sentences that merely contain a keyword ('...one line summary...') are
    not misread as section headers."""
    banner = bool(BANNER_RE.search(line))
    core = _strip_banner(line)
    if not core:
        return None
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return None
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if not (banner or upper_ratio > 0.7):
        return None
    norm = " ".join(re.sub(r"[^a-z ]", " ", core.lower()).split())
    if not norm or len(norm) > 50:
        return None
    for canon, kws in HEADING_KEYWORDS:
        for kw in kws:
            if kw in norm:
                return canon
    return None


def _looks_like_title(line):
    """A short, mostly-uppercase line -> a role title sitting above its
    company/date line."""
    s = line.strip()
    if not s or len(s) > 60:
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.7


def _split_sections(text):
    """Return {section: [lines]} plus a 'head' block before the first
    recognized section. Named banners with no known canon start an 'other'
    bucket, which cleanly ends the prior (e.g. experience) section."""
    sections = {"head": []}
    current = "head"
    for line in text.splitlines():
        line = line.rstrip()
        canon = _heading_canon(line)
        if canon:
            current = canon
            sections.setdefault(current, [])
            continue
        if BANNER_RE.search(line):
            core = _strip_banner(line)
            if core:                      # named banner, unknown canon
                current = "other"
                sections.setdefault(current, [])
            continue                      # bare divider: skip, keep current
        sections.setdefault(current, []).append(line)
    return sections


def _extract_contact(head_lines, full_text):
    email = EMAIL_RE.search(full_text)
    phone = PHONE_RE.search(full_text)
    location = None
    for l in head_lines:
        for chunk in re.split(r"\s*\|\s*", l.strip()):
            c = chunk.strip()
            if re.match(r"^[A-Za-z .'-]+,\s*[A-Za-z][A-Za-z .]{1,}\.?$", c) and len(c) < 40:
                location = c
                break
        if location:
            break
    return {
        "email": email.group(0) if email else None,
        "phone": phone.group(1).strip() if phone else None,
        "location": location,
    }


def _extract_name(head_lines):
    for l in head_lines:
        s = l.strip()
        if not s or EMAIL_RE.search(s) or PHONE_RE.search(s):
            continue
        toks = s.split()
        if 1 < len(toks) <= 4 and all(re.match(r"^[A-Za-z.'-]+$", t) for t in toks):
            return s
    return head_lines[0].strip() if head_lines else None


def _split_title_company(header):
    for sep in [" at ", " @ ", " | ", " - ", " \u2013 ", ", "]:
        if sep in header:
            a, b = header.split(sep, 1)
            return a.strip(), b.strip()
    return (header.strip() or None), None


def _extract_experience(exp_lines):
    """Group into entries. A role begins at a line carrying a date (range or
    single year). Title comes from the line itself ("Title at Company dates")
    or, in the operator's format, from the ALL-CAPS line above the
    "Company | dates" line. Bullets beneath attach to the current role."""
    entries = []
    cur = None
    pending_title = None
    for raw in exp_lines:
        line = raw.strip()
        if not line:
            continue
        is_bullet = bool(BULLET_RE.match(raw)) or raw.startswith("  ")
        if is_bullet and cur:
            cur["bullets"].append(BULLET_RE.sub("", line).strip())
            continue
        rng = RANGE_RE.search(line)
        yr = YEAR_ONLY_RE.search(line)
        pipe = "|" in line
        if (rng or (yr and pipe)) and not is_bullet:
            if cur:
                entries.append(cur)
            if pipe:
                company = line.split("|")[0].strip()
                if rng:
                    start, end = rng.group(1).strip(), rng.group(2).strip()
                else:
                    start = end = yr.group(1)
                title = pending_title
            else:
                header = RANGE_RE.sub("", line).strip(" ,-\u2013\u2014|")
                title, company = _split_title_company(header)
                start, end = rng.group(1).strip(), rng.group(2).strip()
            cur = {"title": title, "company": company,
                   "start": start, "end": end, "bullets": []}
            pending_title = None
            continue
        if _looks_like_title(line):
            pending_title = line
    if cur:
        entries.append(cur)
    return entries


def _extract_skills(skill_lines):
    parts = []
    for l in skill_lines:
        s = BULLET_RE.sub("", l.strip()).strip()
        if not s or _looks_like_title(s):      # drop sub-headers like "TECHNICAL COMPETENCIES"
            continue
        for piece in re.split(r"[,;\u2022\u00b7|]|\s{2,}", s):
            piece = piece.strip()
            if piece and 1 < len(piece) < 60:
                parts.append(piece)
    return parts


def _extract_education(edu_lines):
    out = []
    for l in edu_lines:
        s = BULLET_RE.sub("", l.strip()).strip()
        if not s or _looks_like_title(s):
            continue
        year = re.search(r"\b(?:19|20)\d{2}\b", s)
        out.append({
            "institution": re.sub(r"\b(?:19|20)\d{2}\b", "", s).strip(" ,-\u2013\u2014"),
            "credential": None,
            "year": year.group(0) if year else None,
        })
    return out


def _extract_summary(summary_lines):
    txt = " ".join(l.strip() for l in summary_lines if l.strip())
    return txt or None


# ---- intake reader (operator-supplied answers) -------------------------
#
# The intake file is a labeled block written by the operator; sections are
# "--- NAME ---" banners. Section-level capture is robust; targeted label
# and regex extraction is used only where a clean value is reliable.

_INTAKE_SEC_RE = re.compile(r"^\s*---\s*(.+?)\s*---\s*$")
_INTAKE_LABEL_RE = re.compile(r"^\s*([A-Za-z0-9 ()/'\"-]{1,45}?):\s*(.*)$")
_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


def _intake_sections(text):
    secs, cur = {}, None
    for line in text.splitlines():
        m = _INTAKE_SEC_RE.match(line)
        if m:
            cur = m.group(1).strip().lower()
            secs[cur] = []
        elif cur is not None:
            secs[cur].append(line)
    return secs


def _sec_lines(secs, *needles):
    for k, v in secs.items():
        if all(n in k for n in needles):
            return v
    return []


def _sec_blob(secs, *needles):
    lines = [l.strip() for l in _sec_lines(secs, *needles) if l.strip()]
    return " ".join(lines) or None


def _label_value(lines, needle):
    """Value of the first 'Label: value' line whose label contains needle,
    including any following non-label continuation lines."""
    val, collecting = [], False
    for raw in lines:
        m = _INTAKE_LABEL_RE.match(raw)
        if m and needle in m.group(1).strip().lower():
            val = [m.group(2).strip()]
            collecting = True
            continue
        if collecting:
            if m:                       # a new label ends the value
                break
            if raw.strip():
                val.append(raw.strip())
    out = " ".join(v for v in val if v).strip()
    return out or None


def _split_list(value, seps=";,"):
    if not value:
        return []
    parts = re.split(f"[{re.escape(seps)}]", value)
    return [p.strip() for p in parts if p.strip()]


def read_intake(intake_text):
    """Parse the operator intake block into structured fields. Best-effort and
    non-brittle: prose fields keep their text; only clean values are split."""
    if not isinstance(intake_text, str):
        raise TypeError("intake_text must be a string")
    secs = _intake_sections(intake_text)

    roles = _sec_lines(secs, "target", "role")
    comp = _sec_blob(secs, "compensation") or _sec_blob(secs, "salary")
    money = _MONEY_RE.findall(comp or "")

    return {
        "current_title_confirmed": _label_value(roles, "current title"),
        "target_roles": {
            "primary": _label_value(roles, "primary"),
            "secondary": _label_value(roles, "secondary"),
        },
        "industries_open": _label_value(_sec_lines(secs, "industr"), "willing"),
        "industries_note": _label_value(_sec_lines(secs, "industr"), "exception"),
        "exclude_orgs": _split_list(
            _label_value(_sec_lines(secs, "exclusion"), "will not")
            or _sec_blob(secs, "exclusion")
        ),
        "geography_pref": _sec_blob(secs, "geograph"),
        "direction": _sec_blob(secs, "direction"),
        "salary": {
            "ideal_annual": ("$" + money[0]) if len(money) >= 1 else None,
            "median_hourly": ("$" + money[1]) if len(money) >= 2 else None,
            "floor_hourly_w2": ("$" + money[2]) if len(money) >= 3 else None,
            "note": comp,
        },
        "tired_of_explaining": _sec_blob(secs, "pitch")
        or _sec_blob(secs, "tired"),
    }


# ---- public API --------------------------------------------------------

def build_profile(resume_text, intake_text=None, agent=None):
    """Parse resume text into a structured operator profile, and if an intake
    block is supplied, merge the operator's answers into the reserved fields
    plus target roles, exclusions, salary, and direction. Pure function."""
    if not isinstance(resume_text, str):
        raise TypeError("resume_text must be a string")
    sections = _split_sections(resume_text)
    head = sections.get("head", [])
    contact = _extract_contact(head, resume_text)
    experience = _extract_experience(sections.get("experience", []))
    current_title = experience[0]["title"] if experience else None

    profile = {
        # --- parsed from resume ---
        "name": _extract_name(head),
        "contact": contact,
        "current_title": current_title,            # intake #1
        "summary": _extract_summary(sections.get("summary", [])),
        "experience": experience,
        "skills": _extract_skills(sections.get("skills", [])),
        "education": _extract_education(sections.get("education", [])),
        "industries": [],                          # intake #2 (operator-confirmed)
        # --- operator-supplied intake (filled by merge if intake present) ---
        "target_roles": {"primary": None, "secondary": None},
        "exclude_orgs": [],
        "geography_pref": contact["location"],     # intake #3 (seed from contact)
        "target_orgs": [],                         # intake #4
        "direction": None,
        "salary_floor": None,                      # intake #5
        "tired_of_explaining": None,               # intake #6 -- the real pitch
        # --- meta ---
        "_meta": {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source": "resume",
            "parser_version": PARSER_VERSION,
            "requested_by": agent,
            "intake_merged": False,
        },
    }

    if intake_text:
        ik = read_intake(intake_text)
        profile["target_roles"] = ik["target_roles"]
        profile["exclude_orgs"] = ik["exclude_orgs"]
        profile["salary_floor"] = ik["salary"]
        profile["direction"] = ik["direction"]
        profile["tired_of_explaining"] = ik["tired_of_explaining"]
        if ik["geography_pref"]:
            profile["geography_pref"] = ik["geography_pref"]
        # industries: keep worked-in inference minimal; carry the open note
        if ik["industries_open"]:
            profile["industries"] = [ik["industries_open"]]
        if ik["current_title_confirmed"]:
            profile["_meta"]["current_title_confirmed"] = ik["current_title_confirmed"]
        profile["_meta"]["source"] = "resume+intake"
        profile["_meta"]["intake_merged"] = True

    return profile


def parse_file(path, agent=None):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return build_profile(f.read(), agent=agent)


def parse_files(resume_path, intake_path=None, agent=None):
    """Build the complete profile from the resume file and, if given, the
    operator intake file. This is the fleet entry point for the full profile."""
    with open(resume_path, "r", encoding="utf-8", errors="replace") as f:
        resume_text = f.read()
    intake_text = None
    if intake_path:
        try:
            with open(intake_path, "r", encoding="utf-8", errors="replace") as f:
                intake_text = f.read()
        except FileNotFoundError:
            intake_text = None
    return build_profile(resume_text, intake_text=intake_text, agent=agent)


# ---- selftest ----------------------------------------------------------

_FIXTURE = """JORDAN Q EXAMPLE
Springfield, MA
jqe@example.com | (555) 123-4567

SUMMARY
Product manager who turns loosely defined problems into shipped plans
across logistics, retail, and public services.

EXPERIENCE
Senior Product Manager at Acme Health  2021 - Present
- Led discovery for a scheduling platform
- Cut onboarding time by 30%
Product Owner at Globex Insurance  2018 - 2021
- Owned backlog for a billing rebuild

SKILLS
Product strategy, Roadmapping, SQL, Stakeholder management, Agile

EDUCATION
University of Somewhere  2010
"""

# Operator's real format: === banners, title-above-company, single-year role.
_FIXTURE2 = """NAME PERSON
Somewhere, CA | n@x.com

=========================================== PROFESSIONAL SUMMARY ===========================================
A one line summary of the person.

=========================================== CORE PROFESSIONAL SKILLS ===========================================
PRODUCT MANAGEMENT
• Roadmapping
• Backlog ownership

=========================================== PROFESSIONAL EXPERIENCE ===========================================
SENIOR THING DOER
Acme Corp | 2020-2024 | 4 years
Owned and led the thing across two teams.
Key Achievements:
• Did alpha
• Did beta
JUNIOR THING DOER
Beta LLC | 2018
• Did gamma

=========================================== EDUCATION & CERTIFICATIONS ===========================================
• BS in Something - Some University 2012
"""


# Operator intake block, in the real labeled format.
_INTAKE_FIXTURE = """OPERATOR INTAKE — test

--- TARGET ROLES ---
Current title (most recent):   Senior Thing Doer
Primary target:                Product Manager
Acceptable secondary:          Product Owner; Business Analyst (a step back);
                               Scrum Master

--- INDUSTRIES ---
Worked in:                     logistics, retail
Willing:                       industry-agnostic

--- HARD EXCLUSIONS (do not target) ---
Will not work for:             Acme Bad Co; Evil LLC

--- GEOGRAPHY ---
Commute:                       Boston or Springfield
Remote:                        yes
Relocation:                    no

--- DIRECTION / DREAM ---
Long-term goal:                Example Goal

--- COMPENSATION ---
Ideal:                         $120,000 / year
Median (will take):            $55 / hour
Absolute floor (W2 perm):      $50 / hour

--- THE PITCH (tired of explaining) ---
An example pitch line.
"""


def _raises_typeerror():
    try:
        build_profile(None)
        return False
    except TypeError:
        return True


def _selftest():
    checks = []

    # --- v1 simple-format fixture (no regression) ---
    p = build_profile(_FIXTURE, agent="selftest")
    checks += [
        ("v1 name", p["name"] == "JORDAN Q EXAMPLE"),
        ("v1 email", p["contact"]["email"] == "jqe@example.com"),
        ("v1 phone_present", p["contact"]["phone"] is not None),
        ("v1 location", p["contact"]["location"] == "Springfield, MA"),
        ("v1 summary_present", bool(p["summary"])),
        ("v1 exp_count", len(p["experience"]) == 2),
        ("v1 current_title", p["current_title"] == "Senior Product Manager"),
        ("v1 first_company", p["experience"][0]["company"] == "Acme Health"),
        ("v1 first_end", p["experience"][0]["end"].lower() == "present"),
        ("v1 bullets", len(p["experience"][0]["bullets"]) == 2),
        ("v1 skills", "SQL" in p["skills"]),
        ("v1 education", any(e["year"] == "2010" for e in p["education"])),
        ("v1 intake_reserved", p["salary_floor"] is None and p["target_orgs"] == []),
        ("v1 meta_version", p["_meta"]["parser_version"] == PARSER_VERSION),
        ("v1 type_guard", _raises_typeerror()),
    ]

    # --- operator real-format fixture (new behavior) ---
    q = build_profile(_FIXTURE2, agent="selftest")
    checks += [
        ("v2 name", q["name"] == "NAME PERSON"),
        ("v2 banner_summary", bool(q["summary"])),
        ("v2 exp_count", len(q["experience"]) == 2),
        ("v2 title_from_above", q["current_title"] == "SENIOR THING DOER"),
        ("v2 first_company", q["experience"][0]["company"] == "Acme Corp"),
        ("v2 range_dates", q["experience"][0]["start"] == "2020"
         and q["experience"][0]["end"] == "2024"),
        ("v2 second_title", q["experience"][1]["title"] == "JUNIOR THING DOER"),
        ("v2 single_year", q["experience"][1]["start"] == "2018"
         and q["experience"][1]["end"] == "2018"),
        ("v2 bullets_attach", len(q["experience"][0]["bullets"]) == 2),
        ("v2 skills_present", len(q["skills"]) >= 2),
        ("v2 subheader_dropped", "PRODUCT MANAGEMENT" not in q["skills"]),
        ("v2 education_present", len(q["education"]) >= 1),
    ]

    # --- intake merge fixture (v1.2 profile-builder half) ---
    m = build_profile(_FIXTURE, intake_text=_INTAKE_FIXTURE, agent="selftest")
    checks += [
        ("v3 merged_flag", m["_meta"]["intake_merged"] is True),
        ("v3 source", m["_meta"]["source"] == "resume+intake"),
        ("v3 primary_role", m["target_roles"]["primary"] == "Product Manager"),
        ("v3 secondary_role", "Business Analyst" in (m["target_roles"]["secondary"] or "")),
        ("v3 exclusions_list", m["exclude_orgs"] == ["Acme Bad Co", "Evil LLC"]),
        ("v3 salary_ideal", m["salary_floor"]["ideal_annual"] == "$120,000"),
        ("v3 salary_floor_hr", m["salary_floor"]["floor_hourly_w2"] == "$50"),
        ("v3 geography", "remote" in (m["geography_pref"] or "").lower()),
        ("v3 pitch", bool(m["tired_of_explaining"])),
        ("v3 resume_intact", len(m["experience"]) == 2),
        ("v3 no_intake_default", build_profile(_FIXTURE)["_meta"]["intake_merged"] is False),
    ]

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"resume_parser selftest: {passed}/{len(checks)}")
    return passed == len(checks)


# ---- cli ---------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(description="resume -> profile parser")
    ap.add_argument("--file", help="path to a resume text file")
    ap.add_argument("--intake", help="path to the operator intake file (optional)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--agent", default=None)
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if args.file:
        print(json.dumps(
            parse_files(args.file, args.intake, agent=args.agent), indent=2))
        return
    data = sys.stdin.read()
    if data.strip():
        print(json.dumps(build_profile(data, agent=args.agent), indent=2))
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    _main()
