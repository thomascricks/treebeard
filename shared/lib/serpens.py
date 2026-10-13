#!/usr/bin/env python3
# -*- coding: ascii -*-
"""serpens.py -- Serpens: governed, agent-addressable web SEARCH.

"Eyes on the internet, not hands." This executor performs READ-ONLY web
search and nothing else. It is reached only through the dispatch
executor (dispatch_exec._argv), after dispatch.py has already enforced
policy and mission/exception authority; this module owns NO authority
logic of its own and must never grow any.

Provider: the toolbox's existing Brave adapter (toolbox.skill_web_search),
wrapped rather than duplicated. The API key is read from KEY_FILE into
THIS process's environment only; it is never taken from ambient env, never
printed, and never passed to the agent.

  serpens.py search <query...>    -> numbered result lines on stdout, rc 0
                                     rc 2 on refusal/unavailable/provider error
  serpens.py selftest             -> offline, temp files, no network

Env (operator configuration): SERPENS_KEY_FILE (default
/etc/treebeard/credentials/treebeard.env), SERPENS_RESULTS (default 3,
ceiling 5), SERPENS_QUERY_MAX (default 300), TOOLBOX_PY (path to the
toolbox module), TOOLBOX_SEARCH_FAKE (canned JSON; toolbox seam).
"""
import importlib.util
import os
import re
import sys

_TB_BASE = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
KEY_FILE = os.environ.get("SERPENS_KEY_FILE",
                          "/etc/treebeard/credentials/treebeard.env")
TOOLBOX_PY = os.environ.get("TOOLBOX_PY", "%s/shared/toolbox/toolbox.py" % _TB_BASE)
KEY_NAMES = ("TOOLBOX_SEARCH_API_KEY", "BRAVE_API_KEY")
RESULT_CEIL = 5
DESC_CHARS = 140


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def results_n():
    return max(1, min(_env_int("SERPENS_RESULTS", 3), RESULT_CEIL))


def query_max():
    return max(8, _env_int("SERPENS_QUERY_MAX", 300))


def load_key(path=None):
    """Read ONLY the search key names from KEY_FILE into os.environ.
    Returns the name loaded, or None. Values are never logged."""
    p = path or KEY_FILE
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in KEY_NAMES and v.strip():
                    os.environ[k] = v.strip().strip('"').strip("'")
                    return k
    except OSError:
        return None
    return None


