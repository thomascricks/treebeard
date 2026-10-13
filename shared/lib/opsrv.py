#!/usr/bin/env python3
# opsrv.py -- UI-01 operator console HTTP service. Stdlib only (no new
# dependency), READ-ONLY in this story: it serves the shell, Home and Board
# scaffolds. No write path, no Gate 2 action, no approval endpoint -- those
# arrive under UI-13 with an explicit privilege decision.
#
# BIND IS LOOPBACK-ONLY AND NOT CONFIGURABLE. This host has a public IPv4;
# an operator console reachable from the internet by flipping one env var is
# a defect, not a deployment option. Remote access = SSH tunnel.
import html
import os as _tb_os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_LIB = f"{_TB_BASE}/shared/lib"
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import agents
import opchat
import board
import opshell
import uistate

HOST = "127.0.0.1"                                   # never configurable
PORT = int(_tb_os.environ.get("TREEBEARD_UI_PORT", "8734"))

CSS = """
:root{--bg:#14161a;--fg:#e6e6e6;--dim:#9aa0a6;--line:#2a2f36;--hot:#e0533d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.5 ui-sans-serif,system-ui,sans-serif}
.tb-shell{display:flex;flex-wrap:wrap;gap:12px;align-items:center;
 padding:10px 14px;border-bottom:1px solid var(--line);position:sticky;top:0;
 background:var(--bg)}
.tb-title{font-weight:700;letter-spacing:.5px}
.tb-shell nav{display:flex;gap:4px}
.tb-shell nav a{color:var(--dim);text-decoration:none;padding:6px 10px;
 border-radius:6px}
.tb-shell nav a.nav-active{color:var(--fg);background:var(--line)}
.needs-you{margin-left:auto;color:var(--dim);text-decoration:none;
 padding:6px 10px;border:1px solid var(--line);border-radius:6px;
 font-variant-numeric:tabular-nums}
.needs-you.hot{color:#fff;background:var(--hot);border-color:var(--hot);
 font-weight:700}
a:focus-visible,button:focus-visible{outline:2px solid #6ea8fe;outline-offset:2px}
main{padding:16px 14px;max-width:900px}
h1{font-size:18px;margin:0 0 12px}
.empty{color:var(--dim);border:1px dashed var(--line);border-radius:8px;
 padding:14px}
.st{display:inline-flex;gap:6px;align-items:center}
.st-glyph{font-family:ui-monospace,monospace}
.count{color:var(--dim);font-weight:400}
.att-list{list-style:none;margin:0 0 18px;padding:0;display:grid;gap:10px}
.att-card{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline;
 padding:12px 14px;border:1px solid var(--line);border-radius:8px;
 border-left-width:4px}
.att-approval{border-left-color:#3d8bfd}
.att-returned{border-left-color:#d9a441}
main.wide{max-width:none}
.board{display:flex;gap:12px;align-items:flex-start;overflow-x:auto;
 padding:4px 2px 14px}
.col{flex:0 0 260px;display:flex;flex-direction:column;min-width:0;
 border:1px solid var(--line);border-radius:10px;background:#12151a;
 max-height:calc(100vh - 190px)}
.col h2{margin:0;padding:10px 12px;font-size:13px;letter-spacing:.04em;
 text-transform:uppercase;border-bottom:1px solid var(--line);
 display:flex;justify-content:space-between;align-items:center;gap:8px}
.col .n{color:var(--dim);font-variant-numeric:tabular-nums;font-weight:400}
.col-cards{overflow-y:auto;padding:10px;display:flex;flex-direction:column;
 gap:8px}
.bcard{display:block;padding:9px 10px;border:1px solid var(--line);
 border-radius:8px;background:#171b21;color:var(--fg);text-decoration:none}
.bcard:hover{border-color:#3d8bfd}
.bcard.att{border-left:3px solid #d9a441}
.bcard .bid{color:var(--dim);font-size:12px;
 font-variant-numeric:tabular-nums}
.bcard .btext{display:block;margin:3px 0;overflow-wrap:anywhere}
.bcard .bmeta{color:var(--dim);font-size:12px;display:flex;gap:8px;
 flex-wrap:wrap;align-items:center}
.col .empty{padding:8px 2px;font-size:12px}
.att-blocked{border-left-color:var(--hot)}
.att-id{color:var(--fg);font-weight:700;text-decoration:none;
 font-family:ui-monospace,monospace}
.att-title{flex:1 1 260px;min-width:0}
.att-reason{color:var(--dim)}
.att-why{flex:1 1 100%;color:var(--dim);font-family:ui-monospace,monospace;
 font-size:13px}
.att-waited{color:var(--dim);font-variant-numeric:tabular-nums}
h2{font-size:16px;margin:22px 0 10px}
.work-list{list-style:none;margin:0 0 18px;padding:0;display:grid;gap:8px}
.work-card{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline;
 padding:10px 14px;border:1px solid var(--line);border-radius:8px}
.work-stage{font-family:ui-monospace,monospace;color:var(--fg)}
.work-meta{flex:1 1 100%;color:var(--dim);font-variant-numeric:tabular-nums}
#finished h2{color:var(--dim)}
.fin-list{list-style:none;margin:0 0 18px;padding:0;display:grid;gap:2px}
.fin-card{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline;
 padding:6px 4px;border-bottom:1px solid var(--line);font-size:14px;
 color:var(--dim)}
.fin-id{color:var(--dim);text-decoration:none;
 font-family:ui-monospace,monospace}
.fin-title{flex:1 1 240px;min-width:0}
.fin-when{font-variant-numeric:tabular-nums}
.sys{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;
 padding:10px 14px;border:1px solid var(--line);border-radius:8px;
 margin-bottom:18px}
.sys h2{margin:0 12px 0 0;font-size:14px}
.sys-item{color:var(--dim);font-size:13px;
 font-variant-numeric:tabular-nums}
.sys-attn{border-color:var(--hot)}
.sys-bad,.sys-unknown{color:var(--hot)}
.task-rows{margin:16px 0;display:grid;gap:0}
.task-rows .row{display:flex;gap:12px;padding:7px 0;
 border-bottom:1px solid var(--line)}
.task-rows dt{flex:0 0 140px;color:var(--dim);margin:0}
.task-rows dd{margin:0;flex:1;min-width:0;overflow-wrap:anywhere}
.task-rows .hot dd{color:var(--hot)}
.mono{font-family:ui-monospace,monospace}
.unk{color:var(--hot)}
.task-text{font-size:16px;line-height:1.6;max-width:70ch}
.task-attn{display:flex;gap:8px;align-items:center;color:var(--dim)}
.conflict{border:1px solid var(--hot);border-radius:8px;padding:10px 14px;
 margin:12px 0;color:var(--hot)}
.conflict ul{margin:6px 0 0;padding-left:20px}
.conv{align-items:center}
.conv-sub{display:block;font-size:11px;color:var(--dim);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.newthread{display:flex;flex-direction:column;gap:4px;margin-top:10px}
.newthread input{background:transparent;color:var(--fg);
 border:1px solid var(--line);border-radius:4px;padding:5px 7px;
 font:inherit;font-size:12px}
.newthread button{background:var(--line);color:var(--fg);border:0;
 border-radius:4px;padding:6px 8px;font:inherit;font-size:12px;
 cursor:pointer}
.stage-track{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0 6px}
.pip{padding:5px 12px;border:1px solid var(--line);border-radius:20px;
 color:var(--dim);font-size:13px}
.pip.done{color:var(--fg);border-color:#3d8bfd}
.pip.now{color:#fff;background:#3d8bfd;border-color:#3d8bfd;font-weight:700}
.stage-track.halted .pip{opacity:.45}
.stage-unknown{color:var(--hot);border:1px dashed var(--line);
 border-radius:8px;padding:10px 14px}
.stage-halt{color:var(--hot);margin:6px 0}
.stage-esc{color:var(--dim);margin:6px 0;font-size:13px}
"""

