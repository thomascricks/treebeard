#!/usr/bin/env python3
"""test_board_lifecycle.py -- BOARD-TRUTH L (Tex rulings L-R1..L-R9, Moe
flags 1-4): the ONE transition, the lock, the approval gate, exec_ref
carrying no authority, the closed status, the sticky status-level failure,
the reconcile sidecar, and the ONE resolver proven equal on every surface.

Every board here is a fixture under TASKBOARD_PATH in a temp directory and
every ledger is a fixture under MEESEEKS_ROOT (R3: never a production card).
Like test_board.py, this file names no absolute install path: Sun is found
under TREEBEARD_BASE, Will under WILL_PY or the running user's ~/bin.
"""
import collections
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board  # noqa: E402
import uistate  # noqa: E402

_TB = os.environ.get("TREEBEARD_BASE", "/opt/treebeard")


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="btl-")
        self.path = os.path.join(self.tmp, "board.json")
        self._env = {k: os.environ.get(k) for k in (
            "TASKBOARD_PATH", "MEESEEKS_ROOT", "BOARD_LOCK_TIMEOUT_S")}
        os.environ["TASKBOARD_PATH"] = self.path
        os.environ["MEESEEKS_ROOT"] = os.path.join(self.tmp, "meeseeks")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def card(self, iid):
        return board.find(board.load_board(), iid)

    def new(self, text="t", **kw):
        return board.add_item("task", text, None, **kw)

    def queued_linked(self, text="t"):
        iid = self.new(text)
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        board.set_phase(iid, "ready")
        return iid

    def active(self, text="t", owner=None):
        iid = board.add_item("task", text, None, owner=owner)
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        board.set_phase(iid, "ready")
        board.set_phase(iid, "in_progress", actor="reflector")
        return iid


