#!/usr/bin/env python3
"""meet.py — operator-convened round-table between named agents.

Every turn is a full governed ask.py call (charter, constitution, router,
budget, per-agent day-log capture). This file contains ZERO cognition and
ZERO routing — it is a convener, not a brain, and ask.py stays the single
entry point for cognition. Agents never message each other autonomously:
the operator convenes, watches every turn live, may interject between
rounds (interactive terminal only), and may end the meeting at any time.
The autonomous form of this pattern is Will's, later, behind its own review.

Walls: /etc/treebeard/walls.conf lists agent pairs that may not be convened
together — one pair per line ("clark han"). Data, not code, same registry
pattern as memory_scopes.conf. Missing file means no walls.

Transcript: printed live and saved under the meetings store — the meeting's
own Fourth-Law artifact. Each turn is ALSO captured in the speaking agent's
day log by ask.py, so the record exists per-agent and per-meeting.

Usage:  meet.py AGENT1,AGENT2[,..]  [--rounds N]  "topic"
        echo "topic" | meet.py AGENT1,AGENT2        (piped: no interjections)
Self-test: meet.py --selftest — offline; no router, no network, temp dir only.
"""
import os
import re
import sys
import datetime
import subprocess
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

PY = f"{_TB_BASE}/shared/venv/bin/python"
ASK = f"{_TB_BASE}/treebeard/ask.py"
WALLS_FILE = f"{_TB_ETC}/walls.conf"
MEET_DIR_DEFAULT = f"{_TB_BASE}/shared/memory/meetings"

# Same alphabet ask.py and memory_scopes.conf accept — names become labels
# and file-path parts; registry consistency over local invention.
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

ROUNDS_DEFAULT = 5
ROUNDS_MAX = 15         # clock governance: meetings end by bound, not vibe.
                        # Raised 2026-07-16 from 2/5 to 5/15: operator friction
                        # with the two-round default was cumulative across
                        # sessions. Cap preserved, default made usable.
AGENTS_MAX = 4          # constitution rides every turn; width is spend
SEP = "-" * 60          # ask.py's status/text separator, parsed below

# Selftest seams (module-global override, dream.py/ask.py pattern).
_RUNNER = None          # fake turn-runner injected by selftest
_MEET_DIR_OVERRIDE = None


def _meet_dir():
    return _MEET_DIR_OVERRIDE or os.environ.get(
        "TREEBEARD_MEET_DIR", MEET_DIR_DEFAULT)


def read_walls(path=None):
    """Return dict {frozenset(pair): set(exception_contexts)}. Absent or
    garbled file -> empty dict (no walls). Tolerant parse, same posture as
    _read_scopes in the memory layer.

    Line format:
        name name                    absolute wall (no exceptions)
        name name except-CTX         wall except in context CTX
        name name except-CTX,CTX2    wall except in either of those contexts

    A bare pair yields an empty exception set (absolute wall). Any token
    after the pair is parsed as "except-<comma-separated-contexts>"; other
    tokens or malformed except-tokens cause the line to be treated as an
    absolute wall (fail-closed: an unparseable exception must not weaken
    the wall).
    """
    walls = {}
    try:
        with open(path or WALLS_FILE, encoding="ascii") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                a, b = parts[0], parts[1]
                if not (_AGENT_RE.match(a) and _AGENT_RE.match(b)):
                    continue
                pair = frozenset((a, b))
                # Default: absolute wall. Any except- token parsed after.
                # Fail-closed: unparseable except-token -> absolute wall
                # (the wall must not weaken silently on bad config).
                exceptions = set()
                for extra in parts[2:]:
                    if extra.startswith("except-"):
                        payload = extra[len("except-"):]
                        # Accept simple, non-empty comma-separated context
                        # names; reject anything with characters outside the
                        # agent-name alphabet to keep this locked down.
                        ctxs = [c.strip() for c in payload.split(",") if c.strip()]
                        if ctxs and all(_AGENT_RE.match(c) for c in ctxs):
                            exceptions.update(ctxs)
                        else:
                            # Malformed exception spec -> treat as absolute wall
                            exceptions = set()
                            break
                # If the same pair appears on multiple lines, the union of
                # exception sets applies. Absolute wall (empty set on any
                # line) wins: an absolute line means the wall is enforced
                # everywhere regardless of other lines' exceptions.
                if pair in walls:
                    prior = walls[pair]
                    if not prior or not exceptions:
                        walls[pair] = set()  # any absolute wins
                    else:
                        walls[pair] = prior | exceptions
                else:
                    walls[pair] = exceptions
    except OSError:
        pass
    return walls


def wall_hit(agents, walls, context=None):
    """First convened pair that a wall forbids, else None.

    `context` names the invocation surface. If the pair has an exception
    set that contains context, the wall is lifted for this call. Default
    context=None means no exceptions apply (safe fallback: any caller not
    passing context gets the strictest interpretation).

    Accepts either the legacy set-of-frozensets shape or the current
    dict-with-exceptions shape, so this function is safe for callers that
    have not been updated to the dict format.
    """
    for i, a in enumerate(agents):
        for b in agents[i + 1:]:
            pair = frozenset((a, b))
            if isinstance(walls, dict):
                if pair not in walls:
                    continue
                exceptions = walls[pair]
                if context is not None and context in exceptions:
                    continue  # wall lifted for this context
                return (a, b)
            else:
                # Legacy shape: set of frozensets, no exceptions.
                if pair in walls:
                    return (a, b)
    return None


_MEET_CHARTER_DIR_OVERRIDE = None   # selftest seam, _MEET_DIR pattern


def _charter_dir():
    return _MEET_CHARTER_DIR_OVERRIDE or os.environ.get(
        "TREEBEARD_ETC", "/etc/treebeard")