PAGES = {"/": "home", "/home": "home", "/board": "board",
         "/chat": "chat"}


# UI-08 columns, left to right. Keys are uistate keys -- this module maps
# nothing itself; it asks uistate what a card is and renders the answer.
BOARD_COLUMNS = ("backlog", "queued", "approval", "working", "review",
                 "returned", "blocked", "failed", "done", "closed")


def board_cards(items=None, failed=None, details=None):
    """Every card, resolved through THE shared resolver (uistate.resolve via
    opshell.resolve_items) and grouped by operator state. failed: optional
    precomputed failed ids (tests). Returns an ordered dict of key ->
    [card]."""
    if items is None:
        items = board.load_board()
    if failed is not None:
        keyed = [(c, "failed" if (c.get("id") in failed
                                  and c.get("status") == "open")
                  else uistate.resolve(c)) for c in items]
    else:
        keyed = opshell.resolve_items(items, details)
    cols = {k: [] for k in BOARD_COLUMNS}
    for c, key in keyed:
        if key not in cols:
            key = "unknown"
            cols.setdefault("unknown", [])
        cols[key].append(c)
    return cols


def board_body(items=None, failed=None):
    """The real board. Every value shown is read from the card or the
    ledger; nothing is estimated, and no progress or ETA is implied."""
    try:
        cols = board_cards(items, failed)
    except Exception as e:
        # Say WHY on the server log. A guard that reports failure by
        # rendering a tidy message and discarding the exception hides the
        # defect it catches -- that is how a missing import shipped here.
        sys.stderr.write("[ui] board render failed: %r\n" % (e,))
        return ("<h1>Board</h1><p class=\"empty\">Board unavailable: the "
                "board could not be read.</p>")
    out = ["<h1>Board</h1><div class=\"board\">"]
    for key in [k for k in BOARD_COLUMNS if k in cols] + \
            [k for k in cols if k not in BOARD_COLUMNS]:
        cards = cols[key]
        label = uistate.get(key)[0]
        out.append('<section class="col"><h2>%s<span class="n">%d</span></h2>'
                   '<div class="col-cards">'
                   % (html.escape(label), len(cards)))
        if not cards:
            out.append('<p class="empty">None</p>')
        for c in cards:
            att = uistate.is_attention(key)
            owner = c.get("owner") or "unassigned"
            meta = ['<span>%s</span>' % html.escape(str(owner))]
            # Only a real phase value earns a line. A null phase adds
            # nothing the column heading has not already said.
            if c.get("phase"):
                meta.append('<span>phase: %s</span>'
                            % html.escape(str(c.get("phase"))))
            out.append(
                '<a class="bcard%s" href="/task/%s"><span class="bid">#%s'
                '</span><span class="btext">%s</span>'
                '<span class="bmeta">%s</span></a>'
                % (" att" if att else "", html.escape(str(c.get("id"))),
                   html.escape(str(c.get("id"))),
                   html.escape(str(c.get("text") or "")),
                   "".join(meta)))
        out.append("</div></section>")
    out.append("</div>")
    return "".join(out)


def page(active, n, attention=None, working=None, finished=None,
         status=None):
    if attention is None:
        attention = opshell.render_attention()
    if working is None:
        working = opshell.render_working()
    if finished is None:
        finished = opshell.render_finished()
    if status is None:
        status = opshell.render_status()
    body = {"home": attention + working + finished + status
                    + "<p class=\"empty\">The command box arrives in "
                      "UI-07.</p>",
            "board": board_body()}[active]
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><title>Treebeard</title><style>%s</style>"
            "</head><body>%s<main class=\"%s\">%s</main>"
            "</body></html>"
            % (CSS, opshell.render_header(active, n=n),
               "wide" if active == "board" else "", body))


CHAT_CSS = """
.chat-wrap{display:flex;gap:0;height:calc(100vh - 52px)}
.chat-side{width:230px;border-right:1px solid var(--line);overflow-y:auto;
 padding:10px}
.chat-main{flex:1;display:flex;flex-direction:column;min-width:0}
.who{display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px;
 border-bottom:1px solid var(--line);align-items:center}
.who form{display:inline}
.pick{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;
 border:1px solid var(--line);border-radius:20px;background:none;
 color:var(--dim);cursor:pointer;font:inherit}
.pick.on{border-color:#3d8bfd;color:var(--fg);background:#1b2536}
.avatar{display:inline-flex;align-items:center;justify-content:center;
 border-radius:50%;font-size:12px;font-weight:700;color:#0d0f12;
 flex:0 0 auto;object-fit:cover}
.avatar-unknown{background:var(--line);color:var(--dim)}
.receipt{border:1px solid #4a5568;border-radius:6px;padding:8px 10px;
 background:#1a202c;font-size:12px;max-width:90%;}
.receipt-h{font-weight:600;letter-spacing:.04em;opacity:.75;
 margin-bottom:4px;}
.receipt pre{margin:0;white-space:pre-wrap;word-break:break-word;}
.status{border:1px dashed #4a5568;border-radius:6px;padding:6px 9px;
 background:#161b22;font-size:11px;max-width:90%;}
.status-h{font-weight:600;letter-spacing:.04em;opacity:.6;
 margin-bottom:3px;}
.status pre{margin:0;white-space:pre-wrap;word-break:break-word;}
.turns{flex:1;overflow-y:auto;padding:14px;display:flex;
 flex-direction:column;gap:14px}
.turn{display:flex;gap:10px;max-width:900px}
.turn .who-name{font-weight:700;font-size:13px}
.turn .meta{color:var(--dim);font-size:12px;
 font-variant-numeric:tabular-nums}
.turn .body{white-space:pre-wrap;overflow-wrap:anywhere}
.turn.me{align-self:flex-end;flex-direction:row-reverse;text-align:right}
.turn.me .bubble{background:#1b2536;border-radius:10px;padding:8px 12px}
.turn.err .body{color:var(--hot)}
.composer{display:flex;gap:8px;padding:12px 14px;
 border-top:1px solid var(--line)}
.composer textarea{flex:1;min-height:60px;background:#0f1115;
 color:var(--fg);border:1px solid var(--line);border-radius:8px;
 padding:8px;font:inherit;resize:vertical}
.composer button{padding:0 20px;border-radius:8px;border:none;
 background:#3d8bfd;color:#fff;font-weight:700;cursor:pointer}
.conv{display:flex;gap:8px;align-items:center;padding:7px 8px;
 border-radius:8px;color:var(--dim);text-decoration:none}
.conv:hover,.conv.on{background:var(--line);color:var(--fg)}
.login{max-width:420px;margin:60px auto;padding:20px;
 border:1px solid var(--line);border-radius:10px}
.login input{width:100%;padding:9px;background:#0f1115;color:var(--fg);
 border:1px solid var(--line);border-radius:6px;font:inherit}
.warn{color:var(--hot)}
"""