# ---------------------------------------------------------------------------
class TransitionTests(_Fixture):
    """One function, one table; every write stamped."""

    def test_create_is_a_transition(self):
        iid = self.new()
        c = self.card(iid)
        self.assertEqual((c["rev"], c["by"], c["status"]), (1, "operator", "open"))
        self.assertEqual(c["history"][0]["op"], "create")
        self.assertEqual(board.canonical_state(c), "BACKLOG")

    def test_every_write_bumps_rev_and_records_history(self):
        iid = self.queued_linked()
        c = self.card(iid)
        self.assertEqual(c["rev"], 3)
        self.assertEqual([h["op"] for h in c["history"]],
                         ["create", "link", "queue"])
        self.assertEqual(c["exec_ref"], {"kind": "meeseeks",
                                         "id": "card-%d" % iid})

    def test_history_is_bounded(self):
        iid = self.new()
        for i in range(board.HISTORY_MAX + 5):
            board.transition(iid, "prioritize", "operator", priority=i)
        self.assertEqual(len(self.card(iid)["history"]), board.HISTORY_MAX)

    def test_schema_stays_a_list(self):
        """C1: every op leaves a top-level JSON list."""
        iid = self.active()
        board.set_phase(iid, "review", actor="reflector")
        board.transition(iid, "close", "operator", outcome="superseded",
                         reason="x")
        board.transition(iid, "reopen", "operator")
        self.assertIsInstance(self.raw(), list)

    def test_active_requires_exec_ref(self):
        iid = self.new()
        board.set_phase(iid, "ready")
        with self.assertRaises(board.TransitionRefused):
            board.set_phase(iid, "in_progress")
        self.assertEqual(self.card(iid)["phase"], "ready")

    def test_no_edge_skips_gate_one(self):
        iid = self.new()
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        for op in ("start", "request_review", "block", "resume"):
            with self.assertRaises(board.TransitionRefused, msg=op):
                board.transition(iid, op, "operator", reason="r")
        self.assertEqual(board.canonical_state(self.card(iid)), "BACKLOG")

    def test_done_means_success_only(self):
        iid = self.active()
        board.transition(iid, "fail", "reflector", reason="exhausted")
        c = self.card(iid)
        self.assertEqual((c["status"], c["outcome"]), ("closed", "failed"))
        self.assertNotEqual(c["status"], "done")
        self.assertEqual(board.canonical_state(c), "FAILED")

    def test_close_outcomes(self):
        for oc in board.CLOSE_OUTCOMES:
            iid = self.new()
            board.transition(iid, "close", "operator", outcome=oc,
                             reason="r", ref="UI-01")
            c = self.card(iid)
            self.assertEqual((c["status"], c["outcome"], c["ref"]),
                             ("closed", oc, "UI-01"))
            self.assertEqual(board.canonical_state(c), "CLOSED")
        iid = self.new()
        for bad in ("failed", "done", None, "clear"):
            with self.assertRaises(board.TransitionRefused):
                board.transition(iid, "close", "operator", outcome=bad,
                                 reason="r")
        with self.assertRaises(board.TransitionRefused):
            board.transition(iid, "close", "operator", outcome="cancelled")

    def test_fail_and_block_need_a_reason(self):
        iid = self.active()
        with self.assertRaises(board.TransitionRefused):
            board.transition(iid, "fail", "reflector")
        with self.assertRaises(board.TransitionRefused):
            board.transition(iid, "block", "reflector")

    def test_terminal_cards_accept_nothing_but_reopen(self):
        iid = self.new()
        board.transition(iid, "close", "operator", outcome="deferred",
                         reason="r")
        for op in ("queue", "complete", "fail", "close", "link", "assign"):
            with self.assertRaises(board.BoardError, msg=op):
                board.transition(iid, op, "operator", reason="r",
                                 outcome="cancelled",
                                 exec_ref={"kind": "status", "id": "x"})
        board.transition(iid, "reopen", "operator")
        self.assertEqual(board.canonical_state(self.card(iid)), "BACKLOG")

    def test_reopen_keeps_last_failure(self):
        iid = self.active()
        board.transition(iid, "fail", "operator", reason="boom")
        board.transition(iid, "reopen", "operator")
        c = self.card(iid)
        self.assertEqual(c["last_failure"]["reason"], "boom")
        self.assertEqual(c["last_failure"]["exec_ref"]["id"], "card-%d" % iid)
        self.assertNotIn("outcome", c)
        self.assertIn("failed", [h.get("prior_outcome")
                                 for h in c["history"]])

    def test_complete_with_open_child_is_a_typed_refusal(self):
        p = self.active("parent")
        board.add_item("task", "child", p)
        with self.assertRaises(board.ChildrenOpen):
            board.mark_done(p, actor="reflector")
        with self.assertRaises(board.PermissionRefused):
            board.mark_done(p, cascade=True, actor="reflector")
        board.mark_done(p, cascade=True)
        self.assertTrue(all(c["status"] == "done" for c in self.raw()))

    def test_terminal_parent_never_strands_open_children(self):
        p = self.new("parent")
        ch = board.add_item("task", "child", p)
        for op, kw in (("close", {"outcome": "superseded"}), ("fail", {})):
            with self.assertRaises(board.ChildrenOpen, msg=op):
                board.transition(p, op, "operator", reason="r", **kw)
        board.transition(ch, "close", "operator", outcome="superseded",
                         reason="r")
        board.transition(p, "close", "operator", outcome="superseded",
                         reason="r")
        self.assertEqual(board.canonical_state(self.card(p)), "CLOSED")

    def test_closed_child_does_not_block_parent(self):
        p = board.add_item("epic", "E", None)
        ch = board.add_item("feature", "F", p)
        board.transition(ch, "close", "operator", outcome="cancelled",
                         reason="r")
        board.mark_done(p)
        self.assertEqual(self.card(p)["status"], "done")

    def test_clear_is_not_governed(self):
        """L-R2: clear refuses on the canonical board, works on fixtures."""
        self.new()
        real = board.DEFAULT_BOARD_PATH
        board.DEFAULT_BOARD_PATH = self.path
        try:
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit):
                board.main(["clear", "--all"])
            self.assertIn("not a governed operation", err.getvalue())
            self.assertEqual(len(self.raw()), 1)
        finally:
            board.DEFAULT_BOARD_PATH = real
        with redirect_stdout(io.StringIO()):
            board.main(["clear", "--all"])
        self.assertEqual(self.raw(), [])

    def test_cli_surface_unchanged(self):
        """L-R1: no new op reaches the CLI. The command set is exactly the
        pre-L one; approve/close/fail/submit/reopen are library-only until W."""
        self.assertEqual(sorted(board.COMMANDS), sorted([
            "epic", "feature", "story", "task", "list", "plate", "assign",
            "top", "done", "phase", "link", "clear"]))
        self.assertNotIn("awaiting_approval", board.PHASES)