def parse_agents(spec):
    """'treebeard,clark' -> validated list. Exits on bad name, dupe, or
    width outside 2..AGENTS_MAX — a bad name here becomes a bad label and
    a bad ask.py flag downstream, so it dies at the door."""
    agents = [a.strip().lower() for a in spec.split(",") if a.strip()]
    if not 2 <= len(agents) <= AGENTS_MAX:
        sys.exit(f"meet.py: need 2..{AGENTS_MAX} agents, got {len(agents)}.")
    if len(set(agents)) != len(agents):
        sys.exit("meet.py: duplicate agent in list.")
    for a in agents:
        if not _AGENT_RE.match(a):
            sys.exit(f"meet.py: invalid agent name {a!r}.")
    # 2026-08-31: a name that PARSES is not necessarily an agent that
    # EXISTS. `meet treebeard,unknown` convened a phantom participant that
    # took real turns, received real messages, and landed in both real
    # meeting records. A single ask.py turn may run unchartered (it is told
    # so, and the constitution still binds it); a MEETING may not, because
    # the cost lands in other agents' durable memory. Existence here means a
    # charter file -- the same file ask.py resolves identity from.
    missing = [a for a in agents
               if not os.path.isfile(os.path.join(_charter_dir(),
                                                  f"{a}.charter.txt"))]
    if missing:
        sys.exit("meet.py: no such agent: %s (no charter at %s). A meeting "
                 "will not convene an agent that does not exist."
                 % (", ".join(missing), _charter_dir()))
    return agents


# CONV fix (2026-08-16): bound the transcript injected per turn. Full
# history had been injected unboundedly; growth across rounds is spend and,
# past model context, silent loss from the wrong end. Newest turns are kept,
# oldest omitted first, and the speaker's own most recent prior turn is
# always preserved so agents can revise their positions across rounds.
TRANSCRIPT_BUDGET_DEFAULT = 24000   # chars, roughly 6k tokens


def _transcript_budget():
    try:
        v = int(os.environ.get("TREEBEARD_MEET_TRANSCRIPT_CHARS", ""))
        return v if v > 0 else TRANSCRIPT_BUDGET_DEFAULT
    except ValueError:
        return TRANSCRIPT_BUDGET_DEFAULT


def _transcript_block(transcript, agent, budget):
    """Render transcript lines within a char budget. Returns (body, omitted).
    Keeps newest turns, drops oldest first; never drops the newest turn;
    rescues the speaker's own most recent prior turn if it would be cut.
    Omission is announced in-band so no speaker mistakes a truncated
    transcript for the whole meeting."""
    if not transcript:
        return "(no turns yet \u2014 you speak first)", 0
    lines = [f"[{who}]: {text}" for who, text in transcript]
    total = sum(len(l) + 1 for l in lines)
    if total <= budget:
        return "\n".join(lines), 0
    kept = []
    used = 0
    for i in range(len(lines) - 1, -1, -1):
        cost = len(lines[i]) + 1
        if kept and used + cost > budget:
            break
        kept.insert(0, i)
        used += cost
    omitted = kept[0]
    if omitted == 0:
        return "\n".join(lines), 0
    me = agent.upper()
    rescue = None
    for i in range(omitted - 1, -1, -1):
        if transcript[i][0] == me:
            rescue = i
            break
    parts = [f"(\u2026 {omitted} earlier turn(s) omitted to fit context; "
             f"the operator's topic above still governs)"]
    if rescue is not None:
        parts.append(f"(your own earlier turn, kept) {lines[rescue]}")
    parts.extend(lines[i] for i in kept)
    return "\n".join(parts), omitted


_KB_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")
_VOWELS = set("aeiouy")


def is_substantive(s):
    """True when operator input is real text, False for blank or mashing.

    Governs meeting length (F33): a substantive interjection extends the
    meeting, blank enter and keyboard-mashing do not. FAIL-OPEN by design --
    anything ambiguous counts as substantive, because ending a meeting the
    operator wanted to continue is unrecoverable while an extra round is
    one bounded call.
    """
    t = (s or "").strip()
    if not t:
        return False
    low = t.lower()
    if len(low.split()) > 1:
        return True
    if len(low) > 1 and len(set(low)) == 1:
        return False
    if not any(c.isalnum() for c in low):
        return False
    word = "".join(c for c in low if "a" <= c <= "z")
    if not word:
        return True
    for row in _KB_ROWS:
        if len(word) >= 4 and (word in row or word in row[::-1]):
            return False
    v = sum(1 for c in word if c in _VOWELS)
    if len(word) >= 4 and v == 0:
        return False
    if len(word) >= 8 and v / len(word) < 0.15:
        return False
    return True


def _sms_mod():
    sys.path.insert(0, f"{_TB_BASE}/shared/lib")
    import sms  # noqa: PLC0415
    return sms


def sms_present(agent, turn_id):
    """Messages addressed to THIS agent, for THIS turn. Fail-soft."""
    try:
        msgs, more = _sms_mod().present(agent, turn_id)
        return msgs, more
    except Exception as e:
        print(f"WARN: sms unavailable for {agent} ({e}); no messages",
              file=sys.stderr)
        return [], 0


def sms_deliver(turn_id):
    try:
        _sms_mod().deliver(turn_id)
    except Exception as e:
        print(f"WARN: sms deliver failed ({e}); messages will re-present",
              file=sys.stderr)


