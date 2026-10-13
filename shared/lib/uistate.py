#!/usr/bin/env python3
# uistate.py -- UI-02 Shared State and Icon System. Single source of truth
# mapping backend vocabularies (board.py phase, Meeseeks section/project
# states) to operator-facing UI states. Contract: unknown input NEVER
# becomes a known state (UI-17); Blocked and Failed differ in glyph AND
# label, never color-only (UI-02); attention set is exactly the UI-03
# triad (approval / returned / blocked).
#
# BOARD-TRUTH L (2026-10-01): resolve(card, detail) is THE resolver. Every
# surface -- box.card, box.cards, /board, /task, Currently Working, NEEDS
# YOU, Recently Finished, Sun's digest and Will's wake prompt -- calls it,
# over execution facts from ONE place (opshell.card_detail /
# opshell.details_for). "Waiting" is retired: backlog and approved/queued
# are different states. `closed` is its own state and is never Waiting.
STATES = {
    "backlog":  ("Backlog",           "o",  "st-backlog"),
    "queued":   ("Approved / queued", "+",  "st-queued"),
    "working":  ("Working",           ">",  "st-working"),
    "approval": ("Needs approval",    "!",  "st-approval"),
    "review":   ("Needs review",      "*",  "st-review"),
    "returned": ("Returned",          "?",  "st-returned"),
    "blocked":  ("Blocked",           "#",  "st-blocked"),
    "failed":   ("Failed",            "x",  "st-failed"),
    "done":     ("Done",              "=",  "st-done"),
    "closed":   ("Closed",            "/",  "st-closed"),
    "unknown":  ("Unknown",           "~",  "st-unknown"),
}
# approval = pre-work Gate 1 (AWAITING_APPROVAL); review = post-work
# (IN_REVIEW, incl. meeseeks Gate 2). Both need a human.
ATTENTION = frozenset({"approval", "review", "returned", "blocked"})

_PHASE = {None: "backlog", "ready": "queued",
          "awaiting_approval": "approval", "in_progress": "working",
          "review": "review", "returned": "returned", "blocked": "blocked"}
_SECTION = {"QUEUED": "queued", "TESTING": "working", "REVIEW1": "working",
            "REVIEW2": "working", "REVIEW3": "working", "SPLIT": "working",
            "AWAITING_TREEBEARD": "review", "DONE": "done",
            "BLOCKED": "blocked", "UNREVIEWABLE": "failed"}
_PROJECT = {"ACTIVE": "working", "CLOSED": "done",
            "CLOSED_BLOCKED": "failed", "FACTORY_EXHAUSTED": "failed"}
_CLOSE_OUTCOMES = ("cancelled", "superseded", "deferred")

def get(key):
    return STATES.get(key, STATES["unknown"])

def from_phase(phase, status=None):
    """Board-only mapping (no execution facts). Kept for callers that have
    no card; every card surface uses resolve()."""
    if status == "done":
        return "done"
    if status == "closed":
        return "unknown"
    return _PHASE.get(phase, "unknown")

def from_section(state):
    return _SECTION.get(state, "unknown")

def from_project(state):
    return _PROJECT.get(state, "unknown")

# Execution is terminal-unsuccessful when the PROJECT says so, when a
# section is UNREVIEWABLE (terminal in meeseeks: an artifact-caused contract
# violation, meeseeks.py _TERMINAL_ROOTS), or when a section is BLOCKED for
# a reason naming factory exhaustion. Card 58 holds project ACTIVE with
# section BLOCKED / "FACTORY_EXHAUSTED: stages exhausted at github", so
# project state alone would render a dead card as merely Blocked.
_TERMINAL_REASONS = ("FACTORY_EXHAUSTED", "CLOSED_BLOCKED")


def section_terminal(section_state=None, reason=None):
    if section_state == "UNREVIEWABLE":
        return True
    if section_state == "BLOCKED" and reason:
        up = str(reason).upper()
        return any(t in up for t in _TERMINAL_REASONS)
    return False


def is_terminal_failure(project_state=None, section_state=None, reason=None):
    if from_project(project_state) == "failed":
        return True
    return section_terminal(section_state, reason)


# ONE section rule for every surface: the WORST section represents the
# project (terminal < blocked < at gate < unknown < running < queued <
# done). It matches reflector.project_phase's any-BLOCKED rule; the old
# first-section (box.card) vs last-section (failed_ids) split is gone.
def section_rank(state, reason=None):
    if section_terminal(state, reason):
        return 0
    return {"BLOCKED": 1, "AWAITING_TREEBEARD": 2, "TESTING": 4,
            "REVIEW1": 4, "REVIEW2": 4, "REVIEW3": 4, "SPLIT": 4,
            "QUEUED": 5, "DONE": 6}.get(state, 3)