# ---------------------------------------------------------------------------
class AuthorityTests(_Fixture):
    """L-R4 + Tex: agents never close governed work; exec_ref confers NO
    authority; the actor table only narrows."""

    AGENT = "agent:clark"

    def test_agent_never_closes(self):
        iid = self.active(owner="clark")
        for op, kw in (("complete", {}), ("fail", {"reason": "r"}),
                       ("close", {"outcome": "cancelled", "reason": "r"}),
                       ("approve", {}), ("reject", {"note": "n"}),
                       ("reopen", {}), ("start", {}), ("queue", {}),
                       ("block", {"reason": "r"}), ("resume", {}),
                       ("return", {}), ("assign", {"owner": "clark"}),
                       ("link", {"exec_ref": {"kind": "status", "id": "x"}})):
            with self.assertRaises(board.PermissionRefused, msg=op):
                board.transition(iid, op, self.AGENT, **kw)
        self.assertEqual(board.canonical_state(self.card(iid)), "ACTIVE")
        # Will (the scheduler) cannot close either.
        for op in ("complete", "fail", "close", "approve"):
            with self.assertRaises(board.PermissionRefused, msg=op):
                board.transition(iid, op, "will", reason="r",
                                 outcome="cancelled")

    def test_agent_may_request_review_of_its_own_active_card_only(self):
        mine = self.active(owner="clark")
        theirs = self.active(owner="han")
        with self.assertRaises(board.PermissionRefused):
            board.transition(theirs, "request_review", self.AGENT)
        board.transition(mine, "request_review", self.AGENT)
        self.assertEqual(board.canonical_state(self.card(mine)), "IN_REVIEW")
        # ...and that is a claim: it is still not done.
        self.assertEqual(self.card(mine)["status"], "open")

    def test_exec_ref_confers_no_authority(self):
        """Possession of the card's EXACT exec_ref changes nothing."""
        iid = self.active(owner="clark")
        ref = dict(self.card(iid)["exec_ref"])
        for actor in (self.AGENT, "will", "agent:han"):
            for op in ("complete", "fail", "close", "approve", "start",
                       "link", "resume", "block", "reopen"):
                with self.assertRaises(board.PermissionRefused,
                                       msg="%s %s" % (actor, op)):
                    board.transition(iid, op, actor, exec_ref=ref,
                                     reason="r", outcome="cancelled",
                                     expect_rev=self.card(iid)["rev"])
        # nor does it buy a Gate 1 skip or an approval
        ap = self.new("needs ok")
        board.transition(ap, "submit", "operator")
        with self.assertRaises(board.PermissionRefused):
            board.transition(ap, "approve", self.AGENT,
                             expect_rev=self.card(ap)["rev"], exec_ref=ref)
        self.assertEqual(board.canonical_state(self.card(ap)),
                         "AWAITING_APPROVAL")
        # and an unknown actor carrying it is refused outright
        with self.assertRaises(board.PermissionRefused):
            board.transition(iid, "complete", "card-%d" % iid, exec_ref=ref)

    def test_agent_creates_only_as_itself(self):
        with self.assertRaises(board.PermissionRefused):
            board.add_item("task", "x", None, source="operator",
                           actor=self.AGENT)
        with self.assertRaises(board.PermissionRefused):
            board.add_item("task", "x", None, source="agent:han",
                           actor=self.AGENT)
        with self.assertRaises(board.PermissionRefused):
            board.add_item("task", "x", None, source="operator",
                           actor="will")
        self.assertEqual(board.load_board(), [])

    def test_unknown_actor_refused(self):
        iid = self.new()
        for actor in ("root", "", None, "agent:", "agent:../x", "Operator"):
            with self.assertRaises(board.PermissionRefused):
                board.transition(iid, "assign", actor, owner=None)

    def test_permission_table_is_frozen(self):
        """Item 7 of the G3 packet: the authority table, as shipped."""
        got = {op: (sorted(f), t, sorted(a))
               for op, (f, t, a) in board._OPS.items()}
        frozen = {
            "create": ([], "BACKLOG", ["agent", "operator", "scheduler"]),
            "assign": (sorted(board.OPEN_STATES), None, ["operator"]),
            "prioritize": (sorted(board.OPEN_STATES), None, ["operator"]),
            "link": (sorted(board.OPEN_STATES), None,
                     ["executor", "operator"]),
            "queue": (["BACKLOG"], "QUEUED", ["operator"]),
            "submit": (["BACKLOG"], "AWAITING_APPROVAL",
                       ["agent", "operator"]),
            "approve": (["AWAITING_APPROVAL"], "QUEUED", ["operator"]),
            "reject": (["AWAITING_APPROVAL"], "BACKLOG", ["operator"]),
            "start": (["QUEUED"], "ACTIVE", ["executor", "operator"]),
            "request_review": (["ACTIVE", "BLOCKED", "QUEUED", "RETURNED"],
                               "IN_REVIEW",
                               ["agent", "executor", "operator"]),
            "block": (["ACTIVE", "IN_REVIEW", "QUEUED", "RETURNED"],
                      "BLOCKED", ["executor", "operator"]),
            "resume": (["BLOCKED", "IN_REVIEW", "RETURNED"], "ACTIVE",
                       ["executor", "operator"]),
            "return": (["ACTIVE", "IN_REVIEW"], "RETURNED", ["operator"]),
            "complete": (sorted(board.OPEN_STATES), "DONE",
                         ["executor", "operator"]),
            "fail": (sorted(board.OPEN_STATES), "FAILED",
                     ["executor", "operator"]),
            "close": (sorted(board.OPEN_STATES), "CLOSED", ["operator"]),
            "reopen": (["CLOSED", "FAILED"], "BACKLOG", ["operator"]),
        }
        self.assertEqual({k: list(v) for k, v in got.items()},
                         {k: list(v) for k, v in frozen.items()})
        for op in ("complete", "fail", "close", "approve", "reject",
                   "reopen"):
            self.assertIn(op, board.AGENT_FORBIDDEN)


