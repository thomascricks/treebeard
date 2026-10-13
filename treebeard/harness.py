#!/usr/bin/env python3
"""harness.py -- talk to Treebeard in plain English; enrolled tools run.

Loop: route the sentence; if the reply carries one fenced ``intent``
block, execute its single line through the toolbox, feed the clipped
result back, route again. Up to MAX_TURNS.

Execute-only by construction: run / status / cards are the only verbs
that ever reach the toolbox. enroll, allow, revoke, invokers stay
typed commands. No memory recall: the only text steering execution is
the typed sentence plus tool results. Args pass as an argv list, never
a shell, so shell metacharacters are inert bytes.

Return channel (v2): the toolbox CLI prints run *metadata* -- rid,
state, result hashes -- not the tool's product. After a run intent the
harness reads the product itself from the run directory (stdout plus
result-pool files) and feeds it back alongside the metadata, so the
model answers from what the tool actually said. Fixes the v1
blank-answer defect.

Runs as treebeard and refuses root -- the toolbox OS gate is only a
real boundary when the caller is unprivileged.
"""
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

ENV = f"{_TB_ETC}/credentials/treebeard.env"
LIB = f"{_TB_BASE}/shared/lib"
AGENT = "treebeard"
WRAPPER = f"{_TB_BASE}/shared/toolbox/toolbox"
AUDIT = f"{_TB_BASE}/treebeard/data/harness.jsonl"
RUNS_ROOT = f"{_TB_BASE}/shared/toolbox/data/runs"

MAX_TURNS = 5
MAX_REFUSALS = 2
RESULT_CLIP = 4000
INVENTORY_CLIP = 2000
PRODUCT_CLIP = 1500
MAX_RESULT_FILES = 4
TOOL_TIMEOUT = 200

ALLOWED = ("run", "status", "cards")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_FENCE_RE = re.compile(r"```\s*intent[^\n]*\n(.*?)```", re.I | re.S)
_RID_RE = re.compile(r"\brun (r[0-9a-f]+):")

SYSTEM_HEAD = (
    "You are Treebeard, overseer of a self-hosted multi-agent system, "
    "and you can operate enrolled tools.\n"
    "\n"
    "To use a tool, end your reply with exactly one fenced block, and "
    "the block holds exactly one line. Real examples of the only "
    "allowed forms:\n"
    "\n"
    "```intent\n"
    "run evens 12\n"
    "```\n"
    "\n"
    "```intent\n"
    "status evens\n"
    "```\n"
    "\n"
    "```intent\n"
    "cards\n"
    "```\n"
    "\n"
    "Write real tool names and real argument values, never placeholders "
    "or angle brackets. If no tool is needed, reply with no intent "
    "block at all. When a TOOL RESULT appears, either answer the user "
    "in plain prose or emit one more intent block.\n"
    "\n"
    "Enrolled tools and state right now:\n"
)


