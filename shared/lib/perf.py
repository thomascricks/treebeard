"""perf.py - derived per-model performance from observed telemetry.

signals.py records observations and deliberately computes no score: "quality
is derived elsewhere, later, and only where enough non-synthetic evidence
exists." This module is that derivation. It reads ONLY stores that carry a
synthetic flag (routing_decisions, signals) so test traffic can never
masquerade as production evidence -- llm_calls carries no such flag and is
deliberately not consulted.

THE PRIOR IS NOT EVIDENCE. Every model starts at the same neutral prior, so
with zero observations the perf term differentiates nothing: it cannot rank
models apart, it can only shrink real evidence toward neutral while samples
are small. Status makes the evidence state explicit per model
(no-evidence / thin / measured) instead of hiding it inside a number.

COMPLETION IS RELIABILITY, NOT QUALITY (signals.py doctrine: status=='ok'
means HTTP 200 and is REFUSED as a quality proxy). A plain successful call
therefore holds a model exactly AT the prior -- it is evidence the model
works, not evidence it is better. Errors and negative-polarity signals sink
perf BELOW the prior; only positive-polarity signals (validator_pass today,
operator/QA judgements when they exist) lift it ABOVE. Superiority must be
earned through quality evidence, never through traffic volume: a model that
received all the traffic and merely completed it scores the same prior as a
model that received none, so concentration cannot launder itself into
"measured" dominance.

Formula, per model, over a rolling window of NON-SYNTHETIC decisions:

    n        = routed decisions whose final_model is this model
    ok       = those with outcome_status == 'ok'
    neg/pos  = negative/positive polarity signals for this model, linked to a
               persisted non-synthetic decision
    credit   = clamp(prior*ok - neg_weight*neg + pos_weight*pos, 0, n)
    perf     = (prior*prior_n + credit) / (prior_n + n)

Properties: all-ok with no signals == prior exactly; errors sink; negative
signals sink; positive signals lift; shrinkage keeps small samples near the
prior. The rolling window keeps stale eras (e.g. the pre-2026-08-13
credential-failure error clusters) from haunting current routing.

Constants live in /etc/treebeard/routing.conf [perf]; absent section or any
read failure degrades to the shared neutral prior for every model with status
'derivation-unavailable' -- a broken perf source must never fail or skew a
routing decision.
"""
import configparser
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

DB_PATH = f"{_TB_BASE}/shared/telemetry/telemetry.db"
CONF = f"{_TB_ETC}/routing.conf"

DEFAULTS = {
    "window_days": 14.0,
    "prior": 0.75,
    "prior_n": 5.0,
    "neg_weight": 0.5,
    "pos_weight": 0.25,
    "measured_n": 10.0,
}


def load_config(path=CONF):
    """[perf] section with defaults; malformed values fall back per-key."""
    cfg = dict(DEFAULTS)
    try:
        cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
        if cp.read(path) and cp.has_section("perf"):
            for k in DEFAULTS:
                if cp.has_option("perf", k):
                    try:
                        cfg[k] = float(cp.get("perf", k))
                    except ValueError:
                        sys.stderr.write(f"[perf] bad {k} in [perf]; "
                                         f"default {DEFAULTS[k]} kept\n")
    except Exception as e:
        sys.stderr.write(f"[perf] config unreadable ({e}); defaults kept\n")
    return cfg


def _neutral(models, cfg, status):
    return {m: {"perf": round(cfg["prior"], 4), "n": 0, "ok": 0,
                "neg": 0, "pos": 0, "status": status} for m in models}