def login_page(msg=""):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><title>Treebeard</title><style>%s%s</style>"
            "</head><body><div class=\"login\"><h1>Treebeard</h1>"
            "<p>Paste the operator token to continue.</p>"
            "%s"
            "<form method=\"POST\" action=\"/login\">"
            "<label for=\"tok\">Operator token</label>"
            "<input id=\"tok\" name=\"token\" type=\"password\" autofocus>"
            "<p><button type=\"submit\">Sign in</button></p>"
            "</form></div></body></html>"
            % (CSS, CHAT_CSS,
               ('<p class="warn">%s</p>' % html.escape(msg)) if msg else ""))


def chat_page(session, selected=None, rows=None, convs=None, note=None,
              query=None, results=None, thread_id=None, threads=None):
    """Direct conversation workspace. Selection is explicit and visible
    before Send: the form posts the agent id it displays as chosen."""
    selected = selected or ""
    thread_id = opchat.resolve_thread(selected, thread_id) if selected else ""
    rows = rows if rows is not None else []
    convs = convs if convs is not None else []
    tok = opchat.csrf_token(session)
    picks = []
    for a in agents.roster():
        on = " on" if a["id"] == selected else ""
        picks.append(
            '<form method="POST" action="/chat"><input type="hidden" '
            'name="csrf" value="%s"><input type="hidden" name="agent" '
            'value="%s"><button class="pick%s" type="submit" name="pick" '
            'value="1"%s>%s<span>%s</span></button></form>'
            % (html.escape(tok), html.escape(a["id"]), on,
               ' aria-current="true"' if on else "",
               agents.avatar(a["id"], 26), html.escape(a["name"])))
    # THREADS: one entry per (agent, thread), not per agent. Entry shape is
    # uniform and kind-tagged so a meeting kind can join this same column
    # later without a second list. Label is display metadata only -- the
    # href carries the thread id, which is what selects history.
    side = []
    for c in (threads if threads is not None else []):
        ident = agents.get(c.get("agent"))
        if not ident:
            continue
        on = (" on" if (ident["id"] == selected
                        and c.get("thread_id") == thread_id) else "")
        side.append('<a class="conv%s" href="%s">%s<span>%s</span>'
                    '<span class="conv-sub">%s</span></a>'
                    % (on, html.escape(str(c.get("url") or "")),
                       agents.avatar(ident["id"], 26),
                       html.escape(ident["name"]),
                       html.escape(str(c.get("label") or ""))))
    if selected:
        side.append(
            '<form class="newthread" method="POST" action="/chat">'
            '<input type="hidden" name="csrf" value="%s">'
            '<input type="hidden" name="agent" value="%s">'
            '<label class="sr" for="tlabel">New thread name</label>'
            '<input id="tlabel" name="label" placeholder="New thread name">'
            '<button type="submit" name="new_thread" value="1">'
            '+ New thread</button></form>'
            % (html.escape(tok), html.escape(selected)))
    turns = []
    for r in rows:
        who = agents.get(r.get("agent"))
        name = who["name"] if who else str(r.get("agent"))
        turns.append('<div class="turn me"><div class="bubble">'
                     '<div class="who-name">You</div>'
                     '<div class="body">%s</div></div></div>'
                     % html.escape(str(r.get("prompt") or "")))
        if r.get("ok"):
            meta = " ".join(x for x in (r.get("tier"), r.get("model")) if x)
            turns.append('<div class="turn">%s<div><div class="who-name">%s'
                         '</div><div class="body">%s</div>'
                         '<div class="meta">%s &middot; %s</div></div></div>'
                         % (agents.avatar(r.get("agent"), 44),
                            html.escape(name),
                            html.escape(str(r.get("reply") or "")),
                            html.escape(str(r.get("ts") or "")),
                            html.escape(meta)))
            # WEB-STATUS: deterministic enforcement evidence from
            # ask.run_turn(), rendered in its OWN block -- not folded
            # into .meta, which carries ordinary turn metadata. The
            # operator distinction between system-generated evidence
            # and model prose is the property this story restores.
            # Escaped whole; never parsed, trimmed or reconstructed.
            # Legacy rows have no key -> falsy -> no block at all.
            _st = str(r.get("status") or "").strip()
            if _st:
                turns.append('<div class="turn"><div class="status">'
                             '<div class="status-h">TURN STATUS '
                             '(system-generated)</div><pre>%s</pre>'
                             '</div></div>' % html.escape(_st))
            # BOX-09: deterministic execution evidence, generated by
            # dispatch.render_receipts() and persisted WITH this turn.
            # Rendered separately from the reply so the operator can see
            # at a glance that it is system-generated, not model prose.
            # Turns written before BOX-09 have no such key and render
            # nothing, which is accurate -- they carried no receipt.
            _ev = str(r.get("receipt_evidence") or "").strip()
            if _ev:
                turns.append('<div class="turn"><div class="receipt">'
                             '<div class="receipt-h">EXECUTION RECEIPT '
                             '(system-generated)</div><pre>%s</pre>'
                             '</div></div>' % html.escape(_ev))
        else:
            turns.append('<div class="turn err">%s<div><div class="who-name">'
                         '%s</div><div class="body">turn failed: %s</div>'
                         '<div class="meta">%s</div></div></div>'
                         % (agents.avatar(r.get("agent"), 44),
                            html.escape(name),
                            html.escape(str(r.get("error") or "unknown")),
                            html.escape(str(r.get("ts") or ""))))
            # WEB-STATUS: deterministic enforcement evidence from
            # ask.run_turn(), rendered in its OWN block -- not folded
            # into .meta, which carries ordinary turn metadata. The
            # operator distinction between system-generated evidence
            # and model prose is the property this story restores.
            # Escaped whole; never parsed, trimmed or reconstructed.
            # Legacy rows have no key -> falsy -> no block at all.
            _st = str(r.get("status") or "").strip()
            if _st:
                turns.append('<div class="turn"><div class="status">'
                             '<div class="status-h">TURN STATUS '
                             '(system-generated)</div><pre>%s</pre>'
                             '</div></div>' % html.escape(_st))
    if results is not None:
        turns = ['<p class="empty">%d result(s) for %s</p>'
                 % (len(results), html.escape(str(query)))] + [
            '<div class="turn"><div><div class="who-name">%s</div>'
            '<div class="body">%s</div><div class="meta">%s</div></div></div>'
            % (html.escape(str(r.get("agent"))),
               html.escape((str(r.get("prompt") or "") + " -> "
                            + str(r.get("reply") or ""))[:400]),
               html.escape(str(r.get("ts") or ""))) for r in results]
    composer = ""
    if selected:
        composer = ('<form class="composer" method="POST" action="/chat">'
                    '<input type="hidden" name="csrf" value="%s">'
                    '<input type="hidden" name="agent" value="%s">'
                    '<input type="hidden" name="thread" value="%s">'
                    '<label class="sr" for="msg">Message %s</label>'
                    '<textarea id="msg" name="message" placeholder="Message '
                    '%s..." autofocus></textarea>'
                    '<button type="submit">Send</button></form>'
                    % (html.escape(tok), html.escape(selected),
                       html.escape(thread_id), html.escape(selected),
                       html.escape(selected)))
    else:
        composer = ('<p class="empty">Choose an agent above to start a '
                    'conversation.</p>')
    # Story: open at the newest message. .turns is its own scroll
    # container (flex:1;overflow-y:auto), so this positions that element,
    # not the window. Emitted at end of body: it runs before first paint
    # (no visible travel from the top) and after the composer is parsed,
    # so the flex height is settled. One assignment -- no listener, no
    # timer, no fetch -- so nothing can pull the operator back down once
    # they scroll up. Search results read top-down and are not pinned.
    pin = "" if results is not None else (
        '<script>(function(){var t=document.querySelector(".turns");'
        'if(t){t.scrollTop=t.scrollHeight;}})();</script>')
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><title>Treebeard chat</title>"
            "<style>%s%s</style></head><body>%s<div class=\"chat-wrap\">"
            "<aside class=\"chat-side\"><form method=\"GET\" "
            "action=\"/chat\"><label class=\"sr\" for=\"q\">Search"
            "</label><input id=\"q\" name=\"q\" placeholder=\"Search "
            "history\" value=\"%s\"></form>%s</aside>"
            "<main class=\"chat-main\"><div class=\"who\">%s</div>"
            "%s<div class=\"turns\">%s</div>%s</main></div>%s</body></html>"
            % (CSS, CHAT_CSS, opshell.render_header("chat", n=None),
               html.escape(str(query or "")), "".join(side), "".join(picks),
               ('<p class="warn">%s</p>' % html.escape(note)) if note else "",
               "".join(turns), composer, pin))