# ---------------------------------------------------------------------------
class ApprovalTests(_Fixture):
    """L-R3: AWAITING_APPROVAL is Gate 1, operator-only, revision-bound and
    idempotent; required only by the criteria."""

    def approval_card(self, **kw):
        iid = board.transition(None, "create", "operator", kind="task",
                               text="deploy X", **kw)["id"]
        return iid

    def test_criteria(self):
        self.assertFalse(board.requires_approval({}))
        self.assertFalse(board.requires_approval({"source": "agent:clark"}))
        for c in board.APPROVAL_CLASSES:
            self.assertTrue(board.requires_approval({"approval_class": c}))
        self.assertTrue(board.requires_approval({"approval_required": True}))
        # a junk class fails CLOSED
        self.assertTrue(board.requires_approval({"approval_class": "??"}))
        with self.assertRaises(board.BoardError):
            self.approval_card(approval_class="whatever")

    def test_routine_task_needs_no_approval(self):
        iid = self.new()
        board.set_phase(iid, "ready")
        self.assertEqual(board.canonical_state(self.card(iid)), "QUEUED")

    def test_approval_card_cannot_take_the_plain_gate(self):
        iid = self.approval_card(approval_class="deployment")
        with self.assertRaises(board.TransitionRefused):
            board.set_phase(iid, "ready")
        self.assertEqual(board.canonical_state(self.card(iid)), "BACKLOG")

    def test_submit_approve_is_revision_bound_and_idempotent(self):
        iid = self.approval_card(approval_class="external_action")
        board.transition(iid, "submit", "operator")
        rev = self.card(iid)["rev"]
        with self.assertRaises(board.TransitionRefused):
            board.transition(iid, "approve", "operator")       # no rev
        with self.assertRaises(board.StaleRevision):
            board.transition(iid, "approve", "operator", expect_rev=rev - 1)
        r1 = board.transition(iid, "approve", "operator", expect_rev=rev)
        self.assertEqual((r1["from"], r1["to"]),
                         ("AWAITING_APPROVAL", "QUEUED"))
        self.assertTrue(r1["receipt"])
        before = open(self.path, "rb").read()
        r2 = board.transition(iid, "approve", "operator", expect_rev=rev)
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r2["receipt"], r1["receipt"])
        self.assertEqual(open(self.path, "rb").read(), before, "no write")
        # a DIFFERENT (stale) rev after approval is refused, not re-approved
        with self.assertRaises(board.StaleRevision):
            board.transition(iid, "approve", "operator", expect_rev=rev + 5)

    def test_card_change_after_submit_invalidates_the_reviewed_rev(self):
        iid = self.approval_card(approval_required=True)
        board.transition(iid, "submit", "operator")
        rev = self.card(iid)["rev"]
        board.transition(iid, "assign", "operator", owner="clark")
        with self.assertRaises(board.StaleRevision):
            board.transition(iid, "approve", "operator", expect_rev=rev)

    def test_only_the_operator_approves_or_rejects(self):
        iid = self.approval_card(approval_class="authority")
        board.transition(iid, "submit", "operator")
        rev = self.card(iid)["rev"]
        for actor in ("reflector", "will", "agent:treebeard"):
            with self.assertRaises(board.PermissionRefused, msg=actor):
                board.transition(iid, "approve", actor, expect_rev=rev)
            with self.assertRaises(board.PermissionRefused, msg=actor):
                board.transition(iid, "reject", actor, expect_rev=rev,
                                 note="n")

    def test_reject_returns_to_backlog_with_a_note(self):
        iid = self.approval_card(approval_class="scope")
        board.transition(iid, "submit", "operator")
        rev = self.card(iid)["rev"]
        with self.assertRaises(board.TransitionRefused):
            board.transition(iid, "reject", "operator", expect_rev=rev)
        board.transition(iid, "reject", "operator", expect_rev=rev,
                         note="not now")
        self.assertEqual(board.canonical_state(self.card(iid)), "BACKLOG")

    def test_agent_submits_only_its_own(self):
        iid = board.add_item("task", "x", None, source="agent:clark",
                             owner="clark", actor="will")
        with self.assertRaises(board.PermissionRefused):
            board.transition(iid, "submit", "agent:han")
        board.transition(iid, "submit", "agent:clark")
        self.assertEqual(board.canonical_state(self.card(iid)),
                         "AWAITING_APPROVAL")


# ---------------------------------------------------------------------------
_CHILD = r'''
import os, sys, time, json
sys.path.insert(0, %(lib)r)
import board
role, go, slow = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
real_save = board.save_board
def slow_save(items):
    time.sleep(slow)          # widen the load->save window
    real_save(items)
board.save_board = slow_save
while time.time() < go:
    time.sleep(0.001)
if role == "reflector":
    board.set_phase(int(sys.argv[4]), "review", actor="reflector")
elif role == "will":
    board.add_item("task", "derived by will", None, source="agent:han",
                   owner="han", actor="will")
elif role == "holder":
    with board.board_lock():
        time.sleep(slow)
print("ok")
'''