def sms_send_out(agent, body):
    """Send the turn's outbound message, if the reply carried one.
    Parsing and policy both live in sms.py; meet only routes."""
    import re as _re
    if body.count("=== SMS OUT ===") != 1:
        return "sms-out:none"
    tail = body[body.index("=== SMS OUT ==="):].strip()
    m = _re.match(
        r"^=== SMS OUT ===[ \t]*\n"
        r"TO:[ \t]*([A-Za-z][A-Za-z0-9_-]{0,31})[ \t]*\n"
        r"BODY:[ \t]*(.+?)[ \t]*\n?"
        r"=== END SMS ===[ \t]*$", tail, _re.S)
    if not m:
        return "sms-out:malformed"
    try:
        mid = _sms_mod().send(agent, m.group(1).lower(), m.group(2).strip())
        return f"sms-out:ok#{mid}"
    except Exception as e:
        return f"sms-out:refused({e})"


def _needs_operator(text):
    """F35: a BARE [OPERATOR] line means the agent is stuck and wants the
    operator. Same bare-line rule as [LEAVE]: mid-sentence does not fire."""
    return any(ln.strip().upper() == "[OPERATOR]"
               for ln in (text or "").splitlines())


def build_prompt(topic, transcript, agent, agents, rnd, rounds):
    """The turn prompt: convening frame + bounded transcript + speak-as-one
    rule. The only 'prompt engineering' in the file, kept declarative.
    CONV fix (2026-08-16): the frame had imposed a 150-word cap and a
    mandatory three-way close; agents converged on identical skeleton
    memos regardless of charter (2026-08-16T172444Z artifact, where an
    explicit operator 'do not all give the same answer' lost to the
    frame). Cap and mandate removed; conversation and charter identity
    stated instead. The ALPHA precedence block is preserved verbatim."""
    names = ", ".join(a.upper() for a in agents)
    if isinstance(transcript, list) and transcript and isinstance(
            transcript[0], dict):
        body = "\n\n".join(
            f"--- from {m['sender'].upper()} at {m['ts_utc']} ---\n{m['body']}"
            for m in transcript) or "(nothing addressed to you yet)"
    else:
        body = "(nothing addressed to you yet)"
    return (
        f"OPERATOR-CONVENED MEETING \u2014 round {rnd} of {rounds}.\n"
        f"Participants: {names}. You are {agent.upper()}. The other "
        f"participants are real agents in this stable. F35: you do NOT see "
        f"their turns. You see only what was SENT TO YOU. To reach another "
        f"participant you must message them; anything you merely say here "
        f"is heard by the operator alone.\n"
        f"Operator's topic: {topic}\n\n"
        f"Messages addressed to you:\n{body}\n\n"
        f"To send: end your reply with exactly one block — a line "
        f"'=== SMS OUT ===', then 'TO: <participant>', then 'BODY: <text>', "
        f"then '=== END SMS ==='. One per turn. It arrives on their NEXT "
        f"turn, never live. If you need the OPERATOR — you are blocked, "
        f"the topic is ambiguous, or a decision is theirs to make — put "
        f"[OPERATOR] on its own line and the meeting will pause for them.\n\n"
        f"Your turn. Speak as {agent.upper()} only \u2014 never write another "
        f"participant's lines. This is a working conversation among the "
        f"participants, not a round of status reports: engage directly with "
        f"what has been said \u2014 build on it, question it, challenge it, "
        f"concede, or revise your own earlier position when the evidence "
        f"warrants. Address participants by name when responding to them. "
        f"Speak in your own voice, from your charter and your active "
        f"mission; do not restate the question, the governance, or prior "
        f"speakers' points except where you are actually responding to "
        f"them. There is no required format \u2014 structure your contribution "
        f"only when its substance needs structure. Keep it proportionate: "
        f"say what this turn needs and stop; a few sentences is often "
        f"enough. You exist only in invoked turns \u2014 promise no work "
        f"between turns or after the meeting; deliver what you can within "
        f"this turn.\n\n"
        f"OPERATOR PRECEDENCE (Redwin THIRD LAW \u2014 the operator retains final "
        f"authority over all agent actions). Where the operator's topic above "
        f"gives an explicit instruction governing this turn, that instruction "
        f"controls what you say and how you close, and overrides the closing "
        f"options below. Final authority governs permitted actions; it does "
        f"not compel prohibited ones \u2014 you may decline an operator instruction "
        f"ONLY by naming the specific Redwin law or safety constraint that "
        f"complying would violate. Absent such a conflict, do not substitute "
        f"your own preferred sequencing, priorities, or process for the "
        f"operator's decision. Disagreement is not refusal: state honestly any "
        f"risk, error, or contrary evidence you see (FIRST LAW \u2014 do not soften "
        f"truth to manage the operator's mood).\n\n"
        f"Unless the operator's instruction directs otherwise, close however "
        f"your contribution naturally ends. Useful closes include: a question "
        f"to a named participant; a concrete recommendation to the operator; "
        f"or comply and report \u2014 state what you did, found, or conclude, "
        f"with no question and no recommendation. None of these is mandatory "
        f"on any turn; comply and report is always a valid close, and is the "
        f"correct one when the operator has asked you not to recommend or "
        f"not to direct sequencing."
    )


def _meeting_env():
    """Environment for the ask.py subprocess of a meeting turn.

    2026-08-31: marks the turn as a MEETING turn so ask.py suppresses its
    "no inter-agent channel exists" narration stamp -- in a meeting that
    claim is false, SMS is live. Copies the environment; never mutates
    this process, so nothing outside the spawned turn is affected."""
    env = dict(os.environ)
    env["TREEBEARD_MEETING"] = "1"
    return env


# FC04 ruling C3(a): the turn whose receipts await acknowledgement. Set by
# run_turn, consumed by _ack_after_render() ONLY after the turn's text --
# reply, receipts, notices, actionable lines -- is in the transcript and
# printed. Cleared at the start of every turn, so a turn whose rendering
# failed never has its receipts acknowledged by a later turn.
_PENDING_ACK = None


