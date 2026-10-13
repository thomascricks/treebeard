#!/usr/bin/env python3
# -*- coding: ascii -*-
"""moon_retention.py -- Moon-owned retention/cleanup of Treebeard
operational artifacts (story 2026-08-19). Phase 1 = AUDIT ONLY.

Actions: KEEP / ARCHIVE / DELETE / SKIP. Nothing is ever silently
deleted: execute() only acts when policy mode == "live" AND the CLI
was invoked with --confirm-live, and every destructive op is logged.
Classification is deterministic, provenance-first (application state
over file age; unknown means unknown -> KEEP), and confined to
whitelisted roots with symlink/traversal protection.
"""
import json
import os
import shutil
import sys
import time
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

POLICY_PATH = os.environ.get(
    "RETENTION_POLICY", f"{_TB_BASE}/shared/config/retention.json")
APPS_PATHS = (
    f"{_TB_BASE}/shared/career_clark/applications.json",
    f"{_TB_BASE}/shared/career/applications.json",
)
ACTIVE_STATES = ("SENT_TO_OPERATOR", "PACKET_READY", "SELECTED")
DRAFT_STATES = ("GENERATION_BLOCKED",)
CLOSED_STATES = ("CLOSED", "REJECTED", "WITHDRAWN", "EXPIRED")
DAY = 86400.0


def load_policy(path=POLICY_PATH):
    pol = json.load(open(path))
    for k in ("report_dir", "archive_root", "roots", "days",
              "protected_prefixes", "mode"):
        if k not in pol:
            raise ValueError("policy missing key: %s" % k)
    return pol


def _norm_name(s):
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def build_app_index(paths=APPS_PATHS):
    """(norm_company, norm_role) -> state, from applications.json
    records that carry company/role fields. Tolerant of anything."""
    idx = {}
    for p in paths:
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        for rec in d.values():
            if not isinstance(rec, dict):
                continue
            c, r, s = (rec.get("company"), rec.get("role"),
                       rec.get("state"))
            if c and r and s:
                idx[(_norm_name(c), _norm_name(r))] = s
    return idx


def _is_protected(path, policy):
    for pref in policy["protected_prefixes"]:
        if path == pref.rstrip("/") or path.startswith(pref):
            return True
    return False


def classify(path, root, root_class, age_days, policy, app_index):
    """Deterministic decision for one file. Returns decision dict."""
    days = policy["days"]
    dec = {"path": path, "root": root, "class": root_class,
           "age_days": round(age_days, 1), "app_state": None,
           "action": "KEEP", "reason": ""}
    if _is_protected(path, policy):
        dec["class"] = "canonical"
        dec["reason"] = "protected prefix"
        return dec
    if root_class == "lockbox":
        rel = os.path.relpath(path, root)
        parts = rel.split(os.sep)
        state = None
        if len(parts) >= 3:
            state = app_index.get((_norm_name(parts[1]),
                                   _norm_name(parts[2])))
        dec["app_state"] = state
        if state in ACTIVE_STATES:
            dec["reason"] = "active application (%s)" % state
        elif state in DRAFT_STATES:
            dec["class"] = "draft"
            if age_days > days["draft_delete"]:
                dec["action"] = "DELETE"
                dec["reason"] = ("draft/blocked older than %dd"
                                 % days["draft_delete"])
            else:
                dec["reason"] = "draft within retention"
        elif state in CLOSED_STATES:
            dec["class"] = "closed"
            if age_days > days["closed_delete"]:
                dec["action"] = "DELETE"
                dec["reason"] = ("closed application older than %dd"
                                 % days["closed_delete"])
            elif age_days > days["closed_archive"]:
                dec["action"] = "ARCHIVE"
                dec["reason"] = ("closed application older than %dd"
                                 % days["closed_archive"])
            else:
                dec["reason"] = "closed within retention"
        else:
            dec["reason"] = "unknown application state -> keep"
        return dec
    if root_class == "digest":
        if age_days > days["digest_delete"]:
            dec["action"] = "DELETE"
            dec["reason"] = ("digest artifact older than %dd"
                             % days["digest_delete"])
        elif age_days > days["digest_archive"]:
            dec["action"] = "ARCHIVE"
            dec["reason"] = ("digest artifact older than %dd"
                             % days["digest_archive"])
        else:
            dec["reason"] = "digest within retention"
        return dec
    dec["action"] = "SKIP"
    dec["reason"] = "unrecognized root class %r" % root_class
    return dec