class LockTests(_Fixture):
    """Moe flag 2 / L-R1: serialization is mandatory. Reflector and Will
    fire in the same second; neither may lose the other's write."""

    def _spawn(self, role, go, slow, *extra):
        code = _CHILD % {"lib": _HERE}
        return subprocess.Popen(
            [sys.executable, "-c", code, role, str(go), str(slow)]
            + [str(e) for e in extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ))

    def _race(self, slow_r, slow_w, stagger):
        iid = self.active()
        go = time.time() + 0.5
        r = self._spawn("reflector", go, slow_r, iid)
        w = self._spawn("will", go + stagger, slow_w)
        for p in (r, w):
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        items = board.load_board()
        return iid, items

    def test_concurrent_reflector_and_will_lose_nothing(self):
        # Both orders, with the load->save window held open by the first.
        for slow_r, slow_w, stagger in ((0.6, 0.0, 0.15), (0.0, 0.6, -0.15)):
            iid, items = self._race(slow_r, slow_w, stagger)
            c = board.find(items, iid)
            self.assertEqual(c["phase"], "review",
                             "reflector's write was lost")
            self.assertTrue(any(i["text"] == "derived by will"
                                for i in items), "Will's write was lost")
            self.assertEqual([h["op"] for h in c["history"]][-1],
                             "request_review")
            board.save_board([])

    def test_lock_timeout_refuses_without_writing(self):
        iid = self.new()
        before = open(self.path, "rb").read()
        h = self._spawn("holder", time.time(), 2.0)
        time.sleep(0.6)
        os.environ["BOARD_LOCK_TIMEOUT_S"] = "0.3"
        try:
            with self.assertRaises(board.BoardLockTimeout):
                board.transition(iid, "assign", "operator", owner="clark")
        finally:
            h.communicate(timeout=30)
        self.assertEqual(open(self.path, "rb").read(), before)

    def test_lock_is_reentrant_in_process(self):
        with board.board_lock():
            iid = self.new()          # nested acquisition
            board.transition(iid, "assign", "operator", owner="han")
        self.assertEqual(self.card(iid)["owner"], "han")

    def test_lockfile_is_beside_the_board(self):
        self.new()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".board.lock")))


# ---------------------------------------------------------------------------
class PlateTests(_Fixture):
    """Carried item f: plate filter and review cap."""

    def test_plate_excludes_review_blocked_approval_and_closed(self):
        a = board.add_item("task", "backlog", None, owner="han")
        b = self.active(owner="han")
        board.set_phase(b, "review", actor="reflector")
        c = self.active(owner="han")
        board.set_phase(c, "blocked", actor="reflector", reason="r")
        d = board.add_item("task", "approval", None, owner="han")
        board.transition(d, "submit", "operator")
        e = board.add_item("task", "gone", None, owner="han")
        board.transition(e, "close", "operator", outcome="cancelled",
                         reason="r")
        f = self.active(owner="han")
        self.assertEqual(sorted(i["id"] for i in
                                board.plate(board.load_board(), "han")),
                         sorted([a, f]))

    def test_review_cap(self):
        for _ in range(board.REVIEW_CAP):
            i = self.active(owner="han")
            board.set_phase(i, "review", actor="reflector")
        self.assertEqual(board.plate(board.load_board(), "han"), [])
        self.assertFalse(board.may_derive(board.load_board(), "han"))
        self.assertTrue(board.may_derive(board.load_board(), "clark"))


# ---------------------------------------------------------------------------
class MetaSidecarTests(_Fixture):
    """L-R7: freshness/reconcile metadata ONLY, never a second board."""

    def test_meta_is_flat_whitelisted_and_never_a_board(self):
        self.new()
        d = board.write_meta("reflector", reconciled_utc="2026-10-02T00:00:00Z",
                             reconciled_epoch=1, inconsistent=[58])
        self.assertEqual(d["board_sha256"], board.board_digest())
        self.assertIsInstance(board.read_meta(), dict)
        with self.assertRaises(board.BoardError):
            board.write_meta("reflector", cards=[{"id": 1}])
        with self.assertRaises(board.BoardError):
            board.write_meta("reflector", inconsistent=[{"id": 1}])
        with self.assertRaises(board.BoardError):
            board.write_meta("reflector", run_id={"nested": 1})
        with self.assertRaises(board.PermissionRefused):
            board.write_meta("will", faults=0)
        with self.assertRaises(board.PermissionRefused):
            board.write_meta("agent:clark", faults=0)
        self.assertEqual(len(board.load_board()), 1)
        self.assertTrue(board.meta_path().endswith("board.meta.json"))

    def test_s1_tells_unchanged_from_unreconciled(self):
        import statusboard
        self.new()
        now = int(time.time())
        old = now - (statusboard.BOARD_STALE_AFTER_H * 3600 + 600)
        os.utime(self.path, (old, old))
        self.assertEqual(statusboard.board_freshness(self.path, now)
                         ["verdict"], "STALE")
        board.write_meta("reflector", reconciled_epoch=now - 60,
                         reconciled_utc="x")
        fr = statusboard.board_freshness(self.path, now)
        self.assertEqual(fr["verdict"], "UNCHANGED")
        self.assertFalse(fr["stale"])
        # an old reconcile is not good enough
        board.write_meta("reflector", reconciled_epoch=old,
                         reconciled_utc="x")
        self.assertEqual(statusboard.board_freshness(self.path, now)
                         ["verdict"], "STALE")
        # a reconcile of DIFFERENT board bytes is not good enough either
        board.write_meta("reflector", reconciled_epoch=now - 60,
                         reconciled_utc="x")
        with open(self.path, "a") as fh:
            fh.write(" ")
        os.utime(self.path, (old, old))
        fr = statusboard.board_freshness(self.path, now)
        self.assertEqual((fr["verdict"], fr["reconcile_matches"]),
                         ("STALE", False))


