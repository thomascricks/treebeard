"""
Treebeard monthly spend cap — all-tier budget.

COST BASIS (bug #3 fix, 2026-08-16):
    month_spend_usd sums the ledger's STORED cost_usd, which embeds the
    ingestion trust hierarchy (provider_reported actuals > price-table
    estimate) and the rate in force when each call ran. Token-based
    derivation via pricing.py remains ONLY as a fallback for legacy rows
    written before ingestion populated cost_usd (the pre-fix stub left
    1514 of 1710 rows NULL). History: this file originally re-derived
    every row because of that stub; once rates changed (advanced_a 9.0 -> 1.875
    blended) re-derivation mispriced history and discarded
    provider-reported actuals.

BUDGET (operator-set, "for now"):
    $100 per calendar month, UTC. Tiers 1 (local local_model_family) and 2 (free Free_provider)
    cost $0; tier 3 (Cheap_provider) and tier 4 (Frontier_model_family, logged as 'premium') are
    priced below. When month-to-date paid spend >= the cap, the paid tiers are
    over-budget; the router degrades to the free tiers and answers as best it
    can, and a best-effort email warning fires once for the month.

MONTH BUCKETING:
    Uses SQLite strftime('%Y-%m', ts_utc), which converts any timezone offset
    to UTC before formatting. So the bucket is correct even if the stored
    format ever drifts (Z, +HH:MM, or a genuine non-UTC offset) — a lexical
    string compare would mis-bucket those silently. Verified 2026-06-28.

PRICES: live in pricing.py, shared with ingestion. The stale local table
    was removed with the bug #3 fix; budget holds no rates of its own.
"""

from __future__ import annotations
import datetime as _dt
import os
import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

BUDGET_USD = 100.0  # operator-set monthly cap, all tiers. "for now."

TELEMETRY_DB = f"{_TB_BASE}/shared/telemetry/telemetry.db"
# Flag path overridable for tests; same default in production.
_ALERT_FLAG = os.environ.get(
    "TREEBEARD_BUDGET_ALERT_FLAG",
    f"{_TB_BASE}/shared/telemetry/.budget_alert_month")

# Telemetry tier labels that cost money. frontier logs as 'premium' at the
# telemetry write boundary, so match the telemetry label, not 'frontier'.
PAID_TIERS = ("cheap", "premium")

# Router-side tier NAMES the cap gates inside route(). NOTE the asymmetry: the
# frontier/advanced_a/advanced_b rungs' router names are their own labels, but ALL three
# are LOGGED to telemetry as 'premium' (via the router shim). So the route()
# gate matches native tier names here while the ledger cost query matches
# 'premium' (PAID_TIERS above). 'cheap' is the same in both.
# advanced_a and advanced_b added 2026-07-16 under shared cap (ratified: shared budget
# accepts obscured per-provider cost signal until two-axis redesign; a separate
# Advanced_a cap was rejected as >30 lines of budget.py refactor for a holding-position
# wiring).
PAID_TIER_NAMES = ("cheap", "frontier", "advanced_a", "advanced_b")


def _row_cost(model: str, tokens_in, tokens_out) -> float:
    """Derived USD cost of one call. Unknown model -> conservative fallback with
    a stderr note, so a new/unpriced paid model is visible and never free."""
    import pricing
    return pricing.row_cost(model, tokens_in, tokens_out)



def _connect(con):
    """Return (connection, opened_here). If con is None, open the real
    telemetry DB read-only so the cap can never mutate the ledger. Python's
    sqlite3 applies a 5s busy timeout by default, which waits out brief writer
    locks; a hard read failure is handled fail-closed at the call site."""
    if con is not None:
        return con, False
    import sqlite3
    c = sqlite3.connect(f"file:{TELEMETRY_DB}?mode=ro", uri=True)
    return c, True


def month_spend_usd(con=None, now: _dt.datetime | None = None) -> float:
    """Sum STORED cost_usd of paid-tier rows in `now`'s calendar month (UTC);
    rows with NULL cost_usd (legacy, pre-ingestion-fix) fall back to
    token-based derivation via pricing.py.
    Counts all rows that ran; gated/skipped rows log 0 tokens so add ~$0.
    Read-only.

    Float accumulation is acceptable: hundreds of calls/month against a $100
    cap; float64 error is ~1e-13 relative, far below the cap's granularity."""
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)  # treat naive as UTC, never local
    month_key = now.astimezone(_dt.timezone.utc).strftime("%Y-%m")
    con, opened = _connect(con)
    try:
        placeholders = ",".join("?" for _ in PAID_TIERS)
        q = ("SELECT cost_usd, model, tokens_in, tokens_out FROM llm_calls "
             "WHERE strftime('%Y-%m', ts_utc) = ? AND tier IN (" + placeholders + ")")
        total = 0.0
        for cost, model, ti, to in con.execute(q, (month_key, *PAID_TIERS)):
            # Stored cost wins: it embeds the ingestion trust hierarchy
            # (provider_reported > price_table) and the rate in force when
            # the call ran. Derivation is the legacy-NULL fallback only.
            total += cost if cost is not None else _row_cost(model, ti, to)
        return total
    finally:
        if opened:
            con.close()


def over_budget(con=None, now: _dt.datetime | None = None,
                cap: float = BUDGET_USD) -> bool:
    """True once month-to-date paid spend has reached the cap. Read-only."""
    return month_spend_usd(con, now) >= cap