def worst_section(sections):
    """sections: dicts with sid/state/blocked_reason. None when empty."""
    rows = [s for s in (sections or []) if isinstance(s, dict)]
    if not rows:
        return None
    return min(rows, key=lambda r: (section_rank(r.get("state"),
                                                 r.get("blocked_reason")),
                                    str(r.get("sid") or "")))


def resolve(card, detail=None):
    """THE resolver for a board card's operator state.

    card: the board item. detail: execution facts from opshell (keys
    project_state, state = WORST section state, reason); None/{} when the
    card is unlinked or the ledger cannot be read (degrade detail, never
    invent failure).

      closed       -> failed (outcome failed) | closed (cancelled /
                      superseded / deferred) | unknown (bad outcome)
      done         -> done, UNLESS linked execution disagrees (terminal
                      failure, or a section not DONE): then unknown --
                      a disagreement is never rendered as a success
      open         -> failed when linked execution is terminal; else the
                      board phase (backlog / queued / approval / working /
                      review / returned / blocked)
      anything else-> unknown (UI-17)"""
    card = card or {}
    d = detail or {}
    st = card.get("status")
    terminal = is_terminal_failure(d.get("project_state"), d.get("state"),
                                   d.get("reason"))
    if st == "closed":
        oc = card.get("outcome")
        if oc == "failed":
            return "failed"
        return "closed" if oc in _CLOSE_OUTCOMES else "unknown"
    if st == "done":
        has_exec = bool(d.get("project_state") or d.get("state"))
        unfinished = (has_exec and d.get("state") != "DONE"
                      and d.get("project_state") != "CLOSED")
        return "unknown" if (terminal or unfinished) else "done"
    if st != "open":
        return "unknown"
    if terminal:
        return "failed"
    return _PHASE.get(card.get("phase"), "unknown")


def from_card(phase, status=None, project_state=None, section_state=None,
              reason=None):
    """Compatibility shim over resolve() (pre-L signature)."""
    return resolve({"phase": phase, "status": status},
                   {"project_state": project_state, "state": section_state,
                    "reason": reason})

def is_attention(key):
    return key in ATTENTION

def badge(key, text=True):
    """Canonical markup for one state. Every screen renders state through
    this call -- no page builds its own. Glyph is decorative (aria-hidden)
    because the label carries the meaning for screen readers; the CSS class
    adds color on top of glyph+label, never instead of them (UI-02, UI-20)."""
    label, glyph, css = get(key)
    parts = ['<span class="st %s" role="img" aria-label="%s">' % (css, label),
             '<span class="st-glyph" aria-hidden="true">%s</span>' % glyph]
    if text:
        parts.append('<span class="st-label">%s</span>' % label)
    parts.append('</span>')
    return "".join(parts)


