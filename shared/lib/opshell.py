#!/usr/bin/env python3
# opshell.py -- UI-01 Shared Application Shell: attention computation +
# persistent header/nav/badge markup. The badge count and the Home attention
# queue must come from THIS one computation (UI-16 groundwork); no page
# derives its own count. Attention set = BOARD CARDS whose ui-state is in
# uistate.ATTENTION (PO ruling 2026-08-31: cards only, never standalone
# Meeseeks objects). Board is read via board.py as a library (reflector
# precedent), never by parsing board.json directly here.
import html
import os as _tb_os
import sys
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_LIB = f"{_TB_BASE}/shared/lib"
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import board
import uistate
_MEESEEKS_DIR = f"{_TB_BASE}/shared/meeseeks"


def _ledger():
    """Read-only handle on the Meeseeks ledger. immutable=1 makes writes
    physically impossible: the console reads execution truth, it never
    participates in it. Row-shape logic stays Meeseeks-owned (get_section),
    so this is not a second query layer. Returns (con, module) or (None,None)
    -- a ledger we cannot read degrades detail, never the badge."""
    import sqlite3
    try:
        if _MEESEEKS_DIR not in sys.path:
            sys.path.insert(0, _MEESEEKS_DIR)
        import meeseeks
        con = sqlite3.connect("file:%s?immutable=1" % meeseeks.db_path(),
                              uri=True)
        return con, meeseeks
    except Exception:
        return None, None


def _detail_from_rows(srows, prow):
    """Execution facts from one project's section rows + project row.
    ONE section rule (uistate.worst_section): the worst section represents
    the project on every surface -- never the first (old box.card) or the
    last (old failed_ids)."""
    d = {"state": None, "stage": None, "since": None, "reason": None,
         "retries": None, "sid": None, "started": None,
         "project_state": None, "sections": srows}
    if prow:
        d["started"] = prow.get("created")
        d["project_state"] = prow.get("state")
    w = uistate.worst_section(srows)
    if w:
        d.update(sid=w.get("sid"), state=w.get("state"),
                 stage=w.get("stage"), since=w.get("updated"),
                 reason=w.get("blocked_reason") or None,
                 retries=w.get("misfires"))
    return d


def _read_detail(con, M, pid):
    cols0 = [c[0] for c in con.execute(
        "select * from sections limit 0").description]
    srows = [dict(zip(cols0, r)) for r in con.execute(
        "select * from sections where pid=? order by sid", (pid,))]
    prow = M.get_project(con, pid)
    p = None
    if prow:
        pcols = [c[0] for c in con.execute(
            "select * from projects limit 0").description]
        p = dict(zip(pcols, prow))
    return _detail_from_rows(srows, p)


def _empty_detail():
    return {"state": None, "stage": None, "since": None, "reason": None,
            "retries": None, "sid": None, "started": None,
            "project_state": None, "sections": []}


def _pid(card):
    """The meeseeks project behind a card's exec_ref (or legacy pid)."""
    ref = board.card_exec_ref(card) if hasattr(board, "card_exec_ref") \
        else ({"kind": "meeseeks", "id": card.get("meeseeks_pid")}
              if card.get("meeseeks_pid") else None)
    if ref and ref.get("kind") == "meeseeks":
        return ref.get("id")
    return None


def card_detail(card):
    """Execution facts for one card, every field None when not known.
    Unlinked cards (no meeseeks exec_ref) legitimately have no wait time or
    reason -- absent, never substituted with card age (UI-17)."""
    pid = _pid(card)
    if not pid:
        return _empty_detail()
    return details_for([card]).get(card.get("id"), _empty_detail())


def details_for(items):
    """{card id: detail} for every LINKED card in items, over ONE read-only
    ledger connection. Unlinked cards are absent. A ledger we cannot read
    yields {} -- degrade detail, never invent failure."""
    linked = [(c.get("id"), _pid(c)) for c in items if _pid(c)]
    if not linked:
        return {}
    con, M = _ledger()
    if con is None:
        return {}
    out = {}
    try:
        for cid, pid in linked:
            try:
                out[cid] = _read_detail(con, M, pid)
            except Exception:
                continue
    finally:
        con.close()
    return out


def resolve_items(items, details=None):
    """[(card, key)] through THE resolver (uistate.resolve). Execution facts
    are fetched once, in a batch, for linked cards only, unless supplied."""
    if details is None:
        details = details_for(items)
    return [(c, uistate.resolve(c, details.get(c.get("id"))))
            for c in items]