def audit(policy, app_index=None, now=None):
    """Inventory + classify every file under governed roots. Pure
    read. Returns (decisions, summary)."""
    if app_index is None:
        app_index = build_app_index()
    if now is None:
        now = time.time()
    decisions = []
    summary = {"scanned": 0, "protected": 0, "active": 0,
               "archive_candidate": 0, "delete_candidate": 0,
               "kept_other": 0, "skipped": 0, "errors": 0}
    for entry in policy["roots"]:
        root = entry["path"]
        rclass = entry["class"]
        if not os.path.isdir(root):
            continue
        real_root = os.path.realpath(root)
        for dirpath, dirnames, filenames in os.walk(
                root, followlinks=False):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                summary["scanned"] += 1
                try:
                    real = os.path.realpath(p)
                    if not real.startswith(real_root + os.sep):
                        decisions.append(
                            {"path": p, "root": root, "class": rclass,
                             "age_days": None, "app_state": None,
                             "action": "SKIP",
                             "reason": "resolves outside governed root"})
                        summary["skipped"] += 1
                        continue
                    age = (now - os.lstat(p).st_mtime) / DAY
                    dec = classify(p, root, rclass, age, policy,
                                   app_index)
                except Exception as e:  # noqa: BLE001
                    decisions.append(
                        {"path": p, "root": root, "class": rclass,
                         "age_days": None, "app_state": None,
                         "action": "SKIP",
                         "reason": "classification error: %s"
                                   % str(e)[:80]})
                    summary["errors"] += 1
                    continue
                decisions.append(dec)
                if dec["class"] == "canonical":
                    summary["protected"] += 1
                elif dec["action"] == "ARCHIVE":
                    summary["archive_candidate"] += 1
                elif dec["action"] == "DELETE":
                    summary["delete_candidate"] += 1
                elif dec["action"] == "SKIP":
                    summary["skipped"] += 1
                elif dec["reason"].startswith("active application"):
                    summary["active"] += 1
                else:
                    summary["kept_other"] += 1
    return decisions, summary


def execute(policy, decisions, confirm_live=False, log_lines=None):
    """Phase 2 engine. Acts ONLY when mode=='live' AND confirm_live.
    Per-file try/except: one failure never aborts the run and is
    never reported as success."""
    ops = {"deleted": 0, "archived": 0, "failed": 0, "noop": 0}
    if log_lines is None:
        log_lines = []
    if policy.get("mode") != "live" or not confirm_live:
        ops["noop"] = sum(1 for d in decisions
                          if d["action"] in ("DELETE", "ARCHIVE"))
        log_lines.append(json.dumps(
            {"event": "audit-only; no destructive action",
             "candidates": ops["noop"]}))
        return ops
    arch_root = os.path.realpath(policy["archive_root"])
    for d in decisions:
        if d["action"] not in ("DELETE", "ARCHIVE"):
            continue
        p = d["path"]
        try:
            if _is_protected(p, policy) or not os.path.lexists(p):
                ops["failed"] += 1
                log_lines.append(json.dumps(
                    {"event": "refused", "path": p,
                     "why": "protected or missing"}))
                continue
            real = os.path.realpath(p)
            real_root = os.path.realpath(d["root"])
            if not real.startswith(real_root + os.sep):
                ops["failed"] += 1
                log_lines.append(json.dumps(
                    {"event": "refused", "path": p,
                     "why": "outside governed root"}))
                continue
            if d["action"] == "DELETE":
                os.remove(p)
                ops["deleted"] += 1
                log_lines.append(json.dumps(
                    {"event": "deleted", "path": p,
                     "reason": d["reason"]}))
            else:
                rel = os.path.relpath(p, d["root"])
                dest = os.path.join(arch_root,
                                    os.path.basename(d["root"]), rel)
                dest_real = os.path.realpath(os.path.dirname(dest))
                if not (dest_real == arch_root or
                        dest_real.startswith(arch_root + os.sep)):
                    ops["failed"] += 1
                    log_lines.append(json.dumps(
                        {"event": "refused", "path": p,
                         "why": "archive dest escapes archive root"}))
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(p, dest)
                ops["archived"] += 1
                log_lines.append(json.dumps(
                    {"event": "archived", "path": p, "dest": dest,
                     "reason": d["reason"]}))
        except Exception as e:  # noqa: BLE001
            ops["failed"] += 1
            log_lines.append(json.dumps(
                {"event": "failed", "path": p, "err": str(e)[:120]}))
    return ops