# ---------------------------------------------------------------------------
class StickyFailureTests(unittest.TestCase):
    """L-R8: status-level sticky last_failure; never authority."""

    def setUp(self):
        import statusboard
        self.sb = statusboard
        self.tmp = tempfile.mkdtemp(prefix="btl-sb-")
        self._dir = statusboard.STATUS_DIR
        statusboard.STATUS_DIR = self.tmp

    def tearDown(self):
        self.sb.STATUS_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def card(self):
        return json.load(open(os.path.join(self.tmp, "job.json")))

    def test_failure_survives_later_successes(self):
        sb = self.sb
        sb.write_status("job", "ok", "fine", stale_after_hours=1, run_id="r1")
        self.assertNotIn("last_failure", self.card())
        sb.write_status("job", "fail", "broke", stale_after_hours=1,
                        run_id="r2")
        sb.write_status("job", "ok", "fine again", stale_after_hours=1,
                        run_id="r3")
        sb.write_status("job", "warn", "meh", stale_after_hours=1,
                        run_id="r4")
        lf = self.card()["last_failure"]
        self.assertEqual((lf["run_id"], lf["summary"]), ("r2", "broke"))
        self.assertEqual(self.card()["status"], "warn")

    def test_newer_failure_supersedes_and_clear_is_explicit(self):
        sb = self.sb
        sb.write_status("job", "fail", "one", stale_after_hours=1, run_id="a")
        sb.write_status("job", "fail", "two", stale_after_hours=1, run_id="b")
        self.assertEqual(self.card()["last_failure"]["run_id"], "b")
        sb.write_status("job", "ok", "acked", stale_after_hours=1,
                        clear_last_failure=True)
        self.assertNotIn("last_failure", self.card())

    def test_unreadable_prior_is_not_erased(self):
        with open(os.path.join(self.tmp, "job.json"), "w") as fh:
            fh.write("{torn")
        self.sb.write_status("job", "ok", "fine", stale_after_hours=1)
        self.assertTrue(self.card()["last_failure"]["unknown"])

    def test_pre_l_fail_card_carries_into_last_failure(self):
        with open(os.path.join(self.tmp, "job.json"), "w") as fh:
            json.dump({"status": "fail", "summary": "old", "run_id": "z",
                       "updated_utc": "2026-10-01T00:00:00Z",
                       "updated_epoch": 1}, fh)
        self.sb.write_status("job", "ok", "fine", stale_after_hours=1)
        self.assertEqual(self.card()["last_failure"]["summary"], "old")

    def test_box_status_labels_it_history_not_state(self):
        import boxread
        real = boxread.STATUS_DIR
        boxread.STATUS_DIR = self.tmp
        try:
            self.sb.write_status("job", "fail", "broke", stale_after_hours=1,
                                 run_id="r2")
            self.sb.write_status("job", "ok", "fine", stale_after_hours=1,
                                 run_id="r3")
            r = boxread._status_one("job")
        finally:
            boxread.STATUS_DIR = real
        self.assertEqual((r["status"], r["state"]), ("OK", "ok"))
        self.assertIn("NOT current state", r["last_failure"])
        self.assertIn("NOT an authority signal", r["last_failure"])
        self.assertIn("run_id=r2", r["last_failure"])


