#!/usr/bin/env python3
"""selector_regress.py -- Stage 0 mission-selector regression against the
PRIVATE corpus (A+ ROUTER, operator ruling 2026-09-04).

Calls ask._lex_decide -- the single production Stage 0 decision -- over the
real routing_index(agent) read-only. Nothing is re-implemented here.
Corpus rows (JSONL): {"added","agent","task","expect","note"[,"candidates"]}
  expect = "<mission_id>"  -> path explicit|lexical selecting that id
  expect = "none"          -> path model (zero lexical signal; fallback stage)
  expect = "tie"           -> path tie with exactly `candidates`
Exit 0 all pass; 1 any miss; 2 corpus missing/malformed.
Corpus is private routing evidence: mission ids and tasks only, never
charter text, never published."""
import json, os, sys

ASK_DIR = os.environ.get("TREEBEARD_ASK_DIR", "/opt/treebeard/treebeard")
LIB = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.environ.get("SELECTOR_CORPUS",
                        "/opt/treebeard/shared/missions/selector_corpus.jsonl")
for p in (LIB, ASK_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
import ask  # noqa: E402  (production selector; _lex_decide is the contract)


def judge(row, rec):
    """(ok, detail) -- compare one decision record with a corpus expectation."""
    exp = row["expect"]
    path = rec.get("path")
    if exp == "none":
        return path == "model", "path=%s" % path
    if exp == "tie":
        want = sorted(row.get("candidates") or [])
        got = sorted(rec.get("tie") or [])
        return (path == "tie" and got == want), "path=%s tie=%s" % (path, got)
    return (path in ("explicit", "lexical") and rec.get("mission_id") == exp), \
        "path=%s sel=%s" % (path, rec.get("mission_id"))


def run(rows, index_for, out=sys.stdout):
    misses = 0
    cache = {}
    for i, row in enumerate(rows, 1):
        ag = row["agent"]
        if ag not in cache:
            cache[ag] = index_for(ag)
        rec = ask._lex_decide(row["task"], cache[ag])
        ok, detail = judge(row, rec)
        misses += 0 if ok else 1
        sc = {k[-8:]: v for k, v in (rec.get("scores") or {}).items()}
        print("  %s %2d. [%s] %-48s %s scores=%s" % ("PASS" if ok else "MISS", i, ag,
              row["task"][:48], detail, sc), file=out)
    return misses


def load_corpus(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            r = json.loads(line)
            for k in ("agent", "task", "expect"):
                if k not in r:
                    raise ValueError("line %d missing %r" % (n, k))
            if r["expect"] == "tie" and not r.get("candidates"):
                raise ValueError("line %d: expect=tie needs candidates" % n)
            rows.append(r)
    if not rows:
        raise ValueError("corpus is empty")
    return rows


def _selftest():
    if not __debug__:
        # Assertions are stripped under -O: this selftest would verify
        # nothing and still exit 0. Fail closed (optmode contract: REFUSE).
        print("selector_regress: selftest requires assertions; refusing to run under -O",
              file=sys.stderr)
        raise SystemExit(2)
    import io
    idx = {"mm": [
        {"mission_id": "m-alpha", "title": "Alpha area", "summary": "widgets and gears.", "routing_terms": ""},
        {"mission_id": "m-beta", "title": "Beta area", "summary": "kites and string.", "routing_terms": "flying"},
    ]}
    rows = [
        {"agent": "mm", "task": "fix the gear widget", "expect": "m-alpha"},
        {"agent": "mm", "task": "go flying", "expect": "m-beta"},
        {"agent": "mm", "task": "widgets and kites", "expect": "tie", "candidates": ["m-alpha", "m-beta"]},
        {"agent": "mm", "task": "what time is it", "expect": "none"},
        {"agent": "mm", "task": "under mission m-beta do it", "expect": "m-beta"},
    ]
    buf = io.StringIO()
    m = run(rows, lambda a: idx[a], out=buf)
    assert m == 0, "selftest: expected all pass, got %d misses\n%s" % (m, buf.getvalue())
    bad = [{"agent": "mm", "task": "fix the gear widget", "expect": "m-beta"},
           {"agent": "mm", "task": "widgets and kites", "expect": "tie", "candidates": ["m-alpha"]},
           {"agent": "mm", "task": "fix the gear widget", "expect": "none"}]
    m = run(bad, lambda a: idx[a], out=io.StringIO())
    assert m == 3, "selftest: miss detection failed (%d/3)" % m
    try:
        load_corpus("/nonexistent/selector_corpus.jsonl")
        raise AssertionError("missing corpus must raise")
    except (OSError, ValueError):
        pass
    print("selector_regress selftest OK (5 pass / 3 detected misses / missing-corpus raises)")


if __name__ == "__main__":
    _selftest()
    if "--selftest" in sys.argv[1:]:
        sys.exit(0)
    try:
        rows = load_corpus(CORPUS)
    except (OSError, ValueError) as e:
        print("selector corpus unusable: %s" % e, file=sys.stderr)
        sys.exit(2)
    import missions
    print("selector corpus %s (%d rows) floor=%s margin=%s" % (CORPUS, len(rows), ask._LEX_FLOOR, ask._LEX_MARGIN))
    misses = run(rows, missions.routing_index)
    print("selector corpus: %d pass, %d miss" % (len(rows) - misses, misses))
    sys.exit(1 if misses else 0)