def run_audit(policy_path=POLICY_PATH):
    policy = load_policy(policy_path)
    decisions, summary = audit(policy)
    os.makedirs(policy["report_dir"], exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rpt = os.path.join(policy["report_dir"], "audit-%s.jsonl" % ts)
    with open(rpt, "w", encoding="utf-8") as fh:
        for d in decisions:
            fh.write(json.dumps(d) + "\n")
        fh.write(json.dumps({"summary": summary}) + "\n")
    print(json.dumps({"mode": policy["mode"], "report": rpt,
                      "summary": summary}, indent=2))
    return 0


def _selftest():
    import tempfile
    fails = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    base = tempfile.mkdtemp(prefix="retn-test-")
    lb = os.path.join(base, "lockbox")
    dg = os.path.join(base, "reports")
    outside = os.path.join(base, "outside")
    canon = os.path.join(base, "operator") + os.sep
    arch = os.path.join(base, "archive")
    for d in (lb, dg, outside, canon):
        os.makedirs(d, exist_ok=True)

    def mk(path, age_days):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("x")
        old = time.time() - age_days * DAY
        os.utime(path, (old, old))
        return path

    pol = {"mode": "audit", "report_dir": base, "archive_root": arch,
           "protected_prefixes": [canon],
           "roots": [{"path": lb, "class": "lockbox"},
                     {"path": dg, "class": "digest"}],
           "days": {"draft_delete": 14, "digest_archive": 45,
                    "digest_delete": 120, "closed_archive": 30,
                    "closed_delete": 180, "active_grace": 90}}
    idx = {(_norm_name("Acme"), _norm_name("PM")): "SENT_TO_OPERATOR",
           (_norm_name("Acme"), _norm_name("Old")): "GENERATION_BLOCKED",
           (_norm_name("Acme"), _norm_name("New")): "GENERATION_BLOCKED",
           (_norm_name("Acme"), _norm_name("Gone")): "REJECTED"}

    d = classify(mk(os.path.join(canon, "resume.txt"), 5000), canon,
                 "lockbox", 5000, pol, idx)
    check("r01 canonical survives any age",
          d["action"] == "KEEP" and d["class"] == "canonical")

    p_active = mk(os.path.join(lb, "d", "Acme", "PM", "resume.pdf"), 400)
    decs, s = audit(pol, idx)
    da = [x for x in decs if x["path"] == p_active][0]
    check("r02 active application survives past TTL",
          da["action"] == "KEEP" and "active" in da["reason"])

    p_old = mk(os.path.join(lb, "d", "Acme", "Old", "r.pdf"), 30)
    p_new = mk(os.path.join(lb, "d", "Acme", "New", "r.pdf"), 2)
    decs, s = audit(pol, idx)
    do = [x for x in decs if x["path"] == p_old][0]
    dn = [x for x in decs if x["path"] == p_new][0]
    check("r03 old abandoned draft -> delete candidate",
          do["action"] == "DELETE")
    check("r04 recent draft remains", dn["action"] == "KEEP")

    p_closed = mk(os.path.join(lb, "d", "Acme", "Gone", "r.pdf"), 60)
    decs, s = audit(pol, idx)
    dc = [x for x in decs if x["path"] == p_closed][0]
    check("r05 closed application follows closed policy",
          dc["action"] == "ARCHIVE" and dc["class"] == "closed")

    p_dig = mk(os.path.join(dg, "old-report.log"), 200)
    decs, s = audit(pol, idx)
    dd = [x for x in decs if x["path"] == p_dig][0]
    check("r06 digest artifact follows digest policy",
          dd["action"] == "DELETE")

    p_unk = mk(os.path.join(lb, "d", "Nobody", "Mystery", "r.pdf"), 900)
    decs, s = audit(pol, idx)
    du = [x for x in decs if x["path"] == p_unk][0]
    check("r07 unknown state fails safe",
          du["action"] == "KEEP" and "unknown" in du["reason"])

    before = sorted(os.path.join(r, f) for rt in (lb, dg)
                    for r, _, fs in os.walk(rt) for f in fs)
    decs, s = audit(pol, idx)
    ops = execute(pol, decs, confirm_live=True)  # mode still audit
    after = sorted(os.path.join(r, f) for rt in (lb, dg)
                   for r, _, fs in os.walk(rt) for f in fs)
    check("r08 audit mode: zero destructive operations",
          before == after and ops["deleted"] == 0
          and ops["archived"] == 0 and ops["noop"] >= 3)

    pol_live = dict(pol, mode="live")
    decs, s = audit(pol_live, idx)
    logl = []
    ops = execute(pol_live, decs, confirm_live=True, log_lines=logl)
    check("r09 live mode deletes eligible artifact",
          not os.path.exists(p_old) and not os.path.exists(p_dig)
          and ops["deleted"] == 2)
    moved = [l for l in logl if '"archived"' in l]
    check("r10 archive moves only into approved archive root",
          not os.path.exists(p_closed) and len(moved) == 1
          and json.loads(moved[0])["dest"].startswith(arch))

    bad = {"path": os.path.join(outside, "x.txt"), "root": lb,
           "class": "lockbox", "action": "DELETE", "reason": "t"}
    mk(bad["path"], 1)
    ops = execute(pol_live, [bad], confirm_live=True)
    check("r11 path outside governed root refused",
          os.path.exists(bad["path"]) and ops["failed"] == 1)

    victim = mk(os.path.join(outside, "victim.txt"), 999)
    link = os.path.join(dg, "sneaky.log")
    os.symlink(victim, link)
    old = time.time() - 999 * DAY
    os.utime(link, (old, old), follow_symlinks=False)
    decs, s = audit(pol_live, idx)
    dl = [x for x in decs if x["path"] == link]
    ops = execute(pol_live, decs, confirm_live=True)
    check("r12 symlink cannot escape governed root",
          os.path.exists(victim) and dl
          and dl[0]["action"] == "SKIP")

    decs, s = audit(pol, idx)
    counted = (s["protected"] + s["active"] + s["archive_candidate"]
               + s["delete_candidate"] + s["kept_other"] + s["skipped"]
               + s["errors"])
    check("r13 telemetry counts match dispositions",
          counted == s["scanned"] == len(decs))

    decs2, s2 = audit(pol_live, idx)
    ops2 = execute(pol_live, decs2, confirm_live=True)
    check("r14 second run idempotent",
          ops2["deleted"] == 0 and ops2["archived"] == 0
          and ops2["failed"] == 0)

    shutil.rmtree(base)
    print("%d/14 retention selftests passed" % (14 - len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        _selftest()
    if "--audit" in sys.argv[1:]:
        sys.exit(run_audit())
    sys.exit("usage: moon_retention.py --selftest | --audit")