def _ago(iso, now=None):
    """Human elapsed time, or None when the input is missing/unparseable.
    Never returns '0m' for absent data (UI-17)."""
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    sec = int((now - t).total_seconds())
    if sec < 0:
        return None
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm %ds" % (sec // 60, sec % 60)
    if sec < 86400:
        return "%dh %dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd %dh" % (sec // 86400, (sec % 86400) // 3600)


_REASONS = {"approval": "Waiting for your approval",
            "review": "Waiting for your review",
            "returned": "Returned - needs your decision",
            "blocked": "Blocked - cannot continue"}


def working_cards(items=None, details=None):
    """Board cards actively working, through THE resolver. Facts are read
    only for open in_progress cards that are linked (an UNREVIEWABLE or
    exhausted project is Failed, not Working)."""
    if items is None:
        items = board.load_board()
    cand = [c for c in items if c.get("status") == "open"
            and c.get("phase") == "in_progress"]
    return [(c, k) for c, k in resolve_items(cand, details)
            if k == "working"]


def render_working(items=None, details=None, now=None):
    """UI-04 Currently Working. Shows stage, last activity, retries when they
    happened, and total time on the card. Deliberately does NOT show
    elapsed-in-stage: no backend field records stage entry, and deriving one
    from activity events would be inference dressed as telemetry. No percent
    complete, no estimated completion -- neither exists."""
    work = working_cards(items, details)
    if not work:
        return ('<section id="working"><h2>Currently Working</h2>'
                '<p class="empty">Treebeard is not working on anything right '
                'now.</p></section>')
    out = ['<section id="working"><h2>Currently Working '
           '<span class="count">%d</span></h2><ul class="work-list">'
           % len(work)]
    for card, key in work:
        d = (details or {}).get(card["id"]) if details is not None \
            else card_detail(card)
        d = d or {}
        raw = (card.get("text") or "").strip().split("\n")[0]
        title = html.escape(raw if len(raw) <= 120
                            else raw[:119].rstrip() + "\u2026") \
            or "<em>untitled card</em>"
        bits = ['<li class="work-card">',
                '<a class="att-id" href="/task/%s">card %s</a>'
                % (card["id"], card["id"]),
                '<span class="att-title">%s</span>' % title,
                uistate.badge(key)]
        stage = d.get("state")
        bits.append('<span class="work-stage">%s</span>'
                    % html.escape(str(stage) if stage else "stage unknown"))
        second = []
        act = _ago(d.get("since"), now=now)
        if act:
            second.append("last activity %s ago" % act)
        tot = _ago(d.get("started"), now=now)
        if tot:
            second.append("on this card %s" % tot)
        retries = d.get("retries")
        if retries:
            second.append("%d %s" % (retries,
                                     "retry" if retries == 1 else "retries"))
        if second:
            bits.append('<span class="work-meta">%s</span>'
                        % html.escape(" \u00b7 ".join(second)))
        bits.append("</li>")
        out.append("".join(bits))
    out.append("</ul></section>")
    return "".join(out)


def finished_cards(items=None, limit=8):
    """Board cards that are genuinely closed (status done OR closed), newest
    first. A card in an attention or working phase is by definition not
    finished."""
    if items is None:
        items = board.load_board()
    done = [c for c in items if c.get("status") in ("done", "closed")]
    done.sort(key=lambda c: c.get("closed") or "", reverse=True)
    return done[:limit]


def render_finished(items=None, details=None, now=None, limit=8):
    """UI-05 Recently Finished. Visually subordinate to attention and work.
    The state is THE resolver's: a failed card is Failed, a cancelled /
    superseded / deferred one is Closed, and a done card whose execution
    record disagrees (terminal failure, unfinished section) is Unknown --
    never a reassuring Done. No duration, no evidence claim, no invented
    completion timestamp."""
    fin = finished_cards(items, limit=limit)
    if not fin:
        return ('<section id="finished"><h2>Recently Finished</h2>'
                '<p class="empty">Nothing has finished yet.</p></section>')
    if details is None:
        details = details_for(fin)
    out = ['<section id="finished"><h2>Recently Finished</h2>'
           '<ul class="fin-list">']
    for card, key in resolve_items(fin, details):
        raw = (card.get("text") or "").strip().split("\n")[0]
        title = html.escape(raw if len(raw) <= 90
                            else raw[:89].rstrip() + "…") \
            or "<em>untitled card</em>"
        bits = ['<li class="fin-card">',
                '<a class="fin-id" href="/task/%s">card %s</a>'
                % (card["id"], card["id"]),
                '<span class="fin-title">%s</span>' % title,
                uistate.badge(key)]
        if card.get("status") == "closed" and card.get("outcome"):
            bits.append('<span class="fin-outcome">%s</span>'
                        % html.escape(str(card.get("outcome"))))
        when = card.get("closed")
        ago = _ago(when, now=now)
        if ago:
            bits.append('<span class="fin-when" title="%s">%s ago</span>'
                        % (html.escape(str(when)), ago))
        bits.append("</li>")
        out.append("".join(bits))
    out.append("</ul></section>")
    return "".join(out)


_TIMERS = ("treebeard-reflector", "treebeard-will",
            "treebeard-clark-intake")


def system_status():
    """Health facts that ACTUALLY exist. Every value is None when its
    source cannot be read: unknown is reported as unknown, never inferred
    to be healthy (UI-06, UI-17). No composite 'system OK' verdict is
    computed -- that would be an opinion, not a measurement."""
    st = {"timers": {}, "spend": None, "projects": None, "last_activity": None}
    import subprocess
    for unit in _TIMERS:
        try:
            p = subprocess.run(["systemctl", "is-active", unit + ".timer"],
                               capture_output=True, text=True, timeout=5)
            st["timers"][unit] = p.stdout.strip() or None
        except Exception:
            st["timers"][unit] = None
    try:
        import budget
        st["spend"] = budget.month_spend_usd()
    except Exception:
        st["spend"] = None
    con, M = _ledger()
    ledger_ts = None
    if con is not None:
        try:
            st["projects"] = dict(con.execute(
                "select state, count(*) from projects group by state"))
        except Exception:
            st["projects"] = None
        try:
            row = con.execute("select max(ts) from events").fetchone()
            ledger_ts = row[0] if row else None
        except Exception:
            ledger_ts = None
        finally:
            con.close()
    newest = None
    sdir = f"{_TB_BASE}/shared/status"
    try:
        for f in _tb_os.listdir(sdir):
            m = _tb_os.stat(_tb_os.path.join(sdir, f)).st_mtime
            newest = m if newest is None or m > newest else newest
    except OSError:
        newest = None
    # Liveness is the NEWEST of two real sources: the execution ledger's
    # most recent event, and the newest agent/status file. Status files
    # alone understate liveness -- reflector ticks and chat turns write no
    # status JSON, so a busy system could read as dormant for hours.
    from datetime import datetime, timezone
    candidates = []
    if newest is not None:
        candidates.append(datetime.fromtimestamp(
            newest, timezone.utc).isoformat())
    if ledger_ts:
        candidates.append(str(ledger_ts))
    st["last_activity"] = max(candidates) if candidates else None
    return st


def render_status(st=None, now=None):
    """UI-06 System Status. Compact, and prominent only when something is
    actually wrong -- an inactive timer or an unreadable source."""
    st = system_status() if st is None else st
    bits = []
    bad = False
    for unit, state in sorted((st.get("timers") or {}).items()):
        label = unit.replace("treebeard-", "")
        if state is None:
            bits.append('<span class="sys-item sys-unknown">%s: Unknown</span>'
                        % html.escape(label))
            bad = True
        elif state == "active":
            bits.append('<span class="sys-item">%s: active</span>'
                        % html.escape(label))
        else:
            bits.append('<span class="sys-item sys-bad">%s: %s</span>'
                        % (html.escape(label), html.escape(state)))
            bad = True
    sp = st.get("spend")
    bits.append('<span class="sys-item">month spend: %s</span>'
                % ("$%.2f" % sp if isinstance(sp, (int, float))
                   else "Unknown"))
    if not isinstance(sp, (int, float)):
        bad = True
    pr = st.get("projects")
    if isinstance(pr, dict):
        active = pr.get("ACTIVE", 0)
        bits.append('<span class="sys-item">projects: %d active, %d total'
                    '</span>' % (active, sum(pr.values())))
    else:
        bits.append('<span class="sys-item sys-unknown">projects: Unknown'
                    '</span>')
        bad = True
    la = _ago(st.get("last_activity"), now=now)
    bits.append('<span class="sys-item">last activity: %s</span>'
                % ("%s ago" % la if la else "Unknown"))
    return ('<section id="status" class="sys%s"><h2>System Status</h2>%s'
            '</section>' % (" sys-attn" if bad else "", "".join(bits)))


def find_card(card_id, items=None):
    """One card by id, or None. Board is authoritative for existence."""
    try:
        cid = int(card_id)
    except (TypeError, ValueError):
        return None
    if items is None:
        items = board.load_board()
    for c in items:
        if c.get("id") == cid:
            return c
    return None


def _row(label, value, cls=""):
    return ('<div class="row%s"><dt>%s</dt><dd>%s</dd></div>'
            % ((" " + cls) if cls else "", html.escape(label),
               value if value else "<span class=\"unk\">Unknown</span>"))


def render_task(card, detail=None, now=None):
    """UI-09 Task Detail: identity and current state. Backend data is
    authoritative. Where the board and the execution record DISAGREE the
    page says so -- it never reconciles them into a single reassuring
    state, because a disagreement is exactly what the operator needs to
    see (UI-09, UI-11)."""
    if card is None:
        return ('<section id="task"><h1>No such card</h1><p class="empty">'
                'That card is not on the board.</p></section>')
    d = (card_detail(card) if detail is None else detail) or {}
    key = uistate.resolve(card, d)
    label, glyph, _css = uistate.get(key)
    raw = (card.get("text") or "").strip()
    rows = [_row("Card", "<span class=\"mono\">card %s</span>" % card["id"]),
            _row("State", uistate.badge(key)),
            _row("Board phase", "<span class=\"mono\">%s</span>"
                 % html.escape(str(card.get("phase") or "none"))),
            _row("Status", "<span class=\"mono\">%s</span>"
                 % html.escape(str(card.get("status") or ""))),
            _row("Lifecycle", "<span class=\"mono\">%s rev %s</span>"
                 % (html.escape(board.canonical_state(card)
                                if hasattr(board, "canonical_state")
                                else "?"),
                    html.escape(str(card.get("rev") or 0)))),
            _row("Owner", html.escape(str(card.get("owner")))
                 if card.get("owner") else None),
            _row("Source", html.escape(str(card.get("source") or "")) or None),
            _row("Project", "<span class=\"mono\">%s</span>"
                 % html.escape(str(card.get("meeseeks_pid")))
                 if card.get("meeseeks_pid") else None),
            _row("Section state", "<span class=\"mono\">%s</span>"
                 % html.escape(str(d.get("state"))) if d.get("state")
                 else None),
            _row("Project state", "<span class=\"mono\">%s</span>"
                 % html.escape(str(d.get("project_state")))
                 if d.get("project_state") else None),
            _row("Created", html.escape(str(card.get("created") or "")) or None),
            _row("On this card", _ago(d.get("started"), now=now)),
            _row("Last activity", (_ago(d.get("since"), now=now) or "") + " ago"
                 if _ago(d.get("since"), now=now) else None)]
    if card.get("outcome"):
        rows.append(_row("Outcome", '<span class="mono">%s</span>'
                         % html.escape(str(card["outcome"]))))
    if card.get("reason"):
        rows.append(_row("Board reason", '<span class="mono">%s</span>'
                         % html.escape(str(card["reason"]))))
    if d.get("reason"):
        rows.append(_row("Reason", '<span class="mono">%s</span>'
                         % html.escape(str(d["reason"])), cls="hot"))
    if d.get("retries"):
        rows.append(_row("Retries", str(d["retries"])))

    # Conflict detection: the board's own phase vs the execution record.
    # These are DIFFERENT SOURCES, and when they disagree the UI reports
    # the disagreement rather than picking a winner.
    conflicts = []
    pstate = d.get("project_state")
    if card.get("status") == "done" and key == "unknown":
        conflicts.append("Board says this card is done, but its execution "
                         "record disagrees (project %s, section %s)."
                         % (pstate or "unknown", d.get("state") or "unknown"))
    if card.get("status") == "open" and key == "failed":
        conflicts.append("Board still holds this card open (phase %s), but "
                         "its execution is terminally failed."
                         % (card.get("phase") or "none"))
    if key == "blocked" and pstate == "ACTIVE" and d.get("state") != "BLOCKED":
        conflicts.append("Board says blocked, but the execution record does "
                         "not show a blocked section.")
    if _pid(card) and not d.get("state"):
        conflicts.append("Card is linked to project %s, but no section "
                         "record could be read for it." % _pid(card))
    warn = ""
    if conflicts:
        warn = ('<div class="conflict"><strong>Sources disagree</strong><ul>'
                + "".join("<li>%s</li>" % html.escape(c) for c in conflicts)
                + "</ul></div>")
    attn = ""
    if uistate.is_attention(key):
        attn = ('<p class="task-attn">%s %s</p>'
                % (uistate.badge(key),
                   html.escape(_REASONS.get(key, label))))
    return ('<section id="task"><h1>card %s</h1>%s%s'
            '<p class="task-text">%s</p><dl class="task-rows">%s</dl>'
            '</section>'
            % (card["id"], attn, warn,
               html.escape(raw) or "<em>untitled card</em>", "".join(rows)))


# The states Meeseeks actually PERSISTS for a section. Verified against
# the live ledger 2026-08-31: QUEUED, BLOCKED, DONE, AWAITING_TREEBEARD --
# four values, not six. There is no persisted TESTING or REVIEW1/2/3 row:
# review cycles happen between ticks and are recorded as EVENTS, never as
# section state. A tracker drawing R1/R2/R3 pips would be inventing
# positions the database cannot confirm.
_SECTION_FLOW = (("QUEUED", "Queued"),
                 ("WORKING", "Working"),
                 ("AWAITING_TREEBEARD", "Your approval"),
                 ("DONE", "Done"))
_FLOW_INDEX = {"QUEUED": 0, "AWAITING_TREEBEARD": 2, "DONE": 3}


def render_stage(detail, now=None):
    """UI-10 stage tracker over the REAL persisted vocabulary. BLOCKED is
    not a step -- it is a halt at whichever step was reached, so it is
    shown as a halt marker rather than a position. Ladder escalation
    (sections.stage) is model-tier spend, NOT workflow position, and is
    labelled as such."""
    d = detail or {}
    state = d.get("state")
    if not state:
        return ('<div class="stage-track stage-unknown">Stage unknown &mdash; '
                'no section record could be read.</div>')
    blocked = state == "BLOCKED"
    # A blocked section halted somewhere; the record does not say where, so
    # the tracker refuses to guess a position for it.
    here = _FLOW_INDEX.get(state)
    pips = []
    for i, (_code, label) in enumerate(_SECTION_FLOW):
        cls = "pip"
        if here is not None and i < here:
            cls += " done"
        elif here is not None and i == here:
            cls += " now"
        pips.append('<span class="%s">%s</span>' % (cls, html.escape(label)))
    halt = ""
    if blocked:
        halt = ('<p class="stage-halt">Halted: %s</p>'
                % html.escape(str(d.get("reason") or "blocked")))
    esc = ""
    stg = d.get("stage")
    if isinstance(stg, int) and stg > 0:
        esc = ('<p class="stage-esc">Escalated to a paid model tier '
               '(ladder stage %d).</p>' % stg)
    note = ""
    if here is None and not blocked:
        note = ('<p class="stage-halt">State %s is not a known workflow '
                'position.</p>' % html.escape(str(state)))
    return ('<div class="stage-track%s">%s</div>%s%s%s'
            % (" halted" if blocked else "", "".join(pips), halt, note, esc))


def render_attention(items=None, details=None, now=None):
    """UI-03 Needs Your Attention. Every item is its own card, never a
    carousel; type is readable without opening; absent facts are omitted
    rather than invented."""
    att = attention_cards(items)
    if not att:
        return ('<section id="attention"><h1>Needs Your Attention</h1>'
                '<p class="empty">Nothing needs you right now. Treebeard '
                'continues on its own.</p></section>')
    out = ['<section id="attention"><h1>Needs Your Attention '
           '<span class="count">%d</span></h1><ul class="att-list">' % len(att)]
    for card, key in att:
        d = (details or {}).get(card["id"]) if details is not None             else card_detail(card)
        raw = (card.get("text") or "").strip().split("\n")[0]
        title = html.escape(raw if len(raw) <= 120 else raw[:119].rstrip() + "\u2026")
        bits = ['<li class="att-card att-%s">' % key,
                '<a class="att-id" href="/task/%s">%s</a>'
                % (card["id"], html.escape("card %s" % card["id"])),
                '<span class="att-title">%s</span>' % title,
                uistate.badge(key),
                '<span class="att-reason">%s</span>'
                % html.escape(_REASONS.get(key, uistate.get(key)[0]))]
        detail_reason = (d or {}).get("reason")
        if detail_reason:
            bits.append('<span class="att-why">%s</span>'
                        % html.escape(str(detail_reason)[:160]))
        waited = _ago((d or {}).get("since"), now=now)
        if waited:
            bits.append('<span class="att-waited">waiting %s</span>' % waited)
        bits.append('</li>')
        out.append("".join(bits))
    out.append("</ul></section>")
    return "".join(out)


def failed_ids(items=None):
    """Ids of OPEN cards whose linked execution is terminally unsuccessful
    (THE resolver says failed), in ONE ledger connection. A ledger we
    cannot read yields the empty set -- degrade detail, never invent
    failure."""
    if items is None:
        items = board.load_board()
    linked = [c for c in items if _pid(c) and c.get("status") == "open"]
    if not linked:
        return set()
    return {c.get("id") for c, k in resolve_items(linked) if k == "failed"}


def attention_cards(items=None, failed=None, details=None):
    """Board cards needing human action, most recently created last.
    items: pre-loaded board list (tests); None loads the live board.
    failed: optional precomputed set of failed ids (tests); details:
    optional {id: detail}. Every page (Home, Chat, Board) gets the SAME
    count from this one computation through THE resolver; terminal Failed
    is not an attention state, so those cards leave the count.

    Cost: the ledger is consulted only for cards that are BOTH in a
    board-level attention phase AND linked to an execution -- otherwise
    not at all. One batched pass, never one read per card."""
    if items is None:
        items = board.load_board()
    cand = [c for c in items if c.get("status") == "open"
            and uistate.is_attention(uistate.resolve(c))]
    if failed is None:
        if details is None:
            linked = [c for c in cand if _pid(c)]
            details = details_for(linked) if linked else {}
        keyed = resolve_items(cand, details)
    else:
        keyed = [(c, "failed" if c.get("id") in failed
                  else uistate.resolve(c)) for c in cand]
    return [(c, k) for c, k in keyed if uistate.is_attention(k)]


def render_header(active, n=None, items=None):
    """Persistent shell header. active: 'home'|'board'. n: precomputed
    attention count (so one page load computes once); None computes here."""
    if n is None:
        try:
            n = len(attention_cards(items))
        except Exception:
            n = None            # unknown, never silently zero (UI-17)
    if n is None:
        return ('<header class="tb-shell"><span class="tb-title">Treebeard'
                '</span><nav>%s</nav><span class="needs-you">NEEDS YOU: '
                '?</span></header>'
                % "".join('<a href="/%s"%s>%s</a>'
                          % (sl, ' class="nav-active"' if sl == active else "",
                             lb)
                          for sl, lb in (("home", "Home"), ("chat", "Chat"),
                                         ("board", "Board"))))
    links = []
    # Chat is a PRIMARY surface, not a utility (UI-CHAT navigation).
    for slug, label in (("home", "Home"), ("chat", "Chat"),
                        ("board", "Board")):
        cls = ' class="nav-active"' if slug == active else ""
        links.append('<a href="/%s"%s>%s</a>' % (slug, cls, label))
    badge_cls = "needs-you hot" if n > 0 else "needs-you"
    badge = ('<a href="/home#attention" class="%s" '
             'aria-label="Needs you: %d items">NEEDS YOU: %d</a>'
             % (badge_cls, n, n))
    return ('<header class="tb-shell"><span class="tb-title">Treebeard</span>'
            '<nav>%s</nav>%s</header>' % ("".join(links), badge))


def _selftest():
    mk = lambda i, ph, st="open": {"id": i, "status": st, "phase": ph}
    fix = [mk(1, None), mk(2, "ready"), mk(3, "in_progress"), mk(4, "review"),
           mk(5, "returned"), mk(6, "blocked"), mk(7, None, "done"),
           mk(8, "GARBAGE")]
    att = attention_cards(fix)
    assert [c["id"] for c, _ in att] == [4, 5, 6], att
    assert {k for _, k in att} == {"review", "returned", "blocked"}
    # Terminal failure is excluded from the count when the caller has
    # resolved it, and the default path stays phase-only.
    _ids = {c.get("id") for c, _ in att}
    _one = sorted(_ids)[0]
    assert len(attention_cards(fix, failed={_one})) == len(att) - 1
    assert len(attention_cards(fix, failed=set())) == len(att)
    # The DEFAULT path must resolve terminal failure itself -- that is what
    # makes Home, Chat and Board agree. Proven by substituting the batch
    # detail reader, so the assertion does not depend on live ledger data.
    _real_df = details_for
    try:
        globals()["details_for"] = lambda cards: {
            c.get("id"): {"project_state": "FACTORY_EXHAUSTED"}
            for c in cards}
        _linked = [dict(c, meeseeks_pid="p%s" % c.get("id"))
                   for c, _ in att]
        _rest = [c for c in fix if c.get("id") not in _ids]
        assert attention_cards(_linked + _rest) == [], \
            "terminally failed linked cards must not count"
        # An unlinked card in an attention state still counts, and no
        # ledger call is made for it at all.
        _calls = []
        globals()["details_for"] = lambda cards: (_calls.append(1), {})[1]
        assert len(attention_cards(fix)) == len(att)
        assert _calls == [], "unlinked board must not touch the ledger"
    finally:
        globals()["details_for"] = _real_df
    assert attention_cards([mk(7, None, "done")]) == []
    # unknown phase is NOT attention (UI-17: unknown != any known state)
    assert all(c["id"] != 8 for c, _ in att)
    h = render_header("home", items=fix)
    assert "NEEDS YOU: 3" in h and "hot" in h and 'aria-label="Needs you: 3' in h
    h0 = render_header("board", items=[mk(7, None, "done")])
    assert "NEEDS YOU: 0" in h0 and "hot" not in h0
    assert 'class="nav-active">Board' in h0 and 'nav-active">Home' not in h0
    assert render_header("home", n=5, items=fix).count("NEEDS YOU: 5") == 1
    # --- UI-03 -----------------------------------------------------------
    from datetime import datetime, timezone
    NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert _ago(None, NOW) is None and _ago("", NOW) is None
    assert _ago("not-a-date", NOW) is None
    assert _ago("2026-01-03T12:00:00+00:00", NOW) is None      # future
    assert _ago("2026-01-02T11:59:30+00:00", NOW) == "30s"
    assert _ago("2026-01-02T11:52:18+00:00", NOW) == "7m 42s"
    assert _ago("2026-01-02T09:30:00+00:00", NOW) == "2h 30m"
    assert _ago("2026-01-01T06:00:00+00:00", NOW) == "1d 6h"
    empty = render_attention([mk(9, None, "done")])
    assert "Nothing needs you" in empty and "att-card" not in empty
    det = {4: {"since": "2026-01-02T11:52:18+00:00", "reason": "boom"},
           5: {}, 6: {"since": None, "reason": None}}
    h = render_attention(fix, details=det, now=NOW)
    assert h.count("att-card") == 3
    assert 'class="count">3<' in h
    # types distinguishable without opening the card
    for k in ("att-review", "att-returned", "att-blocked"):
        assert k in h, k
    assert "Waiting for your review" in h and "Returned" in h
    # L: pre-work approval is its own attention type.
    ah = render_attention([mk(31, "awaiting_approval")], details={31: {}})
    assert "att-approval" in ah and "Waiting for your approval" in ah
    assert "waiting 7m 42s" in h and h.count("att-waited") == 1
    assert "boom" in h and h.count("att-why") == 1
    # absent wait time is absent, never 0m
    assert "0m" not in h and "waiting None" not in h
    # untrusted card text is escaped, never injected
    eh = render_attention([{"id": 1, "status": "open", "phase": "blocked",
                            "text": "<script>x</script>&"}], details={1: {}})
    assert "<script>" not in eh and "&lt;script&gt;" in eh and "&amp;" in eh
    # long titles are cut with an ellipsis, not left mid-word bare
    lh = render_attention([{"id": 2, "status": "open", "phase": "review",
                            "text": "z" * 400}], details={2: {}})
    assert "\u2026" in lh
    assert len(lh.split('att-title">')[1].split("<")[0]) == 120
    # --- UI-04 -----------------------------------------------------------
    assert [c["id"] for c, _ in working_cards(fix)] == [3]
    assert working_cards([mk(3, "in_progress", "done")]) == []
    we = render_working([mk(4, "review")])
    assert "not working on anything" in we and "work-card" not in we
    wd = {3: {"state": "TESTING", "since": "2026-01-02T11:52:18+00:00",
              "started": "2026-01-02T09:30:00+00:00", "retries": 2}}
    w = render_working(fix, details=wd, now=NOW)
    assert w.count("work-card") == 1 and 'class="count">1<' in w
    assert "TESTING" in w
    assert "last activity 7m 42s ago" in w and "on this card 2h 30m" in w
    assert "2 retries" in w
    # No fabricated progress: reject a percentage VALUE (digits followed by
    # a percent sign) and completion-estimate language. Bare "%" and "eta"
    # substrings are unusable as guards -- they match CSS units and the word
    # "meta" respectively.
    import re as _re
    vis = _re.sub(r"<[^>]+>", " ", w)
    assert not _re.search(r"\d+\s*%", vis), vis
    assert not _re.search(r"(?i)\b(eta|estimated|complete in|remaining)\b", vis), vis
    assert "progress" not in vis.lower()
    # retries hidden at zero; unknown stage says so; absent times omitted
    w0 = render_working(fix, details={3: {"retries": 0}}, now=NOW)
    assert "retry" not in w0 and "retries" not in w0
    assert "stage unknown" in w0
    assert "last activity" not in w0 and "on this card" not in w0
    assert "0m" not in w0
    w1 = render_working(fix, details={3: {"retries": 1, "state": "REVIEW1"}},
                        now=NOW)
    assert "1 retry" in w1 and "1 retries" not in w1
    wn = render_working([{"id": 8, "status": "open", "phase": "in_progress"}],
                        details={8: {}})
    assert "untitled card" in wn and 'att-title"></span>' not in wn
    ws = render_working([{"id": 7, "status": "open", "phase": "in_progress",
                          "text": "<b>x</b>"}], details={7: {}})
    assert "<b>x</b>" not in ws and "&lt;b&gt;" in ws
    # --- UI-05 -----------------------------------------------------------
    fin_fix = [{"id": 20, "status": "done", "text": "ok work",
                "closed": "2026-01-02T11:52:18+00:00"},
               {"id": 21, "status": "done", "text": "failed work",
                "closed": "2026-01-01T06:00:00+00:00"},
               {"id": 22, "status": "done", "text": "no record",
                "closed": "2026-01-02T09:30:00+00:00"},
               {"id": 23, "status": "open", "phase": "blocked",
                "text": "still blocked"},
               {"id": 24, "status": "open", "phase": "review",
                "text": "awaiting you"}]
    # only genuinely closed cards, newest first
    assert [c["id"] for c in finished_cards(fin_fix)] == [20, 22, 21]
    assert finished_cards(fin_fix, limit=2) == finished_cards(fin_fix)[:2]
    assert render_finished([], details={}).count("fin-card") == 0
    assert "Nothing has finished yet" in render_finished([], details={})
    fd = {20: {"project_state": "CLOSED"},
          21: {"project_state": "CLOSED_BLOCKED"},
          22: {}}
    f = render_finished(fin_fix, details=fd, now=NOW)
    assert f.count("fin-card") == 3
    # blocked / awaiting cards never appear here at all
    assert "still blocked" not in f and "awaiting you" not in f
    # a done card over a CLOSED_BLOCKED project is a DISAGREEMENT: THE
    # resolver says Unknown -- never Done (L: one resolver, UI-17)
    fail_li = [x for x in f.split("<li") if "failed work" in x][0]
    assert 'aria-label="Unknown"' in fail_li
    assert 'aria-label="Done"' not in fail_li
    # L: closed cards are finished too -- Failed or Closed, never Waiting
    cl = render_finished([
        {"id": 40, "status": "closed", "outcome": "failed", "text": "f",
         "closed": "2026-01-02T11:52:18+00:00", "reason": "exhausted"},
        {"id": 41, "status": "closed", "outcome": "superseded",
         "text": "s", "closed": "2026-01-02T11:00:00+00:00"}],
        details={}, now=NOW)
    f40 = [x for x in cl.split("<li") if "card 40" in x][0]
    f41 = [x for x in cl.split("<li") if "card 41" in x][0]
    assert 'aria-label="Failed"' in f40 and "Waiting" not in cl
    assert 'aria-label="Closed"' in f41 and "superseded" in f41
    ok_li = [x for x in f.split("<li") if "ok work" in x][0]
    assert 'aria-label="Done"' in ok_li
    # unlinked card: board says done, and nothing is invented beyond it
    nr_li = [x for x in f.split("<li") if "no record" in x][0]
    assert 'aria-label="Done"' in nr_li
    # completion times are the card's own, never manufactured
    assert "7m 42s ago" in f and "1d 6h ago" in f and "2h 30m ago" in f
    nt = render_finished([{"id": 25, "status": "done", "text": "no ts"}],
                         details={25: {}}, now=NOW)
    assert "fin-when" not in nt and "0m" not in nt
    # an ACTIVE project under a closed card is a disagreement, not success
    ac = render_finished([{"id": 26, "status": "done", "text": "conflict",
                           "closed": "2026-01-02T11:52:18+00:00"}],
                         details={26: {"project_state": "ACTIVE"}}, now=NOW)
    assert 'aria-label="Unknown"' in ac and 'aria-label="Done"' not in ac
    fe = render_finished([{"id": 27, "status": "done", "text": "<i>x</i>",
                           "closed": "2026-01-02T11:52:18+00:00"}],
                         details={27: {}}, now=NOW)
    assert "<i>x</i>" not in fe and "&lt;i&gt;" in fe
    # --- UI-06 -----------------------------------------------------------
    good = {"timers": {"treebeard-reflector": "active"}, "spend": 12.5,
            "projects": {"ACTIVE": 3, "CLOSED": 1},
            "last_activity": "2026-01-02T11:52:18+00:00"}
    g = render_status(good, now=NOW)
    assert "reflector: active" in g and "$12.50" in g
    assert "3 active, 4 total" in g and "7m 42s ago" in g
    assert "last activity:" in g and "last agent activity" not in g
    assert "sys-attn" not in g, "healthy state is not alarming"
    assert "Unknown" not in g
    # Every unknown stays unknown and raises attention -- never healthy.
    blank = {"timers": {"treebeard-reflector": None}, "spend": None,
             "projects": None, "last_activity": None}
    b = render_status(blank, now=NOW)
    assert b.count("Unknown") == 4, b
    assert "sys-attn" in b and "$0.00" not in b and "0 active" not in b
    assert "0m ago" not in b and "healthy" not in b.lower()
    # A stopped timer is shown as stopped, and is attention-worthy.
    dead = dict(good, timers={"treebeard-reflector": "inactive"})
    d = render_status(dead, now=NOW)
    assert "reflector: inactive" in d and "sys-attn" in d
    # Newest-of-two: whichever source is more recent wins, and one
    # missing source never suppresses the other.
    assert max(["2026-01-02T09:30:00+00:00",
                "2026-01-02T11:52:18+00:00"]) == "2026-01-02T11:52:18+00:00"
    assert max(["2026-01-02T11:52:18+00:00"]) == "2026-01-02T11:52:18+00:00"
    # --- UI-09 -----------------------------------------------------------
    assert find_card(99, fix) is None and find_card("x", fix) is None
    assert find_card(3, fix)["id"] == 3
    assert "No such card" in render_task(None)
    tc = {"id": 58, "status": "open", "phase": "blocked", "text": "do a thing",
          "created": "2026-01-02T09:30:00+00:00", "owner": None,
          "source": "agent:treebeard", "meeseeks_pid": "card-58"}
    td = {"state": "BLOCKED", "project_state": "ACTIVE", "retries": 0,
          "since": "2026-01-02T11:52:18+00:00",
          "started": "2026-01-02T09:30:00+00:00", "reason": "oversize"}
    t9 = render_task(tc, detail=td, now=NOW)
    assert "card 58" in t9 and "do a thing" in t9
    assert 'aria-label="Blocked"' in t9 and "task-attn" in t9
    assert "BLOCKED" in t9 and "ACTIVE" in t9, "both sources shown"
    assert "oversize" in t9 and "7m 42s ago" in t9 and "2h 30m" in t9
    # Null owner is Unknown, never blank or invented.
    assert "Unknown" in t9
    # Retries hidden at zero.
    assert "Retries" not in t9
    # A done card whose project is still ACTIVE is a reported conflict.
    cf = render_task(dict(tc, status="done", phase=None),
                     detail=td, now=NOW)
    assert "Sources disagree" in cf and "record disagrees" in cf
    assert 'aria-label="Unknown"' in cf
    # L / live card 58: open board card over an EXHAUSTED project is
    # Failed on the task page too -- no "Blocked - cannot continue".
    t58 = render_task(tc, detail=dict(
        td, reason="FACTORY_EXHAUSTED: stages exhausted at github"), now=NOW)
    assert 'aria-label="Failed"' in t58 and "task-attn" not in t58
    assert "cannot continue" not in t58 and "terminally failed" in t58
    # a closed card shows its outcome, never Waiting
    tcl = render_task({"id": 9, "status": "closed", "outcome": "cancelled",
                       "phase": None, "text": "x", "reason": "residue",
                       "rev": 3}, detail={}, now=NOW)
    assert 'aria-label="Closed"' in tcl and "cancelled" in tcl
    assert "Waiting" not in tcl and "rev 3" in tcl
    # A linked card with no readable section is also a conflict.
    nl = render_task(tc, detail={"project_state": None}, now=NOW)
    assert "no section record" in nl
    # Clean card: no conflict banner at all.
    ok9 = render_task({"id": 7, "status": "open", "phase": "review",
                       "text": "x", "owner": "clark"},
                      detail={"state": "AWAITING_TREEBEARD"}, now=NOW)
    assert "Sources disagree" not in ok9 and "clark" in ok9
    # Untrusted card text is escaped here too.
    esc = render_task({"id": 8, "status": "open", "phase": None,
                       "text": "<img src=x>"}, detail={}, now=NOW)
    assert "<img src=x>" not in esc and "&lt;img" in esc
    # --- UI-10 -----------------------------------------------------------
    # Only the four persisted states are drawn; no TESTING/R1/R2/R3 pips.
    flow = [c for c, _ in _SECTION_FLOW]
    assert "TESTING" not in flow and "REVIEW1" not in flow
    q = render_stage({"state": "QUEUED"})
    assert q.count("pip") == 4 and "pip now" in q and "pip done" not in q
    aw = render_stage({"state": "AWAITING_TREEBEARD"})
    assert "Your approval" in aw and aw.count("pip done") == 2
    dn = render_stage({"state": "DONE"})
    assert dn.count("pip done") == 3 and "pip now" in dn
    # BLOCKED is a halt, not a position: no pip claims to be current.
    bl = render_stage({"state": "BLOCKED", "reason": "oversize 32044"})
    assert "Halted: oversize 32044" in bl and "pip now" not in bl
    assert "halted" in bl
    # No section record -> explicit unknown, never a hopeful first pip.
    assert "Stage unknown" in render_stage({})
    assert "Stage unknown" in render_stage(None)
    # Ladder stage is spend escalation, never called a workflow stage.
    e1 = render_stage({"state": "QUEUED", "stage": 1})
    assert "paid model tier" in e1 and "ladder stage 1" in e1
    e0 = render_stage({"state": "QUEUED", "stage": 0})
    assert "paid model tier" not in e0
    # An unrecognised state says so rather than being placed.
    nk = render_stage({"state": "SOMETHING_NEW"})
    assert "not a known workflow position" in nk and "pip now" not in nk
    print("opshell selftest: ALL PASS")


if __name__ == "__main__":
    if not __debug__:
        import sys as _sys
        print("opshell: selftest requires assertions; refusing to run under -O",
              file=_sys.stderr)
        raise SystemExit(2)
    _selftest()
