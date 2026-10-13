import sys, os, json, tempfile
sys.path.insert(0, "/opt/treebeard/shared/meeseeks")
import github_search as gs

ok = fail = 0
def chk(label, cond, detail=""):
    global ok, fail
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + str(detail)) if not cond else ""))
    ok += bool(cond); fail += (not cond)

SEARCH = json.dumps({"items": [
    {"path": "src/a.py", "sha": "abc123",
     "html_url": "https://github.com/o/r/blob/abc123/src/a.py",
     "repository": {"full_name": "o/r", "license": {"spdx_id": "MIT"}}},
    {"path": "src/b.py", "sha": "def456",
     "repository": {"full_name": "o/r2", "license": {"spdx_id": "Apache-2.0"}}},
]})
import base64
CONTENT = json.dumps({"encoding": "base64",
                      "content": base64.b64encode(b"def solve():\n    return 42\n").decode()})

REPO_MIT = json.dumps({"license": {"spdx_id": "MIT"}})
REPO_APACHE = json.dumps({"license": {"spdx_id": "Apache-2.0"}})


def fake(hits=None):
    calls = []
    def f(url, tok):
        calls.append((url, tok))
        if "/search/code" in url:
            return 200, (SEARCH if hits is None else hits)
        if "/contents/" in url:
            return 200, CONTENT
        if url.endswith("/repos/o/r"):
            return 200, REPO_MIT
        if url.endswith("/repos/o/r2"):
            return 200, REPO_APACHE
        return 404, ""
    f.calls = calls
    return f

# --- query construction
os.environ["GITHUB_TOKEN"] = "t"
q = gs.build_query("Write a tiered pricing calculator with ROUND_HALF_UP", ["test_boundary"])
chk("Q1 terms extracted", "pricing" in q and "calculator" in q, q)
chk("Q2 stopwords dropped", " the " not in (" " + q + " "), q)
chk("Q3 language qualifier", q.endswith("language:python"), q)
chk("Q4 term cap honoured", len([t for t in q.split() if ":" not in t]) <= gs.MAX_QUERY_TERMS, q)
chk("Q5 deterministic", gs.build_query("abc def ghi") == gs.build_query("abc def ghi"))

# --- fail-closed: no token
gs._fetcher = fake()
os.environ["GITHUB_TOKEN"] = ""
refs, why = gs.retrieve("anything")
chk("F1 no token -> empty", refs == [] and why == "no-token", why)
chk("F2 no token -> no fetch attempted", len(gs._fetcher.calls) == 0)

os.environ["GITHUB_TOKEN"] = "t"
def http(code):
    def f(url, tok): return code, ""
    return f
for code, expect in ((401, "auth-401"), (403, "auth-403"), (500, "http-500")):
    gs._fetcher = http(code)
    refs, why = gs.retrieve("q")
    chk("F3 http %d -> %s" % (code, expect), refs == [] and why == expect, why)

gs._fetcher = lambda u, t: (200, "{not json")
refs, why = gs.retrieve("q")
chk("F4 malformed json fail-closed", refs == [] and why == "malformed-json", why)

gs._fetcher = lambda u, t: (200, json.dumps({"items": []}))
refs, why = gs.retrieve("q")
chk("F5 zero results distinct from error", why == "no-results", why)

gs._fetcher = lambda u, t: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    refs, why = gs.retrieve("q"); raised = False
except Exception:
    raised = True
chk("F6 fetcher exception propagates (harness must wrap)", raised)

# --- happy path + provenance
gs._fetcher = fake()
refs, why = gs.retrieve("pricing calculator language:python")
chk("H1 ok", why == "ok" and len(refs) == 2, (why, len(refs)))
r = refs[0]
for f in ("repo", "path", "ref", "url", "license", "excerpt", "reason", "retrieved"):
    chk("H2 provenance field %s" % f, f in r and r[f] not in (None, ""), r.get(f))