# ---------------------------------------------------------------------------
# ALL-SURFACE RESOLVER EQUALITY. One fixture board + one fixture ledger
# through every consumer. Expected keys are fixed here, independently of
# the code under test.
T0 = "2026-10-01T00:00:00+00:00"
# id: (status, phase, outcome, pid, project_state, [(sid, state, reason)],
#      expected key)
FIXTURE = {
    1: ("open", "blocked", None, "card-1", "ACTIVE",
        [("s01", "SPLIT", None), ("s02", "BLOCKED",
                                  "FACTORY_EXHAUSTED: stages exhausted")],
        "failed"),
    2: ("open", "blocked", None, "card-2", "ACTIVE",
        [("s01", "BLOCKED", "FACTORY_EXHAUSTED: x"), ("s02", "QUEUED", None)],
        "failed"),
    3: ("open", None, None, None, None, None, "backlog"),
    4: ("open", "ready", None, None, None, None, "queued"),
    5: ("open", "blocked", None, "card-5", "ACTIVE",
        [("s01", "BLOCKED", "FACTORY_EXHAUSTED: stages exhausted at github")],
        "failed"),
    6: ("open", "in_progress", None, "card-6", "ACTIVE",
        [("s01", "UNREVIEWABLE", None)], "failed"),
    7: ("open", "blocked", None, "card-7", "ACTIVE",
        [("s01", "BLOCKED", "waiting on operator")], "blocked"),
    8: ("open", "review", None, "card-8", "ACTIVE",
        [("s01", "AWAITING_TREEBEARD", None)], "review"),
    9: ("open", "returned", None, None, None, None, "returned"),
    10: ("open", "awaiting_approval", None, None, None, None, "approval"),
    11: ("open", "in_progress", None, "card-11", "ACTIVE",
         [("s01", "TESTING", None)], "working"),
    12: ("closed", None, "cancelled", None, None, None, "closed"),
    13: ("closed", None, "failed", "card-13", "ACTIVE",
         [("s01", "BLOCKED", "FACTORY_EXHAUSTED: y")], "failed"),
    14: ("done", None, None, "card-14", "ACTIVE", [("s01", "DONE", None)],
         "done"),
    15: ("done", None, None, "card-15", "CLOSED_BLOCKED",
         [("s01", "BLOCKED", "cycles")], "unknown"),
    16: ("done", None, None, None, None, None, "done"),
}
_LABEL2KEY = {v[0]: k for k, v in uistate.STATES.items()}


def _fixture_cards():
    out = []
    for i, (st, ph, oc, pid, _p, _s, _k) in sorted(FIXTURE.items()):
        c = {"id": i, "kind": "task", "text": "FX-%d" % i, "parent": None,
             "status": st, "created": T0, "source": "operator",
             "owner": "han" if i in (3, 4, 9, 11) else None, "priority": 0,
             "phase": ph, "meeseeks_pid": pid}
        if oc:
            c["outcome"] = oc
            c["reason"] = "fixture"
        if st in ("done", "closed"):
            c["closed"] = "2026-10-01T%02d:00:00+00:00" % (i % 24)
        out.append(c)
    return out


def _will_path():
    return os.environ.get("WILL_PY") or os.path.expanduser("~/bin/will.py")