def derive(models, db_path=DB_PATH, cfg=None, now=None):
    """{model: {perf, n, ok, neg, pos, status}} for each model string.

    status: 'no-evidence' (n==0), 'thin' (0<n<measured_n), 'measured'
    (n>=measured_n), or 'derivation-unavailable' (store unreadable; perf is
    the shared prior for every model and MUST NOT be treated as evidence).
    """
    cfg = cfg or load_config()
    models = list(models)
    if not models:
        return {}
    try:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=cfg["window_days"])).isoformat(timespec="seconds")
        out = {}
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            for m in models:
                # F1 (2026-09-27): a decision is an attempt for m when m served it
                # (final_model=m) OR m was selected but did not serve it (escalated
                # away, or every tier failed). The router finalizes under the
                # serving model, so without the second branch a model that always
                # fails over keeps n=0 and the neutral prior. ok stays keyed on
                # final_model: only the serving model earns it. Unfinalized
                # decisions (outcome_status NULL) count for nobody, matching the
                # served branch, which only matches once finalization runs.
                n, ok = con.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(final_model=? AND outcome_status='ok'),0) "
                    "FROM routing_decisions WHERE (final_model=? "
                    "OR (selected_model=? AND outcome_status IS NOT NULL "
                    "AND (final_model IS NULL OR final_model!=?))) "
                    "AND COALESCE(synthetic,0)=0 AND ts_utc>=?",
                    (m, m, m, m, since)).fetchone()
                # WHY the decision link: production is stamped synthetic=NULL
                # (router._synthetic_flag default), so the flag alone cannot
                # separate test from real evidence. router._selftest (run daily
                # by Will's regression suite) stubs decision persistence but still
                # emits signals with fresh decision_ids; those scored as production
                # (cheap_provider 0.23 vs 0.71). A persisted decision is the only proof a
                # signal belongs to a real turn, so unlinked evidence cannot move
                # routing (fails closed if a decision ever fails to persist). NULL
                # decisions remain production under the Router 1 convention.
                neg, pos = con.execute(
                    "SELECT COALESCE(SUM(s.polarity<0),0), COALESCE(SUM(s.polarity>0),0) "
                    "FROM signals s "
                    "WHERE s.model=? AND COALESCE(s.synthetic,0)=0 AND s.ts_utc>=? "
                    "AND EXISTS (SELECT 1 FROM routing_decisions d "
                    "WHERE d.decision_id = s.decision_id "
                    "AND COALESCE(d.synthetic,0)=0)", (m, since)).fetchone()
                credit = (cfg["prior"] * ok - cfg["neg_weight"] * neg
                          + cfg["pos_weight"] * pos)
                credit = max(0.0, min(float(n), credit))
                perf = (cfg["prior"] * cfg["prior_n"] + credit) / (cfg["prior_n"] + n)
                status = ("no-evidence" if n == 0 else
                          "thin" if n < cfg["measured_n"] else "measured")
                out[m] = {"perf": round(perf, 4), "n": int(n), "ok": int(ok),
                          "neg": int(neg), "pos": int(pos), "status": status}
        finally:
            con.close()
        return out
    except Exception as e:
        sys.stderr.write(f"[perf] derivation unavailable ({e}); "
                         f"neutral prior for all models\n")
        return _neutral(models, cfg, "derivation-unavailable")