# --- best-effort operator alert ------------------------------------------------

def _alert_tag(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m")


def _already_notified(when: _dt.datetime) -> bool:
    try:
        with open(_ALERT_FLAG) as f:
            return f.read().strip() == _alert_tag(when)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _mark_notified(when: _dt.datetime) -> None:
    try:
        with open(_ALERT_FLAG, "w") as f:
            f.write(_alert_tag(when))
    except Exception as e:
        sys.stderr.write(f"[budget] could not write alert flag (non-fatal): {e}\n")


def notify_capped(spend: float, cap: float = BUDGET_USD,
                  when: _dt.datetime | None = None) -> None:
    """Operator alert that the monthly budget is exhausted. ALWAYS logs to
    stderr (journald-durable). Emails ONCE per calendar month IF an SMTP relay
    is configured via env; no-ops the email otherwise. Never raises — an alert
    must never break the task ('if it can')."""
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    msg = (f"Treebeard monthly budget reached: ${spend:.2f} of ${cap:.2f} as of "
           f"{when.isoformat()}. Paid tiers (Cheap_provider, Frontier_model_family) are now off; "
           f"running on free tiers (local + Free_provider) until the 1st.")
    sys.stderr.write("[budget] " + msg + "\n")
    if _already_notified(when):
        return
    if _try_email(msg):
        _mark_notified(when)


def _try_email(body: str) -> bool:
    """Send via stdlib smtplib IF a relay is configured. Returns True only on a
    successful send. Env: TREEBEARD_SMTP_HOST/PORT/USER/PASS,
    TREEBEARD_ALERT_TO, TREEBEARD_ALERT_FROM."""
    host = os.environ.get("TREEBEARD_SMTP_HOST")
    to = os.environ.get("TREEBEARD_ALERT_TO")
    if not host or not to:
        return False  # no relay -> email stays dark, by design
    try:
        import smtplib
        from email.message import EmailMessage
        port = int(os.environ.get("TREEBEARD_SMTP_PORT", "587"))
        user = os.environ.get("TREEBEARD_SMTP_USER")
        pw = os.environ.get("TREEBEARD_SMTP_PASS")
        sender = os.environ.get("TREEBEARD_ALERT_FROM", user or to)
        m = EmailMessage()
        m["Subject"] = "Treebeard: monthly budget reached"
        m["From"] = sender
        m["To"] = to
        m.set_content(body)
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            if user and pw:
                s.login(user, pw)
            s.send_message(m)
        return True
    except Exception as e:
        sys.stderr.write(f"[budget] email notify failed (non-fatal): {e}\n")
        return False


# --- offline selftest (bug #3, 2026-08-16): first coverage for this file ---
def _selftest() -> int:
    import sqlite3
    fails = 0
    def check(label, cond):
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            fails += 1
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY, ts_utc TEXT, agent TEXT, tier TEXT,
        model TEXT, tokens_in INTEGER, tokens_out INTEGER,
        cost_usd REAL, cost_basis TEXT)""")
    rows = [
        ("2026-08-02T10:00:00+00:00", "t", "premium", "advanced_a_v2", 1000, 100, 0.5, "provider_reported"),
        ("2026-08-03T10:00:00+00:00", "t", "cheap", "cheap_v1", 1000, 100, 0.25, "price_table"),
        ("2026-08-04T10:00:00+00:00", "t", "premium", "frontier_v1", 1_000_000, 0, None, None),
        ("2026-08-05T10:00:00+00:00", "t", "free", "free_v1", 9000, 900, 99.0, "price_table"),
        ("2026-07-30T10:00:00+00:00", "t", "premium", "advanced_a_v2", 1000, 100, 40.0, "provider_reported"),
    ]
    con.executemany("INSERT INTO llm_calls (ts_utc, agent, tier, model, tokens_in, tokens_out, cost_usd, cost_basis) VALUES (?,?,?,?,?,?,?,?)", rows)
    now = _dt.datetime(2026, 8, 15, tzinfo=_dt.timezone.utc)
    spend = month_spend_usd(con, now)
    derived_null_row = _row_cost("frontier_v1", 1_000_000, 0)
    check("B1 stored cost wins (not re-derived)", abs(spend - (0.5 + 0.25 + derived_null_row)) < 1e-9)
    check("B2 NULL cost falls back to derivation (nonzero)", derived_null_row > 0.0)
    check("B3 free tier excluded despite cost_usd populated", spend < 99.0)
    check("B4 other month excluded", spend < 40.0)
    con.execute("UPDATE llm_calls SET cost_usd = 0.9 WHERE cost_usd IS NULL")
    spend2 = month_spend_usd(con, now)
    check("B5 stored zero-adjacent cost respected exactly", abs(spend2 - (0.5 + 0.25 + 0.9)) < 1e-9)
    check("B6 over_budget false below cap", not over_budget(con, now, cap=10.0))
    check("B7 over_budget true at cap", over_budget(con, now, cap=spend2))
    check("B8 naive datetime treated as UTC", abs(month_spend_usd(con, _dt.datetime(2026, 8, 15)) - spend2) < 1e-9)
    print("ALL PASS" if fails == 0 else f"{fails} FAILED")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv[1:]:
        raise SystemExit(_selftest())