def render_contract(r):
    """Operator presentation of a contract-v1 turn: the reply, then the
    BOX-09 receipts, truth notices and actionable lines it carries (the
    same blocks ask.py's human mode prints). Diagnostics are never in the
    object, so never here."""
    parts = [r.reply]
    if r.receipts:
        import dispatch  # noqa: PLC0415
        parts.append(dispatch.render_receipts(r.receipts))
    parts.extend(r.notices)
    if r.actionable:
        parts.append("\n".join(r.actionable))
    return ("\n" + SEP + "\n").join(p for p in parts if p)


def _ack_after_render():
    """Acknowledge the last turn's receipts as presented to the operator in
    meet -- called after transcript.append() and print(). A failed flush
    (closed terminal, broken pipe) means not presented: no acknowledgement,
    the receipts re-present (at-least-once, BOX-09)."""
    global _PENDING_ACK
    r, _PENDING_ACK = _PENDING_ACK, None
    if r is None:
        return []
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        return []
    return r.acknowledge("operator:meet")


def run_turn(agent, prompt):
    """One governed turn via ask.py subprocess. Fails open like the memory
    layer: a dead turn is recorded in the transcript, never kills the
    meeting. Returns (status, text)."""
    global _PENDING_ACK
    _PENDING_ACK = None
    if _RUNNER:
        return _RUNNER(agent, prompt)
    # FC04 (2026-10-01): bound to ask.py's machine contract (--json, v1),
    # never to its human output. Status is "" on success: the telemetry
    # status line stays off the operator surface (regfix2). Any contract
    # violation, ok=false or empty reply is shown as a failed turn.
    try:
        if f"{_TB_BASE}/shared/lib" not in sys.path:
            sys.path.insert(0, f"{_TB_BASE}/shared/lib")
        import ask_contract  # noqa: PLC0415
        r = ask_contract.run([PY, ASK, "--agent", agent, prompt],
                             env=_meeting_env(), timeout=300)
        if not r.ok:
            tail = (r.stderr or "").strip()[-120:]
            return "", (f"(turn failed rc={r.returncode}: {r.error[:160]}"
                        + (f" {tail}" if tail and r.returncode else "")
                        + ")")
        text = render_contract(r)
        _PENDING_ACK = r
        return "", text
    except Exception as e:  # noqa: BLE001
        _PENDING_ACK = None
        return "", f"(turn failed: {type(e).__name__})"