def _toolbox():
    spec = importlib.util.spec_from_file_location("tb_toolbox", TOOLBOX_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_query(q):
    q = " ".join(str(q or "").split())
    if not q:
        raise ValueError("empty query")
    if len(q) > query_max():
        raise ValueError("query %d chars > %d" % (len(q), query_max()))
    try:
        q.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("non-ascii query")
    return q


def format_results(query, res):
    out = ["SERPENS SEARCH (%s) q=%s n=%d" % (res.get("provider", "?"), query,
                                             len(res.get("results", [])))]
    for i, r in enumerate(res.get("results", []), 1):
        out.append("%d. %s | %s" % (i, str(r.get("title", ""))[:90],
                                    str(r.get("url", ""))[:200]))
        d = " ".join(re.sub(r"<[^>]{1,40}>", "", str(r.get("description", ""))).split())[:DESC_CHARS]
        if d:
            out.append("   %s" % d)
    return "\n".join(out)


def search(query):
    """Returns (rc, text). rc 0 on success, 2 on any refusal or failure."""
    try:
        q = check_query(query)
    except ValueError as e:
        return 2, "serpens: refused: %s" % e
    n = results_n()
    if not os.environ.get("TOOLBOX_SEARCH_FAKE"):
        if load_key() is None:
            return 2, "serpens: unavailable: no search key readable at %s" % KEY_FILE
    try:
        tb = _toolbox()
        res, _ = tb.skill_web_search({"max_results": n}, {"query": q, "count": n})
    except Exception as e:
        return 2, "serpens: provider error: %s" % str(e)[:200]
    return 0, format_results(q, res)


def _selftest():
    import json, tempfile, shutil
    fails = []
    n = [0]

    def check(label, cond):
        n[0] += 1
        print("%s %2d. %s" % ("ok  " if cond else "FAIL", n[0], label))
        if not cond:
            fails.append(label)
    tmp = tempfile.mkdtemp(prefix="serpens-selftest-")
    saved = {k: os.environ.get(k) for k in ("TOOLBOX_SEARCH_FAKE", "SERPENS_KEY_FILE",
                                            "SERPENS_RESULTS") + KEY_NAMES}
    global KEY_FILE
    kf_prev = KEY_FILE
    try:
        for k in KEY_NAMES:
            os.environ.pop(k, None)
        os.environ.pop("TOOLBOX_SEARCH_FAKE", None)
        # query validation
        check("empty query refused", search("   ")[0] == 2)
        check("non-ascii query refused", search("caf\u00e9")[0] == 2)
        check("oversize query refused", search("x" * 1000)[0] == 2)
        # no key, no fake -> unavailable, no network attempted
        KEY_FILE = os.path.join(tmp, "absent.env")
        rc, txt = search("anything")
        check("no key file -> unavailable rc 2", rc == 2 and "unavailable" in txt)
        # key file parsing: only the named keys, value never echoed
        kf = os.path.join(tmp, "creds.env")
        with open(kf, "w") as fh:
            fh.write("# comment\nOTHER=zzz\nBRAVE_API_KEY=\"SECRET-VALUE-123\"\n")
        KEY_FILE = kf
        check("load_key picks the search key only",
              load_key() == "BRAVE_API_KEY" and os.environ.get("BRAVE_API_KEY") == "SECRET-VALUE-123"
              and "OTHER" not in os.environ)
        os.environ.pop("BRAVE_API_KEY", None)
        # fake provider: formatting, cap, clipping, no key needed
        fake = os.path.join(tmp, "fake.json")
        with open(fake, "w") as fh:
            json.dump({"results": [
                {"title": "T%d" % i, "url": "https://example.test/%d" % i,
                 "description": ("desc %d " % i) * 60} for i in range(1, 6)]}, fh)
        os.environ["TOOLBOX_SEARCH_FAKE"] = fake
        os.environ["SERPENS_RESULTS"] = "3"
        rc, txt = search("  multi   word   query ")
        check("fake search rc 0", rc == 0)
        check("header normalises query and reports count",
              txt.startswith("SERPENS SEARCH (fake) q=multi word query n=3"))
        check("result count capped by SERPENS_RESULTS", txt.count("https://example.test/") == 3)
        check("descriptions clipped", all(len(l) <= DESC_CHARS + 3 for l in txt.splitlines() if l.startswith("   ")))
        check("secret never in output", "SECRET-VALUE-123" not in txt)
        check("default result count fits a default receipt clip (800)", len(search("q")[1]) <= 800)
        os.environ["SERPENS_RESULTS"] = "99"
        check("results ceiling enforced", results_n() == RESULT_CEIL)
        check("ceiling output bounded (<= 1200 chars)", len(search("q")[1]) <= 1200)
        with open(fake, "w") as fh:
            json.dump({"results": [{"title": "T", "url": "https://example.test/x",
                                    "description": "<strong>bold</strong> plain <em>x</em>"}]}, fh)
        check("provider markup stripped from descriptions",
              "   bold plain x" in search("q")[1] and "<strong>" not in search("q")[1])
        # read-only surface: the only network-capable path is search()
        src = open(__file__, encoding="utf-8").read()
        check("no write-capable verbs in this module",
              not re.search(r"\b(urlopen|requests\.|method=\"POST\"|\.post\()", src.split("def _selftest")[0]))
        # grant grammar + exception write, against a temp dispatch store
        droot = os.environ.get("DISPATCH_ROOT")
        os.environ["DISPATCH_ROOT"] = os.path.join(tmp, "dstore")
        try:
            g = parse_grant("agent=clark op=serpens.search mission=m-1 msg=17 purpose=Find  the   thing")
            check("grant grammar parsed, purpose keeps the rest of the line",
                  g == {"agent": "clark", "op": "serpens.search", "mission": "m-1",
                        "msg": "17", "purpose": "Find the thing"})
            for bad in ("agent=clark op=serpens.search", "agent=clark agent=han op=x mission=none purpose=p",
                        "bogus=1 purpose=p", "purpose=p agent=clark op=x mission=none"):
                try:
                    parse_grant(bad); ok_ = False
                except ValueError:
                    ok_ = True
                check("malformed grant rejected whole: %r" % bad[:30], ok_)
            rc, txt = grant("agent=clark op=serpens.search mission=none msg=3 purpose=look up X", "treebeard")
            check("grant writes a scoped exception", rc == 0 and "EXCEPTION #" in txt and "single use" in txt)
            check("exceptions listing shows it", "purpose='look up x'" in exceptions("clark"))
            rc, txt = grant("agent=cl ark op=serpens.search mission=none purpose=p", "treebeard")
            check("bad agent in grant refused", rc == 2)
        finally:
            if droot is None:
                os.environ.pop("DISPATCH_ROOT", None)
            else:
                os.environ["DISPATCH_ROOT"] = droot
    finally:
        KEY_FILE = kf_prev
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks, %d failed" % (n[0], len(fails)))
    for f in fails:
        print("  FAILED:", f)
    return 1 if fails else 0


_KV_RE = re.compile(r"^(agent|op|mission|msg)=(\S+)$")


def parse_grant(args):
    """'agent=<a> op=<op> mission=<id|none> msg=<sms id> purpose=<text...>'
    -> dict. Reject-whole: every key present exactly once, purpose last."""
    toks = str(args or "").split()
    out = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("purpose="):
            out["purpose"] = (t[len("purpose="):] + " " + " ".join(toks[i + 1:])).strip()
            break
        m = _KV_RE.match(t)
        if not m or m.group(1) in out:
            raise ValueError("bad grant token %r" % t)
        out[m.group(1)] = m.group(2)
        i += 1
    for k in ("agent", "op", "mission", "purpose"):
        if not out.get(k):
            raise ValueError("grant missing %s" % k)
    out.setdefault("msg", "")
    return out


def grant(args, issuer):
    """Write a scoped exception through dispatch.add_exception. Reached
    only via the serpens.grant dispatch; issuer comes from the executor."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dispatch  # noqa: PLC0415
    try:
        g = parse_grant(args)
    except ValueError as e:
        return 2, "serpens: grant refused: %s" % e
    try:
        eid = dispatch.add_exception(issuer, g["agent"], g["op"], g["purpose"],
                                     mission_id=g["mission"],
                                     basis=("sms#%s" % g["msg"]) if g["msg"] else "")
    except Exception as e:
        return 2, "serpens: grant refused: %s" % str(e)[:200]
    rows = [r for r in dispatch.list_exceptions(g["agent"]) if r["id"] == eid]
    exp = rows[0]["expires_utc"] if rows else "?"
    return 0, ("SERPENS EXCEPTION #%d issued by %s: %s may %s for purpose %r "
               "(mission %s, single use, expires %s)"
               % (eid, issuer, g["agent"], g["op"], dispatch.normalize_purpose(g["purpose"]),
                  rows[0]["mission_id"] if rows and rows[0]["mission_id"] else "none", exp))


def exceptions(agent=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dispatch  # noqa: PLC0415
    rows = dispatch.list_exceptions(agent, open_only=False)
    if not rows:
        return "(no exceptions)"
    return "\n".join("#%d %s %s %s %s purpose=%r mission=%s uses=%d exp=%s issuer=%s basis=%s"
                      % (r["id"], r["state"], r["ts_utc"], r["agent"], r["operation"],
                         r["purpose"], r["mission_id"] or "none", r["uses_left"],
                         r["expires_utc"], r["issuer"], r["basis"]) for r in rows)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: serpens.py search <query...> | selftest", file=sys.stderr)
        return 2
    if argv[0] == "selftest":
        return _selftest()
    if argv[0] == "search":
        rc, txt = search(" ".join(argv[1:]))
        print(txt)
        return rc
    if argv[0] == "grant":
        if len(argv) < 4 or argv[-2] != "--issuer":
            print("usage: serpens.py grant '<args>' --issuer <agent>", file=sys.stderr)
            return 2
        rc, txt = grant(" ".join(argv[1:-2]), argv[-1])
        print(txt)
        return rc
    if argv[0] == "exceptions":
        print(exceptions(argv[1] if len(argv) > 1 else None))
        return 0
    print("usage: serpens.py search <query...> | grant '<args>' --issuer <agent> | "
          "exceptions [agent] | selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