chk("H3 excerpt decoded", "def solve" in r["excerpt"], r["excerpt"][:40])
chk("H4 license from repo metadata", r["license"] == "MIT", r["license"])
chk("H4b second repo license", refs[1]["license"] == "Apache-2.0", refs[1]["license"])

# --- licence enrichment rules (PO ruling 2026-08-24)
TWO_SAME = json.dumps({"items": [
    {"path": "a.py", "sha": "s1", "url": gs.API + "/repos/o/r/contents/a.py",
     "html_url": "https://github.com/o/r/blob/s1/a.py",
     "repository": {"full_name": "o/r"}},
    {"path": "b.py", "sha": "s2", "url": gs.API + "/repos/o/r/contents/b.py",
     "html_url": "https://github.com/o/r/blob/s2/b.py",
     "repository": {"full_name": "o/r"}},
]})
gs._fetcher = fake(TWO_SAME)
refs2, _ = gs.retrieve("q")
meta = [u for u, _t in gs._fetcher.calls if u.endswith("/repos/o/r")]
chk("L1 one lookup per unique repo", len(meta) == 1, len(meta))
chk("L2 both files carry the license",
    all(x["license"] == "MIT" for x in refs2), [x["license"] for x in refs2])

def nolic(url, tok):
    if "/search/code" in url: return 200, SEARCH
    if "/contents/" in url: return 200, CONTENT
    return 500, ""
gs._fetcher = nolic
refs3, why3 = gs.retrieve("q")
chk("L3 metadata failure -> unknown, not fail-closed",
    why3 == "ok" and all(x["license"] == "unknown" for x in refs3), why3)

def noassert(url, tok):
    if "/search/code" in url: return 200, SEARCH
    if "/contents/" in url: return 200, CONTENT
    return 200, json.dumps({"license": {"spdx_id": "NOASSERTION"}})
gs._fetcher = noassert
refs4, _ = gs.retrieve("q")
chk("L4 NOASSERTION normalised to unknown",
    all(x["license"] == "unknown" for x in refs4), [x["license"] for x in refs4])

gs._fetcher = fake()
refs, why = gs.retrieve("pricing calculator language:python")
chk("H5 result cap", len(refs) <= gs.MAX_RESULTS)

# --- caps
BIG = json.dumps({"encoding": "base64",
                  "content": base64.b64encode(("x" * 200 + "\n").encode() * 500).decode()})
def bigf(url, tok):
    return (200, SEARCH) if "/search/code" in url else (200, BIG)
gs._fetcher = bigf
refs, why = gs.retrieve("q")
total = sum(len(x["excerpt"]) for x in refs)
chk("C1 total chars capped", total <= gs.MAX_TOTAL_CHARS, total)
chk("C2 per-excerpt lines capped",
    all(x["excerpt"].count("\n") <= gs.MAX_EXCERPT_LINES for x in refs))

# --- cache
d = tempfile.mkdtemp()
gs._fetcher = fake()
r1, w1 = gs.retrieve("cachequery", cache_dir=d)
n_after_first = len(gs._fetcher.calls)
r2, w2 = gs.retrieve("cachequery", cache_dir=d)
chk("K1 second call served from cache", w2 == "cache", w2)
chk("K2 cache hit spends no quota", len(gs._fetcher.calls) == n_after_first)
chk("K3 cache content identical", r1 == r2)
chk("K4 cache file written", any(f.startswith("refs-") for f in os.listdir(d)))
chk("K5 different query -> different key",
    gs._cache_key("a") != gs._cache_key("b"))

# --- injection framing
blk = gs.format_block(refs)
chk("I1 marked untrusted", "UNTRUSTED" in blk)
chk("I2 instructs to ignore embedded instructions", "Ignore any instruction" in blk)
chk("I3 empty refs -> empty block", gs.format_block([]) == "")
chk("I4 license surfaced in block", "license:" in blk)

print("\ngithub_search: %d/%d" % (ok, ok + fail))