# ---------------------------------------------------------------------------
def _selftest():
    import os, tempfile
    fails = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    cfg = dict(DEFAULTS)
    _RD5 = ("INSERT INTO routing_decisions (ts_utc, final_model,"
            " outcome_status, synthetic, decision_id) VALUES (?,?,?,?,?)")
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE routing_decisions (ts_utc TEXT, final_model TEXT,"
                    " outcome_status TEXT, synthetic INTEGER, decision_id TEXT,"
                    " selected_model TEXT)")
        con.execute("CREATE TABLE signals (ts_utc TEXT, model TEXT,"
                    " polarity INTEGER, synthetic INTEGER, decision_id TEXT)")
        seq = {}

        def dec(rows):
            # append a decision_id per row: "<model>#<k>", k counting per model
            out = []
            for r in rows:
                k = seq[r[1]] = seq.get(r[1], -1) + 1
                out.append(r + (f"{r[1]}#{k}",))
            return out
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        recent = now.isoformat(timespec="seconds")
        old = (now - timedelta(days=30)).isoformat(timespec="seconds")
        rows = []
        rows += [(recent, "m_good", "ok", None)] * 20
        rows += [(recent, "m_bad", "error", None)] * 15 + [(recent, "m_bad", "ok", None)] * 5
        rows += [(recent, "m_synth", "ok", 1)] * 50
        rows += [(old, "m_stale", "error", None)] * 40
        rows += [(recent, "m_flagged", "ok", None)] * 20
        rows += [(recent, "m_vol", "ok", None)] * 200
        con.executemany(_RD5, dec(rows))
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "m_flagged", -1, None, "m_flagged#0")] * 16
                        + [(recent, "m_good", +1, None, "m_good#0")] * 4
                        + [(recent, "m_synth", -1, 1, None)] * 30)
        con.commit(); con.close()

        r = derive(["m_good", "m_bad", "m_unknown", "m_synth", "m_stale",
                    "m_flagged"], db_path=db, cfg=cfg, now=now)

        check("unknown_gets_prior", r["m_unknown"]["perf"] == cfg["prior"]
              and r["m_unknown"]["status"] == "no-evidence")
        check("synthetic_excluded", r["m_synth"]["n"] == 0
              and r["m_synth"]["perf"] == cfg["prior"])
        check("window_excludes_old", r["m_stale"]["n"] == 0)
        check("good_above_prior", r["m_good"]["perf"] > cfg["prior"])
        check("bad_below_prior", r["m_bad"]["perf"] < cfg["prior"])
        check("neg_signals_sink", r["m_flagged"]["perf"] < r["m_good"]["perf"])
        check("good_measured", r["m_good"]["status"] == "measured")
        check("prior_identical_when_no_evidence",
              r["m_unknown"]["perf"] == r["m_synth"]["perf"])
        check("bounds", all(0.0 < v["perf"] <= 1.0 for v in r.values()))
        # shrinkage: 5 oks must move perf less than 20 oks
        con = sqlite3.connect(db)
        con.execute(_RD5,
                    dec([(recent, "m_five", "ok", None)])[0])
        con.executemany(_RD5,
                        dec([(recent, "m_five", "ok", None)] * 4))
        con.commit(); con.close()
        r2 = derive(["m_five", "m_good"], db_path=db, cfg=cfg, now=now)
        check("plain_ok_holds_prior",
              abs(r2["m_five"]["perf"] - cfg["prior"]) < 1e-9
              and r2["m_five"]["status"] == "thin")
        check("volume_cannot_launder",
              abs(derive(["m_vol"], db_path=db, cfg=cfg, now=now)["m_vol"]["perf"]
                  - cfg["prior"]) < 1e-9)
        # shrinkage: same neg-signal ratio, small sample sits closer to prior
        con = sqlite3.connect(db)
        con.executemany(_RD5,
                        dec([(recent, "m_small", "ok", None)] * 2
                            + [(recent, "m_large", "ok", None)] * 40))
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "m_small", -1, None, "m_small#0")] * 1
                        + [(recent, "m_large", -1, None, "m_large#0")] * 20)
        con.commit(); con.close()
        r4 = derive(["m_small", "m_large"], db_path=db, cfg=cfg, now=now)
        check("shrinkage_orders_confidence",
              r4["m_large"]["perf"] < r4["m_small"]["perf"] < cfg["prior"])

        # Evidence boundary: a signal counts only when linked to a persisted
        # non-synthetic decision (router._selftest emits orphan signals).
        import uuid

        def expect(n, ok, neg, pos):
            credit = max(0.0, min(float(n), cfg["prior"] * ok
                                  - cfg["neg_weight"] * neg + cfg["pos_weight"] * pos))
            return round((cfg["prior"] * cfg["prior_n"] + credit)
                         / (cfg["prior_n"] + n), 4)

        con = sqlite3.connect(db)
        for m in ("m_s1", "m_s2", "m_s3b", "m_s3c", "m_s3d"):
            con.executemany(_RD5,
                            dec([(recent, m, "ok", None)] * 10))
        con.execute(_RD5,
                    (recent, "m_s3c", "ok", 1, "m_s3c#synth"))
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "m_s1", -1, None, "m_s1#0")] * 2
                        + [(recent, "m_s2", -1, 1, "m_s2#0")] * 3
                        + [(recent, "m_s3b", -1, None, str(uuid.uuid4()))] * 3
                        + [(recent, "m_s3c", -1, None, "m_s3c#synth")] * 3
                        + [(recent, "m_s3d", -1, None, None)] * 3)
        con.commit(); con.close()
        r5 = derive(["m_s1", "m_s2", "m_s3b", "m_s3c", "m_s3d"],
                    db_path=db, cfg=cfg, now=now)
        check("S1 linked NULL-synthetic negative lowers perf to formula value",
              r5["m_s1"]["neg"] == 2
              and abs(r5["m_s1"]["perf"] - expect(10, 10, 2, 0)) < 1e-9
              and r5["m_s1"]["perf"] < cfg["prior"])
        check("S2 synthetic=1 signal linked to a real decision has no effect",
              r5["m_s2"]["neg"] == 0 and r5["m_s2"]["perf"] == cfg["prior"])
        check("S3a NULL-synthetic decisions count as production (n, measured)",
              r5["m_s1"]["n"] == 10 and r5["m_s1"]["status"] == "measured")
        check("S3b NULL-synthetic signal with no persisted decision is excluded",
              r5["m_s3b"]["neg"] == 0 and r5["m_s3b"]["perf"] == cfg["prior"])
        check("S3c NULL-synthetic signal linked to a synthetic=1 decision is excluded",
              r5["m_s3c"]["neg"] == 0 and r5["m_s3c"]["n"] == 10
              and r5["m_s3c"]["perf"] == cfg["prior"])
        check("S3d signal with decision_id NULL is excluded",
              r5["m_s3d"]["neg"] == 0 and r5["m_s3d"]["perf"] == cfg["prior"])

        # S4: a router._selftest-shaped contamination batch changes nothing.
        snap_models = ["m_good", "m_bad", "m_flagged", "m_five", "m_vol",
                       "m_small", "m_large", "m_s1", "m_unknown"]
        before = derive(snap_models, db_path=db, cfg=cfg, now=now)
        con = sqlite3.connect(db)
        batch_models = ["m_good", "m_bad", "m_flagged", "m_five", "m_unknown"]
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, batch_models[i % 5], (-1, 0, 1)[i % 3], None,
                          str(uuid.uuid4())) for i in range(26)])
        con.executemany(_RD5,
                        [(recent, m, st, 1, f"{m}#synth")
                         for m, st in (("m_good", "error"), ("m_vol", "ok"),
                                       ("m_unknown", "error"))])
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "m_good", -1, 1, "m_good#0"),
                         (recent, "m_vol", +1, 1, "m_vol#synth"),
                         (recent, "m_unknown", -1, 1, "m_unknown#synth")])
        con.commit(); con.close()
        check("S4 selftest-shaped contamination leaves derive() identical",
              derive(snap_models, db_path=db, cfg=cfg, now=now) == before)

        # F1 (2026-09-27): selected-but-not-served decisions are failed
        # attempts for the selected model (n+1, ok+0); the serving model's
        # accounting is unchanged; unfinalized decisions count for nobody.
        fdb = os.path.join(td, "f1.db")
        con = sqlite3.connect(fdb)
        con.execute("CREATE TABLE routing_decisions (ts_utc TEXT, final_model TEXT,"
                    " outcome_status TEXT, synthetic INTEGER, decision_id TEXT,"
                    " selected_model TEXT)")
        con.execute("CREATE TABLE signals (ts_utc TEXT, model TEXT,"
                    " polarity INTEGER, synthetic INTEGER, decision_id TEXT)")
        R6 = ("INSERT INTO routing_decisions (ts_utc, selected_model, final_model,"
              " outcome_status, synthetic, decision_id) VALUES (?,?,?,?,?,?)")
        f1 = []
        f1 += [(recent, "f_fail", "f_srv", "ok", None, f"esc#{i}") for i in range(6)]
        f1 += [(recent, "f_srv", "f_srv", "ok", None, f"own#{i}") for i in range(4)]
        f1 += [(recent, "f_dead", None, "failed", None, f"dead#{i}") for i in range(3)]
        f1 += [(recent, "f_fly", None, None, None, f"fly#{i}") for i in range(2)]
        f1 += [(recent, "f_syn", "f_srv2", "ok", 1, f"syn#{i}") for i in range(3)]
        f1 += [(old, "f_old", "f_srv2", "ok", None, f"old#{i}") for i in range(3)]
        f1 += [(recent, "f_self", "f_self", "ok", None, f"self#{i}") for i in range(5)]
        con.executemany(R6, f1)
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "f_fail", -1, None, f"esc#{i}") for i in range(6)])
        con.commit(); con.close()
        rf = derive(["f_fail", "f_srv", "f_srv2", "f_dead", "f_fly", "f_syn",
                     "f_old", "f_self"], db_path=fdb, cfg=cfg, now=now)
        check("F1a selected-but-not-served adds 1 to n and 0 to ok",
              rf["f_fail"]["n"] == 6 and rf["f_fail"]["ok"] == 0
              and rf["f_fail"]["neg"] == 6
              and abs(rf["f_fail"]["perf"] - expect(6, 0, 6, 0)) < 1e-9
              and rf["f_fail"]["perf"] < cfg["prior"]
              and rf["f_fail"]["status"] != "no-evidence")
        check("F1b serving model keeps its existing accounting",
              rf["f_srv"]["n"] == 10 and rf["f_srv"]["ok"] == 10)
        check("F1c all-tiers-failed counts against selected_model",
              rf["f_dead"]["n"] == 3 and rf["f_dead"]["ok"] == 0)
        check("F1d unfinalized decision counts for nobody",
              rf["f_fly"]["n"] == 0 and rf["f_fly"]["status"] == "no-evidence")
        check("F1e synthetic and window filters still exclude",
              rf["f_syn"]["n"] == 0 and rf["f_old"]["n"] == 0
              and rf["f_srv2"]["n"] == 0)
        check("F1f self-served decision is counted once",
              rf["f_self"]["n"] == 5 and rf["f_self"]["ok"] == 5)

        # S5: production pattern pin (cheap_v1, 2026-09-26 window).
        con = sqlite3.connect(db)
        con.executemany(_RD5,
                        dec([(recent, "m_prod", "ok", None)] * 58))
        con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                        [(recent, "m_prod", -1, None, f"m_prod#{k}") for k in range(5)]
                        + [(recent, "m_prod", -1, None, str(uuid.uuid4()))
                           for _ in range(60)])
        con.commit(); con.close()
        rp = derive(["m_prod"], db_path=db, cfg=cfg, now=now)["m_prod"]
        want = ((cfg["prior"] * cfg["prior_n"]
                 + (cfg["prior"] * 58 - cfg["neg_weight"] * 5))
                / (cfg["prior_n"] + 58))
        check("S5 58 ok + 5 linked + 60 orphan negatives -> linked-only perf",
              rp["n"] == 58 and rp["neg"] == 5
              and abs(rp["perf"] - round(want, 4)) < 1e-9)

        # S6: the fixture table has no PRIMARY KEY, so duplicates are possible
        # here; EXISTS must count a linked signal once regardless.
        con = sqlite3.connect(db)
        con.executemany(_RD5,
                        [(recent, "m_dup", "ok", None, "m_dup#0")] * 2)
        con.execute("INSERT INTO signals VALUES (?,?,?,?,?)",
                    (recent, "m_dup", -1, None, "m_dup#0"))
        con.commit(); con.close()
        rd = derive(["m_dup"], db_path=db, cfg=cfg, now=now)["m_dup"]
        check("S6 duplicate decision rows do not double-count a linked signal",
              rd["neg"] == 1 and rd["perf"] == expect(rd["n"], rd["ok"], 1, 0))
        # fail-soft on a missing store
        r3 = derive(["x"], db_path=os.path.join(td, "absent", "no.db"), cfg=cfg)
        check("failsoft_neutral", r3["x"]["status"] == "derivation-unavailable"
              and r3["x"]["perf"] == cfg["prior"])

    print(f"{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