def load_env(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
    except PermissionError:
        sys.exit("harness.py: cannot read %s as %s.\n"
                 "Fix once, as root:  chgrp treebeard %s && chmod 640 %s"
                 % (path, getpass.getuser(), path, path))
    except OSError as e:
        sys.exit("harness.py: %s" % e)


def gate():
    u = getpass.getuser()
    if u == "root":
        sys.exit("harness.py: refusing to run as root. Drop the sudo -- "
                 "the toolbox gate only holds for an unprivileged caller.")
    if u != AGENT:
        sys.exit("harness.py: must run as %s, not %s." % (AGENT, u))


def inventory():
    try:
        p = subprocess.run([WRAPPER, "status"], capture_output=True,
                           timeout=30)
        text = (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace")
        text = text.strip()
        if len(text) > INVENTORY_CLIP:
            text = text[:INVENTORY_CLIP] + "\n...(clipped)"
        return text or "(status returned nothing)"
    except Exception as e:
        return "(tool inventory unavailable: %s)" % e


def extract_intent(text):
    """Return (line, err). (None, None) means no intent block."""
    m = _FENCE_RE.search(text or "")
    if not m:
        return None, None
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    if len(lines) != 1:
        return None, "intent block must hold exactly one line"
    return lines[0], None


def validate(line):
    """Return (argv, err)."""
    if any(c in line for c in "<>`"):
        return None, ("placeholders and backticks are not allowed; "
                      "write real values")
    if len(line) > 500:
        return None, "intent line too long"
    try:
        toks = shlex.split(line)
    except ValueError as e:
        return None, "unparseable intent: %s" % e
    if not toks:
        return None, "empty intent"
    verb = toks[0]
    if verb not in ALLOWED:
        return None, ("verb '%s' not allowed; only: %s"
                      % (verb, " ".join(ALLOWED)))
    if verb == "run":
        if len(toks) < 2 or not _NAME_RE.match(toks[1]):
            return None, "run needs a valid tool name"
        if len(toks) > 16:
            return None, "too many arguments"
    elif verb == "status":
        if len(toks) > 2 or (len(toks) == 2
                             and not _NAME_RE.match(toks[1])):
            return None, "status takes at most one valid tool name"
    elif verb == "cards":
        if len(toks) != 1:
            return None, "cards takes no arguments"
    return toks, None


def execute(toks):
    try:
        p = subprocess.run([WRAPPER] + toks, capture_output=True,
                           timeout=TOOL_TIMEOUT)
        out = (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace")
        out = out.strip()
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return 124, "(tool timed out after %ss)" % TOOL_TIMEOUT
    except OSError as e:
        return 127, "(could not invoke toolbox: %s)" % e
    if len(out) > RESULT_CLIP:
        out = out[:RESULT_CLIP] + "\n...(clipped)"
    return rc, out


def _pclip(s, n=PRODUCT_CLIP):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "\n...(clipped)"


def run_product(cli_out, root=RUNS_ROOT):
    """Read what a run actually produced: stdout plus result files.

    The toolbox CLI prints metadata; the product lives host-side under
    the run directory, keyed by the rid the toolbox itself printed.
    Fails open -- always returns text, never raises."""
    m = _RID_RE.search(cli_out or "")
    if not m:
        return "(no run id in toolbox output; product not read)"
    base = os.path.join(root, m.group(1))
    parts = []
    try:
        with open(os.path.join(base, "logs", "stdout.txt"), "rb") as f:
            s = f.read().decode("utf-8", "replace")
        if s.strip():
            parts.append("STDOUT:\n" + _pclip(s))
    except OSError as e:
        parts.append("(stdout unreadable: %s)" % e)
    rdir = os.path.join(base, "scratch", "result")
    try:
        names = sorted(os.listdir(rdir))
    except OSError:
        names = []
    for name in names[:MAX_RESULT_FILES]:
        try:
            with open(os.path.join(rdir, name), "rb") as f:
                s = f.read(PRODUCT_CLIP + 64).decode("utf-8", "replace")
            parts.append("RESULT %s:\n%s" % (name, _pclip(s)))
        except OSError as e:
            parts.append("(result %s unreadable: %s)" % (name, e))
    if len(names) > MAX_RESULT_FILES:
        parts.append("(%d more result files not shown)"
                     % (len(names) - MAX_RESULT_FILES))
    if not parts:
        return "(run produced no stdout and no result files)"
    return "\n".join(parts)


def audit(rec):
    try:
        os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
        with open(AUDIT, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print("[audit-fail %s]" % e, file=sys.stderr)


def strip_intent(text):
    return _FENCE_RE.sub("", text or "").strip()


def selftest():
    checks = []

    def ck(name, ok):
        checks.append((name, ok))
        print("%-44s %s" % (name, "ok" if ok else "FAIL"))

    l, e = extract_intent("prose\n```intent\nrun evens 12\n```\nmore")
    ck("extracts single intent line", l == "run evens 12" and e is None)
    l, e = extract_intent("no block here")
    ck("no fence -> no intent, no error", l is None and e is None)
    l, e = extract_intent("```intent\nrun a 1\nrun b 2\n```")
    ck("two lines rejected", l is None and e is not None)
    l, e = extract_intent("```INTENT\ncards\n```")
    ck("fence tag case-insensitive", l == "cards" and e is None)
    t, e = validate("run evens 12")
    ck("run allowed", t == ["run", "evens", "12"] and e is None)
    t, e = validate("enroll evens")
    ck("enroll refused", t is None)
    t, e = validate("allow evens router tiers=free")
    ck("allow refused", t is None)
    t, e = validate("run <tool> 12")
    ck("placeholder refused", t is None)
    t, e = validate("run Bad;Name 1")
    ck("bad tool name refused", t is None)
    t, e = validate("status")
    ck("bare status allowed", t == ["status"] and e is None)
    t, e = validate("cards now")
    ck("cards with args refused", t is None)
    t, e = validate("run evens $(rm -rf /)")
    ck("shell metachars inert as argv",
       t is not None and t[2] == "$(rm")
    m = _RID_RE.search("run rae68af3353f: evens -> DONE")
    ck("rid parsed from run line",
       m is not None and m.group(1) == "rae68af3353f")
    ck("no rid -> note, no read",
       run_product("enrolled evens: deny-all").startswith("(no run id"))
    import shutil
    import tempfile
    td = tempfile.mkdtemp(prefix="harness-st-")
    rd = os.path.join(td, "rdeadbeef000")
    os.makedirs(os.path.join(rd, "logs"))
    os.makedirs(os.path.join(rd, "scratch", "result"))
    with open(os.path.join(rd, "logs", "stdout.txt"), "w") as f:
        f.write("12 is even\n")
    with open(os.path.join(rd, "scratch", "result", "answer.txt"),
              "w") as f:
        f.write("12 even\n")
    prod = run_product("run rdeadbeef000: evens -> DONE", root=td)
    ck("product carries stdout and result",
       "12 is even" in prod and "RESULT answer.txt" in prod
       and "12 even" in prod)
    shutil.rmtree(td, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print("SELFTEST %d/%d %s" % (len(checks) - len(failed), len(checks),
                                 "FAIL" if failed else "PASS"))
    sys.exit(1 if failed else 0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    gate()
    sentence = " ".join(sys.argv[1:]).strip()
    if not sentence and not sys.stdin.isatty():
        sentence = sys.stdin.read().strip()
    if not sentence:
        sys.exit('harness.py: no prompt given. Usage: tt "your sentence"')

    load_env(ENV)
    sys.path.insert(0, LIB)
    import router

    system = SYSTEM_HEAD + inventory()
    transcript = sentence
    refusals = 0
    last_result = None

    for turn in range(1, MAX_TURNS + 1):
        r = router.route(system, transcript,
                         classify_text=sentence, agent=AGENT)
        print("[turn=%d ok=%s tier=%s model=%s reason=%s]"
              % (turn, r.ok, r.tier, r.model, r.reason))
        if not r.ok:
            sys.exit("(no answer) error: %s" % r.error)

        line, err = extract_intent(r.text)
        prose = strip_intent(r.text)

        if line is None and err is None:
            print("-" * 60)
            print(prose or r.text)
            return

        if prose:
            print(prose)

        if err is None:
            toks, err = validate(line)
        if err:
            refusals += 1
            print("[refused: %s]" % err)
            audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
                   "prompt": sentence[:300], "refused": err,
                   "line": (line or "")[:200]})
            if refusals > MAX_REFUSALS:
                sys.exit("harness.py: too many invalid intents; stopping.")
            transcript += ("\n\n[REFUSED: %s]\nEmit a corrected "
                           "single-line intent block, or answer without "
                           "one.\n" % err)
            continue

        print("[intent: %s]" % " ".join(toks))
        rc, out = execute(toks)
        product = run_product(out) if toks[0] == "run" else ""
        feedback = out + ("\n\n" + product if product else "")
        last_result = feedback
        audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "prompt": sentence[:300], "intent": " ".join(toks),
               "exit": rc, "tier": r.tier, "model": r.model})
        transcript += ("\n\n[TOOL RESULT exit=%d]\n%s\n\nContinue: "
                       "answer the user in prose, or emit one more "
                       "intent block.\n" % (rc, feedback))

    print("-" * 60)
    if last_result is not None:
        print(last_result)
    sys.exit("harness.py: hit MAX_TURNS=%d without a final answer; "
             "last tool result shown above." % MAX_TURNS)


if __name__ == "__main__":
    main()