def _load_will():
    import importlib.util
    path = _will_path()
    spec = importlib.util.spec_from_file_location("will_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sun():
    import importlib.util
    path = os.path.join(_TB, "sun", "agent.py")
    spec = importlib.util.spec_from_file_location("sun_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AllSurfaceResolverEqualityTests(_Fixture):

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(_TB, "shared", "meeseeks"))
        import meeseeks
        con = meeseeks.connect()
        for i, (_st, _ph, _oc, pid, pstate, secs, _k) in FIXTURE.items():
            if not pid:
                continue
            con.execute("INSERT INTO projects(pid,name,master,state,"
                        "call_cap,created,updated) VALUES(?,?,?,?,?,?,?)",
                        (pid, pid, "m", pstate, 10, T0, T0))
            for sid, state, why in secs:
                con.execute("INSERT INTO sections(pid,sid,parent,title,state,"
                            "blocked_reason,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (pid, sid, None, "t", state, why, T0, T0))
        con.close()
        board.save_board(_fixture_cards())
        self.want = {i: v[-1] for i, v in FIXTURE.items()}
        self.open_ids = {i for i, v in FIXTURE.items() if v[0] == "open"}

    def test_every_surface_agrees(self):
        import boxread
        import opshell
        import opsrv
        seen = collections.defaultdict(dict)
        # R1 box.card
        for i in FIXTURE:
            r = boxread.card_state(str(i))
            self.assertEqual(r["status"], "OK", r)
            seen["box.card"][i] = _LABEL2KEY[r["state"]]
        # R2 box.cards (open cards)
        rows = boxread.cards_active()
        self.assertEqual(rows["status"], "OK", rows)
        for row in rows["rows"]:
            m = re.match(r"^#(\d+) \[([^\]]+)\]", row)
            seen["box.cards"][int(m.group(1))] = _LABEL2KEY[m.group(2)]
        self.assertEqual(set(seen["box.cards"]), self.open_ids)
        # R3 /board
        for key, cards in opsrv.board_cards().items():
            for c in cards:
                seen["/board"][c["id"]] = key
        # R4 /task
        for c in board.load_board():
            h = opshell.render_task(c)
            m = re.search(r'<dt>State</dt><dd><span class="st [^"]+" '
                          r'role="img" aria-label="([^"]+)"', h)
            seen["/task"][c["id"]] = _LABEL2KEY[m.group(1)]
        # R4b Currently Working + NEEDS YOU membership
        work = {c["id"] for c, _k in opshell.working_cards()}
        att = {c["id"]: k for c, k in opshell.attention_cards()}
        self.assertEqual(work, {i for i, k in self.want.items()
                                if k == "working"})
        self.assertEqual(att, {i: k for i, k in self.want.items()
                               if k in uistate.ATTENTION})
        # R5 Recently Finished (done + closed)
        fin = opshell.render_finished(limit=50)
        for li in fin.split("<li")[1:]:
            m = re.search(r"card (\d+)</a>.*?aria-label=\"([^\"]+)\"", li)
            seen["finished"][int(m.group(1))] = _LABEL2KEY[m.group(2)]
        self.assertEqual(set(seen["finished"]),
                         {i for i, v in FIXTURE.items()
                          if v[0] in ("done", "closed")})
        # R6 Sun digest: per-state counts + plates
        sun = _load_sun()
        sun.BOARD_PATH = self.path
        html_ = sun.render_board_activity()
        m = re.search(r"<b>By state:</b> ([^<]+)</p>", html_)
        got = {}
        for part in m.group(1).split(" · "):
            lab, n = part.rsplit(" ", 1)
            got[_LABEL2KEY[lab]] = int(n)
        self.assertEqual(got, dict(collections.Counter(self.want.values())))
        # C2: the Sun source table never counts closed as done
        row = re.search(r"<b>Operator</b></td>((?:<td[^>]*>\d+</td>){5})",
                        html_)
        cols_sun = [int(x) for x in re.findall(r">(\d+)<", row.group(1))]
        st = collections.Counter(
            ("failed" if v[2] == "failed" else "closed") if v[0] == "closed"
            else v[0] for v in FIXTURE.values())
        self.assertEqual(cols_sun, [st["open"], st["done"], st["failed"],
                                    st["closed"], 0])
        cell = sun.render_plate_cell("han")
        self.assertEqual(sorted(int(x) for x in re.findall(r"#(\d+) ", cell)),
                         sorted(c["id"] for c in board.plate(
                             board.load_board(), "han")))
        # R7 Will wake prompt. Will is an operator-only wrapper (~/bin) that
        # the public edition does not ship: when it is absent the five other
        # surfaces are still compared in full, and the test then SKIPS with
        # the reason (B2, RC5). Where Will exists (the production box) this
        # runs exactly as before.
        will_path = _will_path()
        will_absent = not os.path.isfile(will_path)
        if not will_absent:
            will = _load_will()
            txt = will.render_board(board)
            for line in txt.splitlines():
                m = re.match(r"^\s+(\d+) \(task\) \[[^\]]+\] \[([^\]]+)\]", line)
                if m:
                    seen["will"][int(m.group(1))] = _LABEL2KEY[m.group(2)]
            self.assertEqual(set(seen["will"]), self.open_ids)
            self.assertIn("BOARD FRESHNESS:", txt)
        if os.environ.get("BTL_PRINT_MATRIX"):
            cols = ["box.card", "box.cards", "/board", "/task", "finished",
                    "will"]
            print("\ncard  expected  " + "  ".join("%-9s" % c for c in cols)
                  + "  working  NEEDS-YOU")
            for i in sorted(FIXTURE):
                print("%4d  %-8s  " % (i, self.want[i]) + "  ".join(
                    "%-9s" % seen.get(c, {}).get(i, "-") for c in cols)
                    + "  %-7s  %s" % ("yes" if i in work else "-",
                                      att.get(i, "-")))
            print("Sun By-state counts: %s" % dict(sorted(got.items())))
            print("Sun Operator row (open, done, failed, closed, unknown): "
                  "%s" % cols_sun)
        # every surface == the fixed expectation, card by card
        for surface, keys in seen.items():
            for i, k in keys.items():
                self.assertEqual(k, self.want[i],
                                 "%s says card %d is %s, expected %s"
                                 % (surface, i, k, self.want[i]))
        # 6 per-card surfaces here + Sun (per-state counts and plates) +
        # Working / NEEDS YOU membership above = all seven of L sec. 2b.
        self.assertEqual(sorted(seen), sorted(
            ["box.card", "box.cards", "/board", "/task", "finished"]
            + ([] if will_absent else ["will"])))
        if will_absent:
            self.skipTest("Will surface not compared: %s is absent (an "
                          "operator-only wrapper the public edition does "
                          "not ship); the other five surfaces were compared "
                          "and agree" % will_path)

    def test_closed_is_never_waiting_or_backlog(self):
        for c in board.load_board():
            if c["status"] == "closed":
                self.assertIn(uistate.resolve(c), ("closed", "failed"))
        self.assertNotIn("waiting", uistate.STATES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