def save_transcript(agents, topic, transcript):
    """Write the meeting artifact. Store dir first, ~/meetings fallback —
    the record survives even when the shared store is unwritable."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H%M%SZ")
    fname = f"{stamp}-{'-'.join(agents)}.txt"
    for d in (_meet_dir(), os.path.expanduser("~/meetings")):
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, fname)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"MEETING {stamp}\nparticipants: {', '.join(agents)}"
                         f"\ntopic: {topic}\n\n")
                for who, text in transcript:
                    fh.write(f"[{who}]:\n{text}\n\n")
            return path
        except OSError:
            continue
    return None


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit('meet.py: usage: meet.py A,B[,..] [--rounds N] "topic"')
    agents = parse_agents(args[0])
    args = args[1:]
    rounds = ROUNDS_DEFAULT
    if len(args) >= 2 and args[0] == "--rounds":
        try:
            requested = int(args[1])
        except ValueError:
            sys.exit("meet.py: --rounds takes an integer.")
        rounds = max(1, min(ROUNDS_MAX, requested))
        if requested != rounds:
            # F30: silent clamp was a defect; announce it before the meeting
            # starts so the operator knows what will actually run.
            print(f"NOTE: --rounds {requested} clamped to {rounds} "
                  f"(min 1, max {ROUNDS_MAX}).", file=sys.stderr)
        args = args[2:]
    topic = " ".join(args).strip() or (
        sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not topic:
        sys.exit("meet.py: no topic given.")

    hit = wall_hit(agents, read_walls(), context="meet")
    if hit:
        sys.exit(f"meet.py: wall — {hit[0]} and {hit[1]} may not be "
                 "convened together (walls.conf; no except-meet).")

    interactive = sys.stdin.isatty()
    transcript = []
    print(f"MEETING: {', '.join(a.upper() for a in agents)} \u2014 "
          f"{rounds} round(s). Topic: {topic}")
    ended_early = False
    interrupted = False
    # F32: active roster shrinks as agents emit [LEAVE] on their own line.
    # Their [LEAVE] turn still lands in the transcript (it's a real utterance
    # with a governed cost); they simply aren't prompted again. When active
    # is empty, meeting ends (all_left flag distinguishes from operator 'q').
    active = list(agents)
    all_left = False
    need_operator = False   # F35: set when an agent asks for you
    # F33: `rounds` is the CURRENT allowance, not a fixed ceiling. A
    # substantive operator interjection extends it (see is_substantive);
    # blank enter and mashing do not. An unattended run never extends.
    allowance = rounds
    try:
      rnd = 0
      while rnd < allowance:
        rnd += 1
        if ended_early or all_left:
            break
        for agent in list(active):  # snapshot: safe to mutate `active` mid-round
            if agent not in active:
                # An earlier speaker's [LEAVE] can only drop themselves, but
                # this defensive check keeps the loop safe if that ever changes.
                continue
            # F29: operator gates EACH turn, not each round. The prompt
            # renders BEFORE the agent fires. Empty enter fires the turn.
            # Any typed text is inserted into the transcript as an operator
            # line and THEN the turn fires (so the agent sees it). "q" ends
            # the meeting without firing the pending turn.
            # F35: the gate no longer fires every turn. Agents run and
            # message each other unattended; the operator is pulled in only
            # when an agent put [OPERATOR] on its own line, or before the
            # very first turn so the operator can steer at the outset.
            if interactive and (need_operator or rnd == 1):
                # F31: flush stdout so any prior agent output lands on
                # screen before the input prompt renders. sys.stdout is
                # line-buffered on a TTY but explicit flush is cheap and
                # removes the interleave race the operator saw at F31.
                sys.stdout.flush()
                sys.stderr.flush()
                try:
                    say = input(f"\n\u2014 next: {agent.upper()} round "
                                f"{rnd}/{allowance} [enter=fire, "
                                "text=interject-then-fire, q=end]: ").strip()
                except EOFError:
                    say = "q"
                if say.lower() == "q":
                    ended_early = True
                    break
                need_operator = False
                if say:
                    transcript.append(("OPERATOR", say))
                    print(f"[OPERATOR]: {say}")
                    if is_substantive(say):
                        allowance += 1
                        print(f"-- meeting extended to {allowance} rounds "
                              f"(operator engaged).", file=sys.stderr)
            print(f"\n=== {agent.upper()} \u2014 round {rnd} ===")
            sys.stdout.flush()
            _body, _omitted = _transcript_block(transcript, agent,
                                                _transcript_budget())
            print(f"meet-meta: agent={agent} round={rnd}/{allowance} "
                  f"order={active.index(agent)+1}/{len(active)} "
                  f"transcript_chars={len(_body)} turns={len(transcript)} "
                  f"omitted_turns={_omitted}", file=sys.stderr)
            _tid = (f"meet-{agent}-"
                    f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}")
            _msgs, _more = sms_present(agent, _tid)
            prompt = build_prompt(topic, _msgs, agent, list(active),
                                  rnd, allowance)
            status, text = run_turn(agent, prompt)
            transcript.append((agent.upper(), text))
            # NOTE: meet does NOT send the outbound block. run_turn ->
            # ask.py already parses and sends it; sending here too delivered
            # every message twice (found live 2026-08-30, recipient in=2).
            sms_deliver(_tid)
            print(f"meet-sms: agent={agent} in={len(_msgs)} more={_more}",
                  file=sys.stderr)
            if _needs_operator(text):
                need_operator = True
                print(f"\n>> {agent.upper()} IS ASKING FOR YOU. "
                      f"The meeting will pause at the next turn.\n")
            if status:
                print(status)
            print(text)
            # C3(a): receipts were rendered (transcript + console) above;
            # only now are they acknowledged.
            _ack_after_render()
            # F32: [LEAVE] on its own line drops the speaker from the roster.
            # Case-insensitive; whitespace tolerated around the token. Only a
            # BARE line matches - "[LEAVE]" mid-sentence or discussed as a
            # topic doesn't fire the drop.
            if any(ln.strip().upper() == "[LEAVE]"
                   for ln in (text or "").splitlines()):
                active.remove(agent)
                print(f"-- {agent.upper()} left the meeting.")
                if not active:
                    all_left = True
                    break
    except KeyboardInterrupt:
        # Ctrl+C during a turn or an input(). The transcript accumulated
        # so far is the Fourth-Law artifact of the meeting; save it
        # before exiting rather than losing what the operator watched.
        interrupted = True
        print("\n\u2014 meeting interrupted (SIGINT).", file=sys.stderr)
    finally:
        path = save_transcript(agents, topic, transcript)
        # F34: queue the meeting for each participant to consolidate into
        # ITS OWN long-term memory on its next turn. meet.py deliberately
        # does NOT write those stores: longterm.py's rule is "writes are
        # ALWAYS own-only -- oversight is read, never reach." This is a
        # queue, not a write. Fail-soft: the transcript file above is the
        # meeting's artifact and is already saved; a queue failure warns.
        try:
            sys.path.insert(0, f"{_TB_BASE}/shared/lib")
            import meetmem  # noqa: PLC0415
            if transcript:
                # 2026-08-31: capture the DECISION separately from the
                # transcript. After a contested 5-round meeting both
                # participants later reported the conclusion inverted:
                # recall returned the argument, which is thousands of
                # words, rather than the outcome, which is one line. The
                # operator states it; a blank answer falls back to the
                # final turn. Non-interactive runs use the fallback
                # silently. Whichever it was is recorded in provenance.
                _dec = ""
                if interactive and not interrupted:
                    try:
                        sys.stdout.flush()
                        _dec = input("\n\u2014 in one line, what was "
                                     "decided? [enter = use the final "
                                     "turn]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        _dec = ""
                if not _dec and transcript:
                    _dec = str(transcript[-1][1] or "").strip()[:600]
                _ids = meetmem.enqueue(agents, topic, transcript,
                                       decision=_dec or None)
                print(f"-- meeting queued for {len(_ids)} participant(s) "
                      f"to consolidate.", file=sys.stderr)
        except Exception as _me:
            print(f"WARN: meeting not queued for memory ({_me}); "
                  f"transcript file is unaffected.", file=sys.stderr)
        if interrupted:
            tag = " (interrupted)"
        elif ended_early:
            tag = " (operator)"
        elif all_left:
            tag = " (all agents left)"
        else:
            tag = ""
        print(f"\nmeeting ended{tag}. "
              f"transcript: {path or '(save failed)'}")


def selftest():
    """Offline checks: parsing, walls, prompt frame, reply parsing, the
    convening loop against a fake runner, transcript artifact. No router,
    no network, writes only inside a temp dir."""
    import tempfile, shutil
    global _RUNNER, _MEET_DIR_OVERRIDE
    failures = 0

    def check(label, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures += 1

    tmp = tempfile.mkdtemp(prefix="meet_selftest_")
    try:
        # 1-2. name alphabet holds at the door
        check("valid name accepted", bool(_AGENT_RE.match("clark")))
        check("traversal rejected", not _AGENT_RE.match("../etc"))
        # 2026-08-31: existence, not just name shape.
        global _MEET_CHARTER_DIR_OVERRIDE
        _MEET_CHARTER_DIR_OVERRIDE = tmp
        for _n in ("alpha", "beta"):
            with open(os.path.join(tmp, f"{_n}.charter.txt"), "w") as _fh:
                _fh.write("charter\n")
        check("existing agents parse", parse_agents("alpha,beta") ==
              ["alpha", "beta"])
        _rc = 0
        try:
            parse_agents("alpha,ghostagent")
        except SystemExit as _e:
            _rc = 1
            check("unknown agent names the offender",
                  "ghostagent" in str(_e))
        check("meeting refuses an agent with no charter", _rc == 1)
        _env = _meeting_env()
        check("meeting turns are marked for ask.py",
              _env.get("TREEBEARD_MEETING") == "1")
        check("marking a meeting turn does not mutate this process",
              "TREEBEARD_MEETING" not in os.environ)
        _MEET_CHARTER_DIR_OVERRIDE = None

        # 3-6. walls: parse, block, tolerate absence, ignore garbage
        wpath = os.path.join(tmp, "walls.conf")
        with open(wpath, "w") as fh:
            fh.write("# wall\nclark han\nnot-a-line\n")
        walls = read_walls(wpath)
        check("wall parsed", frozenset(("clark", "han")) in walls)
        check("garbage line ignored", len(walls) == 1)
        check("bare wall has empty exception set",
              walls.get(frozenset(("clark", "han"))) == set())
        check("wall blocks pair (no context)",
              wall_hit(["treebeard", "han", "clark"], walls) is not None)
        check("wall blocks pair even when context passed but no exception",
              wall_hit(["treebeard", "han", "clark"], walls,
                       context="meet") is not None)
        check("missing walls file -> empty dict",
              read_walls(os.path.join(tmp, "nope")) == {})

        # F33: except-meet lifts the wall only for the meet context.
        wpath2 = os.path.join(tmp, "walls_exc.conf")
        with open(wpath2, "w") as fh:
            fh.write("clark han except-meet\n")
        walls2 = read_walls(wpath2)
        check("except-meet parsed into exception set",
              walls2.get(frozenset(("clark", "han"))) == {"meet"})
        check("except-meet: no-context call still blocked",
              wall_hit(["clark", "han"], walls2) is not None)
        check("except-meet: meet context lifts wall",
              wall_hit(["clark", "han"], walls2, context="meet") is None)
        check("except-meet: other context still blocked",
              wall_hit(["clark", "han"], walls2, context="other") is not None)

        # F33: fail-closed on malformed exception spec.
        wpath3 = os.path.join(tmp, "walls_bad.conf")
        with open(wpath3, "w") as fh:
            fh.write("clark han except-\n")  # empty payload
        walls3 = read_walls(wpath3)
        check("malformed except- treated as absolute wall",
              walls3.get(frozenset(("clark", "han"))) == set())
        check("malformed except: wall still enforced under meet context",
              wall_hit(["clark", "han"], walls3, context="meet") is not None)

        # 7-8. unwalled pair passes; prompt frame is speak-as-one
        check("unwalled pair passes",
              wall_hit(["treebeard", "clark"], walls) is None)
        check("unwalled pair passes under meet context",
              wall_hit(["treebeard", "clark"], walls, context="meet") is None)
        p = build_prompt("t", [{"sender": "clark",
                                "ts_utc": "2026-01-01T00:00:00Z",
                                "body": "hi"}], "treebeard",
                         ["treebeard", "clark"], 1, 2)
        check("prompt names speaker and forbids ventriloquism",
              "You are TREEBEARD" in p and "never write another" in p)
        check("F35 message addressed to this agent is shown",
              "from CLARK" in p and "hi" in p)
        check("F35 frame teaches the send block and operator escape",
              "=== SMS OUT ===" in p and "[OPERATOR]" in p)

        # --- ALPHA BUG regression: operator-command precedence in the frame.
        # The defect was an unconditional closer ("end with either a question
        # or a recommendation") sitting AFTER the operator's topic, leaving no
        # compliant way to obey an operator who forbids recommendations. These
        # assertions bind the fix: precedence stated, closer conditional,
        # comply/report available, disagreement preserved, and the closing
        # options explicitly subordinate to a live operator instruction.
        pa = build_prompt("Do not recommend sequencing.", [], "clark",
                          ["clark", "han"], 1, 2)
        check("A1 precedence clause present and cites THIRD LAW",
              "OPERATOR PRECEDENCE" in pa and "THIRD LAW" in pa
              and "final authority" in pa)
        check("A2 closer is conditional, not unconditional",
              "Unless the operator's instruction directs otherwise" in pa
              and "end with either a question" not in pa)
        check("A3 comply/report is offered as an always-valid third close",
              "comply and report" in pa
              and "no question and no recommendation" in pa
              and "always a valid close" in pa)
        check("A4 disagreement/objection explicitly preserved",
              "Disagreement is not refusal" in pa
              and "contrary evidence" in pa and "FIRST LAW" in pa)
        check("A5 declining requires naming a specific law/safety conflict",
              "ONLY by naming the specific Redwin law or safety constraint"
              in pa)
        check("A6 recommendation option survives (not suppressed)",
              "recommendation to the operator" in pa)
        check("A7 closing options are subordinate to operator instruction",
              pa.index("overrides the closing "
                       "options below") < pa.index("Useful closes include"))
        check("A8 operator topic still precedes the closing options",
              pa.index("Operator's topic:") < pa.index("Useful closes include")
              and pa.index("Operator's topic:")
              < pa.index("OPERATOR PRECEDENCE"))
        check("A9 no per-agent wiring in the frame",
              build_prompt("t", [], "han", ["han", "clark"], 1, 2)
              .replace("HAN", "X").replace("CLARK", "X")
              == build_prompt("t", [], "clark", ["clark", "han"], 1, 2)
              .replace("CLARK", "X").replace("HAN", "X"))

        # --- CONV fix regression (2026-08-16): conversational frame and
        # bounded transcript. The defect was structural convergence: a
        # 150-word cap plus a mandatory templated close produced identical
        # skeleton memos from every agent (2026-08-16T172444Z artifact,
        # where "do not all give the same answer" lost to the frame).
        pc = build_prompt("t", [("CLARK", "hi")], "treebeard",
                          ["treebeard", "clark"], 1, 2)
        check("C1 word cap removed", "150 words" not in pc)
        check("C2 close mandate removed, options preserved",
              "close in ONE of" not in pc and "Useful closes include" in pc
              and "None of these is mandatory" in pc)
        check("C3 conversational default stated",
              "working conversation" in pc and "no required format" in pc)
        check("C4 charter voice and revision invited",
              "your charter" in pc and "your own voice" in pc
              and "revise your own earlier position" in pc)
        check("C5 roster present with speaker identified",
              "Participants: TREEBEARD, CLARK" in pc
              and "You are TREEBEARD" in pc)
        turns = [("CLARK", f"c{i:03d} " + "y" * 200) for i in range(30)]
        turns[3] = ("TREEBEARD", "own-early-turn " + "z" * 200)
        tbody, tom = _transcript_block(turns, "treebeard", 2000)
        check("T1 over budget omits oldest, keeps newest",
              tom > 0 and "c029" in tbody and "c000" not in tbody)
        check("T2 omission announced in-band",
              "omitted to fit context" in tbody)
        check("T3 speaker's own earlier turn rescued",
              "own-early-turn" in tbody)
        check("T4 labels intact after truncation", "[CLARK]:" in tbody)
        tbody2, tom2 = _transcript_block(turns[:2], "treebeard", 100000)
        check("T5 under budget: full transcript, no marker",
              tom2 == 0 and "omitted" not in tbody2 and "c000" in tbody2
              and "c001" in tbody2)
        # F35 (supersedes T6): agents do NOT receive each other's turns.
        # A transcript-shaped argument must not leak into the prompt; only
        # messages addressed to this agent are shown. This is the
        # convergence protection -- the old T6 asserted the mechanism that
        # caused convergence (2026-08-23 retro).
        p_r2 = build_prompt("t", [("CLARK", "round1-evidence")], "han",
                            ["han", "clark"], 2, 3)
        check("F35 another agent's turn does NOT reach this agent",
              "round1-evidence" not in p_r2)
        p_r2m = build_prompt("t", [{"sender": "clark", "ts_utc": "2026-01-01T00:00:00Z", "body": "round1-evidence"}], "han",
                             ["han", "clark"], 2, 3)
        check("F35 but a message TO this agent does",
              "round1-evidence" in p_r2m)
        check("T7 newest turn never dropped",
              "x" * 100 in _transcript_block(
                  [("CLARK", "x" * 9000)], "han", 100)[0])

        # 9-10. FC04: run_turn binds to the --json contract (ask_contract)
        sys.path.insert(0, f"{_TB_BASE}/shared/lib")
        import ask_contract as _ac
        _svr = _ac._RUNNER

        def _mt(out, rc=0):
            _ac._RUNNER = lambda argv, inp, to, env: (rc, out, "")
            try:
                return run_turn("clark", "p")
            finally:
                _ac._RUNNER = _svr
        s, t = _mt(_ac.dumps(_ac.build("clark", True, "answer body", None,
                                       0)))
        check("FC04 --json success -> reply shown, no status telemetry",
              t == "answer body" and s == "")
        _seen = {}
        _ac._RUNNER = lambda argv, inp, to, env: _seen.update(a=argv,
                                                              e=env) or (
            0, _ac.dumps(_ac.build("clark", True, "x", None, 0)), "")
        try:
            run_turn("clark", "p")
        finally:
            _ac._RUNNER = _svr
        check("FC04 meet passes --json and the meeting env",
              "--json" in _seen["a"]
              and _seen["e"].get("TREEBEARD_MEETING") == "1")
        for _lbl, _out, _rc in (
                ("empty reply", _ac.dumps(_ac.build("clark", True, "", None,
                                                    0)), 0),
                ("narration-only old shape",
                 "REPLY\n" + SEP + "\nNARRATION STAMP\n", 0),
                ("malformed JSON", "{oops", 0),
                ("not ok", _ac.dumps(_ac.build("clark", False, "", "boom",
                                               0)), 0)):
            s3, t3 = _mt(_out, _rc)
            check("FC04 %s -> shown as failed turn, never as a reply" % _lbl,
                  t3.startswith("(turn failed") and "NARRATION" not in t3)

        # C3(a): receipts rendered into the turn text BEFORE any
        # acknowledgement; render failure -> never acknowledged.
        _rk = dict((k, None) for k in _ac.RECEIPT_KEYS)
        _rk.update(id=11, operation="serpens.search", state="DONE",
                   exit=0, result="rc=0 found")
        _acks = []
        _sva = _ac._ACK
        _ac._ACK = lambda a, t, i, p: _acks.append((list(i), p)) or list(i)
        try:
            _obj = _ac.build("clark", True, "the reply", None, 0, "t", "th",
                             receipts=[_rk], notices=["[NOTICE-X]"],
                             actionable=["DISPATCH #11 serpens.search -> DONE"])
            s, t = _mt(_ac.dumps(_obj))
            check("C3a receipts, notices, actionable rendered in the text",
                  t.startswith("the reply") and "RECEIPT #11" in t
                  and "[NOTICE-X]" in t and "DISPATCH #11" in t)
            check("C3a run_turn itself never acknowledges", _acks == [])
            _ack_after_render()
            check("C3a acknowledged once, as operator:meet, after render",
                  _acks == [([11], "operator:meet")])
            _ack_after_render()
            check("C3a second _ack_after_render is a no-op",
                  len(_acks) == 1)
            del _acks[:]
            _mt(_ac.dumps(_obj))          # rendered? no: print "raised"
            _rk2 = dict(_rk); _rk2["id"] = 12
            _mt(_ac.dumps(_ac.build("clark", True, "next", None, 0, "t",
                                    "th", receipts=[_rk2])))
            _ack_after_render()
            check("C3a render failure: that turn's receipt is never "
                  "acknowledged by a later turn", _acks == [([12],
                                                             "operator:meet")])
            del _acks[:]
            _mt("{oops")
            _ack_after_render()
            check("C3a failed turn: nothing to acknowledge", _acks == [])
        finally:
            _ac._ACK = _sva
        import inspect as _insp
        _src = _insp.getsource(main)
        _ia = _src.find("transcript.append((agent.upper(), text))")
        _ip = _src.find("print(text)")
        _ik = _src.find("_ack_after_render()")
        check("C3a main(): transcript.append -> print(text) -> ack, in order",
              -1 < _ia < _ip < _ik and _src.count("_ack_after_render()") == 1)

        # 11-13. convening loop against a fake runner
        calls = []
        _RUNNER = lambda a, pr: calls.append(a) or ("[agent=%s]" % a,
                                                    "reply-" + a)
        _MEET_DIR_OVERRIDE = tmp
        transcript = []
        for rnd in range(1, 3):
            for agent in ["treebeard", "clark"]:
                _, tx = run_turn(agent, build_prompt(
                    "t", transcript, agent, ["treebeard", "clark"], rnd, 2))
                transcript.append((agent.upper(), tx))
        check("turns = agents x rounds", len(calls) == 4)
        check("turn order interleaves",
              calls == ["treebeard", "clark", "treebeard", "clark"])
        check("transcript labeled",
              transcript[0] == ("TREEBEARD", "reply-treebeard"))

        # 14-15. artifact written; operator interjection rides the prompt
        path = save_transcript(["treebeard", "clark"], "t", transcript)
        check("transcript artifact written",
              path and os.path.isfile(path) and tmp in path)
        # F35: the operator reaches agents through the gate, not through a
        # shared transcript. What the frame must carry is the ESCAPE.
        p2 = build_prompt("t", [], "clark", ["treebeard", "clark"], 2, 2)
        check("F35 empty inbox states so plainly",
              "nothing addressed to you" in p2)

        # 16. F29: build_prompt receives a mid-round OPERATOR line and shows
        # it to the next speaker BEFORE they speak.
        check("F35 bare [OPERATOR] line requests the operator",
              _needs_operator("I am blocked.\n[OPERATOR]"))
        check("F35 [OPERATOR] mid-sentence does NOT request",
              not _needs_operator("we should ask the [OPERATOR] later"))
        check("F35 no token means keep going",
              not _needs_operator("normal reply"))

        # 17. F30 regression: --rounds clamp must announce, not swallow.
        # Exercise the exact parsing block from main() by replicating its
        # logic against captured stderr. The selftest stays offline (no
        # subprocess); we test the branch's observable effect.
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            requested = 99
            clamped = max(1, min(ROUNDS_MAX, requested))
            if requested != clamped:
                print(f"NOTE: --rounds {requested} clamped to {clamped} "
                      f"(min 1, max {ROUNDS_MAX}).", file=sys.stderr)
        out = buf.getvalue()
        check("clamp announces when reducing",
              "clamped to" in out and str(ROUNDS_MAX) in out)

        # 18. F32: [LEAVE] detection - the bare-line rule.
        # A reply containing "[LEAVE]" on its own line signals departure.
        # A reply mentioning "[LEAVE]" mid-sentence does NOT.
        def _has_leave(text):
            return any(ln.strip().upper() == "[LEAVE]"
                       for ln in (text or "").splitlines())
        check("bare [LEAVE] line detected",
              _has_leave("I have nothing further.\n[LEAVE]"))
        check("bare [LEAVE] with whitespace detected",
              _has_leave("done\n   [LEAVE]   \nend"))
        check("lowercase [leave] detected (case-insensitive)",
              _has_leave("bye\n[leave]"))
        check("inline [LEAVE] mid-sentence does NOT fire",
              not _has_leave("we should discuss [LEAVE] policy"))
        check("no leave token means stay",
              not _has_leave("normal reply text"))
        check("empty text handled",
              not _has_leave("") and not _has_leave(None))

        # 19. F32: raised defaults are what we shipped.
        check("ROUNDS_DEFAULT is 5", ROUNDS_DEFAULT == 5)
        check("ROUNDS_MAX is 15", ROUNDS_MAX == 15)
        # 20. F33: substantive input extends, mashing and blank do not.
        for _txt in ("", "   ", "aaaa", "....", "asdf", "qwer", "sdfgh"):
            check(f"non-substantive: {_txt!r}", not is_substantive(_txt))
        for _txt in ("no", "why", "k", "2", "stop", "ship it",
                     "push back on that premise"):
            check(f"substantive: {_txt!r}", is_substantive(_txt))

        print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
        return 0 if failures == 0 else 1
    finally:
        _RUNNER = None
        _MEET_DIR_OVERRIDE = None
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(selftest())
    main()