def _selftest():
    for m in (_PHASE, _SECTION, _PROJECT):
        for v in m.values():
            assert v in STATES, v
    assert from_phase("garbage") == "unknown"
    assert from_section("SOMETHING_NEW") == "unknown"
    assert from_project("JULY_STALE") == "unknown"
    # CLOSED is the successful terminal state; the two failure terminals
    # must never collapse into it.
    assert from_project("CLOSED") == "done"
    assert from_project("CLOSED_BLOCKED") == "failed"
    assert from_project("FACTORY_EXHAUSTED") == "failed"
    assert from_phase(None, "done") == "done"
    assert from_phase("returned") == "returned"
    assert get("returned")[0] == "Returned"
    # L: "Waiting" is retired; backlog and approved/queued are distinct.
    assert "waiting" not in STATES
    assert from_phase(None) == "backlog" and from_phase("ready") == "queued"
    assert get("backlog")[0] != get("queued")[0]
    assert from_phase("awaiting_approval") == "approval"
    assert from_phase("review") == "review"
    _canon = ("backlog", "queued", "working", "approval", "review",
              "returned", "blocked", "failed", "done", "closed")
    for _k in _canon:
        assert _k in STATES, _k
    assert len({get(k)[0] for k in _canon}) == len(_canon)
    assert "decision" not in STATES
    C = lambda **k: dict({"status": "open", "phase": None}, **k)
    # closed is NEVER Waiting/backlog, and failed stays failed.
    assert resolve(C(status="closed", outcome="cancelled")) == "closed"
    assert resolve(C(status="closed", outcome="superseded")) == "closed"
    assert resolve(C(status="closed", outcome="deferred")) == "closed"
    assert resolve(C(status="closed", outcome="failed")) == "failed"
    assert resolve(C(status="closed", outcome="bogus")) == "unknown"
    assert resolve(C(status="closed")) == "unknown"
    assert from_phase(None, "closed") == "unknown"
    # done wins only when execution agrees.
    assert resolve(C(status="done")) == "done"
    assert resolve(C(status="done"), {"project_state": "ACTIVE",
                                      "state": "DONE"}) == "done"
    assert resolve(C(status="done"), {"project_state": "CLOSED"}) == "done"
    assert resolve(C(status="done"),
                   {"project_state": "FACTORY_EXHAUSTED"}) == "unknown"
    assert resolve(C(status="done"), {"project_state": "ACTIVE",
                                      "state": "QUEUED"}) == "unknown"
    # Terminal project state -> failed, whatever the phase says.
    assert resolve(C(phase="in_progress"),
                   {"project_state": "FACTORY_EXHAUSTED"}) == "failed"
    assert resolve(C(phase="blocked"),
                   {"project_state": "CLOSED_BLOCKED"}) == "failed"
    # The live card-58 shape: project ACTIVE, section BLOCKED, exhausted.
    assert resolve(C(phase="blocked"), {
        "project_state": "ACTIVE", "state": "BLOCKED",
        "reason": "FACTORY_EXHAUSTED: stages exhausted at github"}) \
        == "failed"
    # M4: UNREVIEWABLE is terminal, never Working.
    assert resolve(C(phase="in_progress"),
                   {"project_state": "ACTIVE", "state": "UNREVIEWABLE"}) \
        == "failed"
    assert section_terminal("UNREVIEWABLE")
    # A recoverable block stays Blocked and stays actionable.
    assert resolve(C(phase="blocked"), {
        "project_state": "ACTIVE", "state": "BLOCKED",
        "reason": "waiting on operator"}) == "blocked"
    assert resolve(C(phase="blocked")) == "blocked"
    assert resolve(C(phase="review")) == "review"
    assert resolve(C(phase="returned")) == "returned"
    assert resolve(C(phase="GARBAGE")) == "unknown"
    assert resolve(C(status="weird")) == "unknown"
    assert resolve(None) == "unknown"
    # One section rule: the worst section, never first/last.
    secs = [{"sid": "s01", "state": "SPLIT"},
            {"sid": "s02", "state": "BLOCKED",
             "blocked_reason": "FACTORY_EXHAUSTED: x"}]
    assert worst_section(secs)["sid"] == "s02"
    assert worst_section(list(reversed(secs)))["sid"] == "s02"
    assert worst_section([{"sid": "s01", "state": "BLOCKED",
                           "blocked_reason": "FACTORY_EXHAUSTED"},
                          {"sid": "s02", "state": "QUEUED"}])["sid"] == "s01"
    assert worst_section([]) is None
    # compat shim agrees with resolve.
    assert from_card("blocked", "open", "ACTIVE", "BLOCKED",
                     "FACTORY_EXHAUSTED") == "failed"
    assert from_card(None, "done", project_state="FACTORY_EXHAUSTED") \
        == "unknown"
    # Failed and Closed are terminal, never attention.
    assert not is_attention("failed") and not is_attention("closed")
    assert is_attention("returned") and is_attention("approval")
    assert is_attention("blocked") and is_attention("review")
    b, f = STATES["blocked"], STATES["failed"]
    assert b[0] != f[0] and b[1] != f[1]
    glyphs = [v[1] for v in STATES.values()]
    assert len(glyphs) == len(set(glyphs)), "glyph collision"
    labels = [v[0] for v in STATES.values()]
    assert len(labels) == len(set(labels)), "label collision"
    for label, glyph, css in STATES.values():
        assert label and glyph and css.startswith("st-")
    assert ATTENTION == {"approval", "review", "returned", "blocked"}
    assert not is_attention("working") and is_attention("blocked")
    for key in STATES:
        b = badge(key)
        assert 'aria-label="%s"' % STATES[key][0] in b, key
        assert 'aria-hidden="true"' in b, key
        assert STATES[key][2] in b, key
        assert badge(key) == badge(key)
    assert "st-label" not in badge("blocked", text=False)
    assert 'aria-label="Blocked"' in badge("blocked", text=False)
    assert badge("nonsense") == badge("unknown")
    assert badge("blocked") != badge("failed")
    print("uistate selftest: ALL PASS (%d states, badge render verified)" % len(STATES))

if __name__ == "__main__":
    # Harness argv convention: "--selftest" accepted (and ignored) so
    # the registry entry matches the store suites; bare run identical.
    # Under -O asserts vanish, so refuse loudly, never false-green.
    if not __debug__:
        print("uistate: selftest requires asserts (no -O)")
        raise SystemExit(2)
    _selftest()