def task_page(card, detail=None, now=None, n=None):
    """UI-09 page frame around opshell.render_task."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><title>Treebeard task</title>"
            "<style>%s%s</style></head><body>%s<main>%s"
            "<p><a href=\"/home\">&larr; Home</a></p></main></body></html>"
            % (CSS, CHAT_CSS, opshell.render_header("home", n=n),
               opshell.render_task(card, detail=detail, now=now)
               + (opshell.render_stage(
                   opshell.card_detail(card) if detail is None else detail,
                   now=now) if card else "")))


class Handler(BaseHTTPRequestHandler):
    server_version = "treebeard-ui"
    sys_version = ""

    # --- session helpers ---------------------------------------------
    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tb_session":
                return v
        return None

    def _authed(self):
        return opchat.valid_session(self._cookie())

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie",
                             "tb_session=%s; Path=/; HttpOnly; SameSite=Strict"
                             % cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1_000_000:
            return {}
        import urllib.parse
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        form = self._form()
        if path == "/login":
            if opchat.check_token(form.get("token")):
                self._redirect("/chat", cookie=opchat.make_session())
            else:
                self._send(401, login_page("That token was not accepted."))
            return
        # EVERY state-changing route past this point requires a valid
        # session AND a session-bound CSRF token. No agent invocation, and
        # therefore no model spend, is reachable without both.
        sess = self._cookie()
        if not opchat.valid_session(sess):
            self._send(401, login_page("Session expired. Sign in again."))
            return
        if not opchat.valid_csrf(sess, form.get("csrf")):
            self._send(403, login_page("Request rejected (CSRF)."))
            return
        if path == "/chat":
            agent = (form.get("agent") or "").strip().lower()
            if agents.get(agent) is None:
                self._send(400, chat_page(sess, note="No such agent."))
                return

            thread_id = opchat.resolve_thread(agent, form.get("thread"))

            if form.get("new_thread"):
                row = opchat.create_thread(agent, form.get("label"))
                if row is None:
                    self._send(500, chat_page(
                        sess, selected=agent, thread_id=thread_id,
                        rows=opchat.conversation(agent, thread_id),
                        convs=opchat.conversations(),
                        threads=opchat.list_threads(agent),
                        note="Thread not created (not persisted)."))
                    return
                self._redirect(opchat.chat_url(agent, row["thread_id"]))
                return
            if form.get("pick"):
                self._redirect(opchat.chat_url(agent))
                return

            msg = (form.get("message") or "").strip()
            if not msg:
                self._redirect(opchat.chat_url(agent, thread_id))
                return

            try:
                # G2: this route is reached only after valid_session AND
                # valid_csrf above, so the turn's origin is operator-web.
                opchat.send(agent, msg, thread_id=thread_id,
                            origin="operator-web")
            except Exception as exc:
                sys.stderr.write("[ui] send failed: %r\n" % (exc,))

            self._redirect(opchat.chat_url(agent, thread_id))
            return
        self._send(404, login_page())

    # Anything not in this set requires an authenticated operator session.
    # The boundary is "does this belong to the operator console", NOT "does
    # this route mutate state" -- board text and task pages carry private
    # project detail, and a future proxy or bind change must not silently
    # make read-only operational data public.
    PUBLIC_PATHS = ("/login",)
    PUBLIC_PREFIXES = ("/portrait/",)

    def _public(self, path):
        return (path in self.PUBLIC_PATHS
                or any(path.startswith(p) for p in self.PUBLIC_PREFIXES))

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if not self._public(path) and not self._authed():
            self._send(401, login_page())
            return
        if path.startswith("/portrait/"):
            self._portrait(path[len("/portrait/"):])
            return
        if path == "/login":
            self._send(200, login_page())
            return
        if path.startswith("/task/"):
            card = opshell.find_card(path[len("/task/"):])
            self._send(200 if card else 404, task_page(card))
            return
        if path == "/chat":
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            sess = self._cookie()
            query = (q.get("q") or [""])[0].strip()
            if query:
                self._send(200, chat_page(sess, query=query,
                                          convs=opchat.conversations(),
                                          threads=opchat.list_threads(),
                                          results=opchat.search(query)))
                return
            agent = (q.get("agent") or [""])[0].strip().lower()
            if agent and agents.get(agent) is None:
                agent = ""

            # Canonicalize every selected conversation onto an explicit
            # thread URL. Old agent-only links remain valid entry points,
            # but the browser must never stay ambiguously agent-scoped.
            if agent and not (q.get("thread") or [""])[0].strip():
                self._redirect(opchat.chat_url(agent))
                return

            thread_id = (
                opchat.resolve_thread(
                    agent, (q.get("thread") or [""])[0])
                if agent else ""
            )

            self._send(200, chat_page(
                sess, selected=agent, thread_id=thread_id,
                rows=(opchat.conversation(agent, thread_id)
                      if agent else []),
                convs=opchat.conversations(),
                threads=opchat.list_threads(agent or None)))
            return
        active = PAGES.get(path)
        if active is None:
            self._send(404, "<!doctype html><meta charset=\"utf-8\">"
                            "<p>404 &mdash; no such page.</p>")
            return
        try:
            n = len(opshell.attention_cards())
        except Exception as exc:                     # UI-17: never fake healthy
            self._send(503, "<!doctype html><meta charset=\"utf-8\">"
                            "<p>Board unavailable: %s</p>"
                            % html.escape(type(exc).__name__))
            return
        self._send(200, page(active, n))

    def _portrait(self, name):
        """Serve only files the registry itself advertises. The request
        never supplies a path -- it supplies a name we look up."""
        import posixpath
        name = posixpath.basename(name or "")
        for a in agents.roster():
            if a["portrait"] and a["portrait"].endswith("/" + name):
                p = _tb_os.path.join(agents.PORTRAIT_DIR, name)
                try:
                    with open(p, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    break
                self.send_response(200)
                self.send_header("Content-Type",
                                 "image/svg+xml" if name.endswith(".svg")
                                 else "image/*")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
        self._send(404, "<!doctype html><p>no such portrait</p>")

    def _send(self, code, body):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[ui] %s\n" % (fmt % args))


def _host_ipv4s():
    """(interface, address) for every non-loopback IPv4 configured on THIS
    host, read from the kernel at test time (SIOCGIFADDR). The exposure probe
    targets whatever addresses the machine it runs on actually has, so no
    host address is ever written into this source."""
    import fcntl
    import socket
    import struct
    siocgifaddr = 0x8915
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            try:
                res = fcntl.ioctl(s.fileno(), siocgifaddr,
                                  struct.pack("256s", name.encode()[:15]))
            except OSError:
                continue                    # interface without an IPv4
            addr = socket.inet_ntoa(res[20:24])
            if not addr.startswith("127.") and addr not in [a for _n, a in out]:
                out.append((name, addr))
    finally:
        s.close()
    return out


def _selftest():
    # Fixture stubs FIRST: every page() call in this suite renders from
    # them, so a safe-tier run never touches the live board or ledger.
    SSTUB = opshell.render_status(
        {"timers": {}, "spend": 0.0, "projects": {}, "last_activity": None})
    # Behavioural, not textual: bind a real socket on an ephemeral port and
    # ask the OS what it bound to. A source grep for the wildcard address
    # would both match its own assertion text and miss equivalent spellings.
    import socket
    probe = HTTPServer((HOST, 0), Handler)
    try:
        bound = probe.socket.getsockname()[0]
        assert bound == "127.0.0.1", "bound to %r, not loopback" % bound
        assert probe.socket.family == socket.AF_INET
        targets = _host_ipv4s()
        if not targets:
            # Nothing non-loopback to probe: say so; never pass silently.
            print("opsrv selftest: exposure probe SKIPPED "
                  "(this host has no non-loopback IPv4)")
        for ifname, addr in targets:
            # Positive control first: a throwaway listener on this address
            # IS reachable, so a refusal below means "the console does not
            # listen there", not "this address cannot be reached at all".
            ctl = socket.socket()
            try:
                ctl.bind((addr, 0))
                ctl.listen(1)
                c = socket.socket()
                c.settimeout(0.4)
                try:
                    c.connect((addr, ctl.getsockname()[1]))
                finally:
                    c.close()
            finally:
                ctl.close()
            remote = socket.socket()
            remote.settimeout(0.4)
            try:
                refused = False
                try:
                    remote.connect((addr, probe.server_port))
                except OSError:
                    refused = True
                # name the interface, never the address, in any output
                assert refused, ("console reachable on interface %s's "
                                 "address" % ifname)
            finally:
                remote.close()
    finally:
        probe.server_close()
    fix = [{"id": 1, "status": "open", "phase": "review"}]
    h = page("home", len(opshell.attention_cards(fix)),
             attention=opshell.render_attention(fix, details={}),
             working=opshell.render_working(fix, details={}),
             finished=opshell.render_finished([], details={}),
             status=SSTUB)
    # Home must carry the attention section itself, not just the badge.
    assert "Needs Your Attention" in h and 'id="attention"' in h
    assert "Currently Working" in h and 'id="working"' in h
    assert "Recently Finished" in h and 'id="finished"' in h
    assert "System Status" in h and 'id="status"' in h
    # WEB-STATUS: turn status renders as its OWN block, once, escaped.
    _sess = opchat.make_session()
    _tstat = '[agent=t ok=True box:unverified-no-read <b>x</b>]'
    _agent = agents.roster()[0]["id"]
    _cp = chat_page(_sess, selected=_agent, rows=[
        {"agent": _agent, "ok": True, "prompt": "p", "reply": "a",
         "ts": "T", "status": _tstat}])
    assert _cp.count('class="status"') == 1, "exactly one status block"
    assert "TURN STATUS" in _cp
    assert html.escape(_tstat) in _cp, "status rendered verbatim, escaped"
    assert "<b>x</b>" not in _cp, "status must never render raw HTML"
    # Legacy row (no key) and quiet row (empty) render NO block at all.
    _cp2 = chat_page(_sess, selected=_agent, rows=[
        {"agent": _agent, "ok": True, "prompt": "p", "reply": "a",
         "ts": "T"},
        {"agent": _agent, "ok": True, "prompt": "q", "reply": "b",
         "ts": "T", "status": ""}])
    assert 'class="status"' not in _cp2, "no block without status"
    # Failed turns carry it too -- that is when it matters most.
    _cp3 = chat_page(_sess, selected=_agent, rows=[
        {"agent": _agent, "ok": False, "error": "boom", "prompt": "p",
         "ts": "T", "status": _tstat}])
    assert _cp3.count('class="status"') == 1, "error turn renders status"
    # UI-09: task page frames the detail and links back to Home.
    tp = task_page({"id": 58, "status": "open", "phase": "blocked",
                    "text": "t"}, detail={}, n=0)
    assert 'id="task"' in tp and "card 58" in tp and 'href="/home"' in tp
    assert task_page(None, n=0).count("No such card") == 1
    # UI-10: the tracker rides on the task page, and a missing card
    # renders no tracker at all rather than an empty one.
    ts_ = task_page({"id": 1, "status": "open", "phase": "in_progress",
                     "text": "t"}, detail={"state": "QUEUED"}, n=0)
    # Check the BODY, not the whole document: the stylesheet defines
    # .stage-track on every page, so a bare substring test would pass
    # whether or not a tracker was rendered.
    body_ = lambda h: h.split("</style>", 1)[1]
    assert 'class="stage-track' in body_(ts_) and "Your approval" in body_(ts_)
    assert 'class="stage-track' not in body_(task_page(None, n=0))
    assert "Your approval" not in body_(task_page(None, n=0))
    # Console-wide auth boundary: only login and portraits are public.
    h_ = Handler
    assert h_.PUBLIC_PATHS == ("/login",)
    for p in ("/", "/home", "/board", "/chat", "/task/58", "/task/999"):
        assert not h_._public(h_, p), "%s must require a session" % p
    assert h_._public(h_, "/login")
    assert h_._public(h_, "/portrait/clark.png")
    # A path that merely starts with a public-looking name is not public.
    assert not h_._public(h_, "/loginsomething")
    # Finished must sit after attention and work in document order:
    # subordinate placement is part of the contract, not styling taste.
    assert h.index('id="attention"') < h.index('id="working"') \
        < h.index('id="finished"')
    assert "NEEDS YOU: 1" in h and "hot" in h
    # Parse rather than substring-match: assert the document really declares
    # a language, a charset and a responsive viewport, whatever the quoting.
    from html.parser import HTMLParser

    class _Doc(HTMLParser):
        lang = charset = viewport = None
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "html":
                _Doc.lang = a.get("lang")
            if tag == "meta":
                if "charset" in a:
                    _Doc.charset = a["charset"]
                if a.get("name") == "viewport":
                    _Doc.viewport = a.get("content")

    _Doc.lang = _Doc.charset = _Doc.viewport = None
    _Doc().feed(h)
    assert _Doc.lang == "en", _Doc.lang
    assert (_Doc.charset or "").lower() == "utf-8", _Doc.charset
    assert "width=device-width" in (_Doc.viewport or ""), _Doc.viewport
    # Asserts the stylesheet SHIPS a focus rule -- not that focus behaves.
    # Real keyboard/focus verification needs a browser and is UI-20.
    assert "focus-visible" in CSS
    # Parse the anchors: exactly one nav link is marked active, and it is the
    # one matching the requested page. Counting the raw string would also
    # match the CSS rule that styles the class.
    class _Nav(HTMLParser):
        links = []
        depth = 0
        def handle_starttag(self, tag, attrs):
            if tag == "nav":
                _Nav.depth += 1
            elif tag == "a" and _Nav.depth:
                a = dict(attrs)
                _Nav.links.append((a.get("href", ""),
                                   "nav-active" in a.get("class", "").split()))
        def handle_endtag(self, tag):
            if tag == "nav" and _Nav.depth:
                _Nav.depth -= 1

    STUB = opshell.render_attention([], details={})
    WSTUB = opshell.render_working([], details={})
    FSTUB = opshell.render_finished([], details={})
    for want in ("home", "board"):
        _Nav.links = []
        _Nav.depth = 0
        _Nav().feed(page(want, 0, attention=STUB))
        active = [href for href, on in _Nav.links if on]
        assert [h for h, _ in _Nav.links] == ["/home", "/chat", "/board"], \
            _Nav.links
        assert active == ["/" + want], (want, active)
    assert "NEEDS YOU: 0" in page("board", 0, attention=STUB, working=WSTUB, finished=FSTUB, status=SSTUB)
    assert set(PAGES.values()) == {"home", "board", "chat"}
    # --- UI-08 board ---------------------------------------------------
    # The placeholder must be gone, and every column must be present even
    # when empty, so the operator sees the whole workflow.
    _mk = lambda i, ph, st, **kw: dict(
        {"id": i, "kind": "task", "text": "card %d" % i, "phase": ph,
         "status": st, "owner": "treebeard", "meeseeks_pid": None}, **kw)
    _items = [_mk(1, None, "open"), _mk(2, "in_progress", "open"),
              _mk(3, "review", "open"), _mk(4, "returned", "open"),
              _mk(5, "blocked", "open"), _mk(6, None, "done"),
              _mk(7, "blocked", "open", meeseeks_pid="p7")]
    bb = board_body(_items, failed={7})
    assert "UI-08" not in bb and "arrives in" not in bb
    for _lbl in ("Backlog", "Approved / queued", "Needs approval", "Working",
                 "Needs review", "Returned", "Blocked", "Failed", "Done",
                 "Closed"):
        assert _lbl in bb, _lbl
    assert "Waiting" not in bb, "L: Waiting is retired"
    # L: a closed card has its own column and is never Waiting/backlog.
    _cc = board_cards([_mk(8, None, "closed", outcome="cancelled"),
                       _mk(9, None, "closed", outcome="failed")], failed=set())
    assert [c["id"] for c in _cc["closed"]] == [8]
    assert [c["id"] for c in _cc["failed"]] == [9]
    assert _cc["backlog"] == []
    # Cards link to the EXISTING task-detail route, not a parallel one.
    assert 'href="/task/3"' in bb and 'href="/task/7"' in bb
    # Terminal execution wins over phase: card 7 is Failed, not Blocked.
    _cols = board_cards(_items, failed={7})
    assert [c["id"] for c in _cols["failed"]] == [7], _cols["failed"]
    assert [c["id"] for c in _cols["blocked"]] == [5]
    assert [c["id"] for c in _cols["done"]] == [6]
    assert [c["id"] for c in _cols["returned"]] == [4]
    # ...and it is not counted as needing a human.
    assert {c.get("id") for c, _ in
            opshell.attention_cards(_items, failed={7})} == {3, 4, 5}
    # No fabricated progress information anywhere on the board. NOTE: a
    # bare "progress" substring is NOT the test -- the real backend phase
    # is literally named in_progress and showing it is required. What must
    # never appear is an INVENTED indicator: a percentage, a progress bar,
    # or an ETA.
    assert "in_progress" in bb, "real phase name must be shown"
    # No null phase is exposed, and no bare state glyph stands in for a
    # label the column heading already provides.
    assert "phase: none" not in bb and "phase: None" not in bb
    assert "st-glyph" not in bb, "no decorative glyph on board cards"
    # The board page lifts main's reading-column cap; other pages keep it.
    _bp = page("board", 0, attention=STUB, working=WSTUB, finished=FSTUB,
               status=SSTUB)
    assert '<main class="wide">' in _bp
    assert "main.wide{max-width:none}" in CSS
    assert '<main class="">' in page("home", 0, attention=STUB,
                                     working=WSTUB, finished=FSTUB,
                                     status=SSTUB)
    import re as _re
    assert not _re.search(r"\d+\s*%", bb), "no fabricated percentages"
    for _bad in ("<progress", 'role="progressbar"', "ETA", "eta:",
                 "estimated"):
        assert _bad not in bb, _bad
    # The DEFAULT path -- no arguments -- is the one page() actually uses.
    # Asserting only the injected-items path is what let a missing import
    # ship: every test passed items, so board.load_board() never ran.
    _live = board_body()
    assert "unavailable" not in _live.lower(), _live[:200]
    assert 'class="board"' in _live
    assert board_cards() is not None
    # A board that cannot be read says so rather than rendering empty.
    import board as _b
    _real = _b.load_board
    _b.load_board = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    try:
        assert "unavailable" in board_body().lower()
    finally:
        _b.load_board = _real
    # --- UI-CHAT auth gates -------------------------------------------
    import tempfile as _tf
    _tmp = _tf.mkdtemp(prefix="opsrv_")
    opchat.SECRET_PATH = _tmp + "/secret"
    opchat.TOKEN_PATH = _tmp + "/token"
    opchat.INDEX_PATH = _tmp + "/index.json"
    sess = opchat.make_session()
    assert opchat.valid_session(sess) and not opchat.valid_session(None)
    assert opchat.valid_csrf(sess, opchat.csrf_token(sess))
    assert not opchat.valid_csrf(sess, "wrong")
    lp = login_page()
    assert opchat.operator_token() not in lp, "login page must not leak token"
    assert "password" in lp and "Sign in" in lp
    real = agents.existing_agents()[0]
    cp = chat_page(sess, selected=real, rows=[], convs=[])
    assert 'name="agent" value="%s"' % real in cp
    assert 'name="csrf" value="%s"' % opchat.csrf_token(sess) in cp
    assert cp.count('class="pick on"') == 1, "one agent shown chosen"
    cp0 = chat_page(sess, selected="", rows=[], convs=[])
    assert "Choose an agent" in cp0 and "<textarea" not in cp0
    ev = chat_page(sess, selected=real, convs=[], rows=[
        {"agent": real, "prompt": "<b>p</b>", "reply": "<script>x</script>",
         "ok": True, "ts": "t", "tier": "free", "model": "m"}])
    # The page now legitimately ships ONE script (newest-message
    # positioning), so "no <script> anywhere" would no longer prove
    # escaping. Assert the property directly: the agent's own script is
    # escaped, and the only script tag present is the known pin.
    assert "<script>x</script>" not in ev and "&lt;script&gt;" in ev
    assert ev.count("<script>") == 1, ev.count("<script>")
    assert 'document.querySelector(".turns")' in ev
    assert "<b>p</b>" not in ev
    fv = chat_page(sess, selected=real, convs=[], rows=[
        {"agent": real, "prompt": "p", "reply": None, "ok": False,
         "ts": "t", "error": "provider down"}])
    assert "turn failed" in fv and "provider down" in fv
    # --- UI-CHAT auth gates -------------------------------------------
    # Every one of these must hold before any agent invocation is
    # reachable, because an invocation spends model budget.
    import tempfile as _tf
    _tmp = _tf.mkdtemp(prefix="opsrv_")
    opchat.SECRET_PATH = _tmp + "/secret"
    opchat.TOKEN_PATH = _tmp + "/token"
    opchat.INDEX_PATH = _tmp + "/index.json"
    sess = opchat.make_session()
    assert opchat.valid_session(sess)
    assert not opchat.valid_session(None)
    assert opchat.valid_csrf(sess, opchat.csrf_token(sess))
    assert not opchat.valid_csrf(sess, "wrong")
    # The login page never leaks the token it checks against.
    lp = login_page()
    assert opchat.operator_token() not in lp
    assert "password" in lp and "Sign in" in lp
    # Chat page: selection is explicit, and the posted agent id is the one
    # displayed as chosen -- no silent rerouting.
    real = agents.existing_agents()[0]
    cp = chat_page(sess, selected=real, rows=[], convs=[])
    assert 'name="agent" value="%s"' % real in cp
    assert 'name="csrf" value="%s"' % opchat.csrf_token(sess) in cp
    assert cp.count('class="pick on"') == 1, "exactly one agent shown chosen"
    # No agent selected -> no composer, so nothing can be sent blind.
    cp0 = chat_page(sess, selected="", rows=[], convs=[])
    assert "Choose an agent" in cp0 and "<textarea" not in cp0
    # Agent replies are escaped: an agent cannot inject markup into the UI.
    ev = chat_page(sess, selected=real, convs=[], rows=[
        {"agent": real, "prompt": "<b>p</b>", "reply": "<script>x</script>",
         "ok": True, "ts": "t", "tier": "free", "model": "m"}])
    # The page now legitimately ships ONE script (newest-message
    # positioning), so "no <script> anywhere" would no longer prove
    # escaping. Assert the property directly: the agent's own script is
    # escaped, and the only script tag present is the known pin.
    assert "<script>x</script>" not in ev and "&lt;script&gt;" in ev
    assert ev.count("<script>") == 1, ev.count("<script>")
    assert 'document.querySelector(".turns")' in ev
    assert "<b>p</b>" not in ev
    # A failed turn is shown as failed, never as an answer.
    fv = chat_page(sess, selected=real, convs=[], rows=[
        {"agent": real, "prompt": "p", "reply": None, "ok": False,
         "ts": "t", "error": "provider down"}])
    assert "turn failed" in fv and "provider down" in fv
    # Opens at the newest message. These assert the SERVED MARKUP: that
    # the positioning script ships, and that nothing capable of moving
    # the viewport later ships with it. A browser is required to prove
    # the scroll itself; that proof is the operator acceptance test.
    long_ = chat_page(sess, selected=real, convs=[], rows=[
        {"agent": real, "prompt": "p%d" % i, "reply": "r%d" % i,
         "ok": True, "ts": "t", "tier": "free", "model": "m"}
        for i in range(40)])
    assert "scrollTop" in body_(long_), "chat must position at newest"
    assert "scrollHeight" in body_(long_)
    # Search results are read from the top, so they must NOT be pinned.
    sr_ = chat_page(sess, selected=real, convs=[], rows=[], query="x",
                    results=[{"agent": real, "prompt": "p", "reply": "r",
                              "ts": "t"}])
    assert "scrollTop" not in body_(sr_), "search results open at top"
    # Nothing may poll, defer or animate: that is what would snap the
    # operator back to the bottom while they are reading history.
    for _b in ("setInterval", "setTimeout", "fetch(", "scroll-behavior",
               "scrollIntoView", "requestAnimationFrame"):
        assert _b not in long_, "chat page must not ship %s" % _b
    assert PAGES["/"] == "home"
    # Error branches: unknown path is 404, and a board that cannot be read
    # renders an explicit failure, never an empty-but-healthy page (UI-17).
    class _Cap:
        code = None
        body = b""
    def _probe(path, boom=False):
        import io, types
        cap = _Cap()
        h = Handler.__new__(Handler)
        h.path = path
        # The console-wide auth gate reads headers on every GET, so the
        # mock must carry a real authenticated session -- otherwise these
        # branch tests would silently all become 401 checks.
        import email.message
        h.headers = email.message.Message()
        h.headers["Cookie"] = "tb_session=%s" % opchat.make_session()
        h.wfile = io.BytesIO()
        h.send_response = lambda c, *a: setattr(cap, "code", c)
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        real = opshell.attention_cards
        if boom:
            opshell.attention_cards = lambda *a, **k: (_ for _ in ()).throw(
                OSError("board gone"))
        try:
            h.do_GET()
        finally:
            opshell.attention_cards = real
        cap.body = h.wfile.getvalue()
        return cap
    # Gate proof through the real handler: no cookie -> 401, whatever the
    # path would otherwise return.
    _bare = Handler.__new__(Handler)
    _bare.path = "/home"
    import email.message as _em
    _bare.headers = _em.Message()
    import io as _io
    _bare.wfile = _io.BytesIO()
    _codes = []
    _bare.send_response = lambda c, *a: _codes.append(c)
    _bare.send_header = lambda *a: None
    _bare.end_headers = lambda: None
    _bare.do_GET()
    assert _codes == [401], _codes
    assert _probe("/nope").code == 404
    assert _probe("/home").code == 200
    bad = _probe("/home", boom=True)
    assert bad.code == 503, bad.code
    assert b"unavailable" in bad.body.lower(), bad.body[:80]
    assert b"NEEDS YOU" not in bad.body, "failure page must not fake a count"

    # --- THREADS story assertions ---------------------------------------
    _a = agents.roster()[0]["id"]
    _sess = opchat.make_session()
    _th = [{"kind": "direct", "agent": _a, "thread_id": "web-default:%s" % _a,
            "label": "Workspace", "ts": "",
            "url": opchat.chat_url(_a, "web-default:%s" % _a)},
           {"kind": "direct", "agent": _a, "thread_id": "web:%s:t2" % _a,
            "label": "Second thread", "ts": "",
            "url": opchat.chat_url(_a, "web:%s:t2" % _a)}]
    _pg = chat_page(_sess, selected=_a, thread_id="web:%s:t2" % _a,
                    rows=[], threads=_th)
    # One sidebar entry per THREAD, each labeled and separately addressable.
    # Count ANCHORS, not the substring: "conv" also matches the
    # .conv-sub rule in the stylesheet, which would make a collapsed
    # sidebar look populated.
    assert _pg.count('<a class="conv') == 2, "sidebar collapsed threads"
    assert "Workspace" in _pg and "Second thread" in _pg, "labels missing"
    assert "thread=web%3A" in _pg.replace("&amp;", "&"), "thread not in href"
    # Exactly the selected thread is marked current -- not the agent.
    assert _pg.count('class="conv on"') == 1, "selection not thread-scoped"
    # Create control is present and CSRF-bound.
    assert 'name="new_thread"' in _pg, "no create control"
    assert opchat.csrf_token(_sess) in _pg, "create control not csrf-bound"
    # No agent selected -> no create control (nothing to create against).
    assert 'name="new_thread"' not in chat_page(_sess, threads=_th), \
        "create control shown with no agent"
    # The search view keeps its sidebar.
    _sp = chat_page(_sess, query="x", results=[], threads=_th)
    assert "Second thread" in _sp, "search view lost the thread list"
    # G2 (governance hardening chunk 1): the only web turn entry passes
    # operator-web, and only after the session AND CSRF gates.
    _osrc = open(__file__, encoding="utf-8").read()
    _needle = "opchat." + "send("
    assert _osrc.count(_needle) == 1, "exactly one web turn entry"
    _i_send = _osrc.index(_needle)
    _i_sess = _osrc.index("if not opchat.valid_session(sess):")
    _i_csrf = _osrc.index('if not opchat.valid_csrf(sess, form.get("csrf")):')
    assert _i_sess < _i_csrf < _i_send, \
        "the web turn must follow the session and CSRF gates"
    assert ('origin="operator-' + 'web"') in _osrc[_i_send:_i_send + 200], \
        "the authenticated web route must pass origin=operator-web"
    print("opsrv selftest: ALL PASS")


if __name__ == "__main__":
    if "serve" in sys.argv:
        srv = HTTPServer((HOST, PORT), Handler)
        sys.stderr.write("[ui] listening on http://%s:%d\n" % (HOST, PORT))
        srv.serve_forever()
    else:
        if not __debug__:
            import sys as _sys
            print("opsrv: selftest requires assertions; refusing to run under -O",
                  file=_sys.stderr)
            raise SystemExit(2)
        _selftest()
