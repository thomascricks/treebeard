#!/usr/bin/env python3
"""test_board.py -- unittest suite for board.py. Runs entirely in a temp
directory via TASKBOARD_PATH; touches no real files."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import board


def run(argv):
    """Invoke board.main capturing stdout/stderr and exit code.
    Returns (code, out, err). SystemExit from _fail is captured."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = board.main(argv)
    except SystemExit as e:
        code = e.code
    return code or 0, out.getvalue(), err.getvalue()


class BoardTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boardtest-")
        self.path = os.path.join(self.tmp, "board.json")
        os.environ["TASKBOARD_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("TASKBOARD_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def raw(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def add(self, *argv):
        code, out, err = run(list(argv))
        self.assertEqual(code, 0, f"add failed: {err}")
        return int(out.strip())

    # -- add commands and ids ---------------------------------------------

    def test_epic_add(self):
        eid = self.add("epic", "Ship it")
        items = self.raw()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "epic")
        self.assertIsNone(items[0]["parent"])
        self.assertEqual(items[0]["status"], "open")
        self.assertEqual(eid, 1)

    def test_ids_monotonic(self):
        a = self.add("epic", "one")
        b = self.add("epic", "two")
        self.assertEqual((a, b), (1, 2))

    def test_feature_under_epic(self):
        eid = self.add("epic", "E")
        fid = self.add("feature", str(eid), "F")
        self.assertEqual(self.raw()[1]["parent"], eid)
        self.assertEqual(fid, 2)

    def test_story_under_feature(self):
        eid = self.add("epic", "E")
        fid = self.add("feature", str(eid), "F")
        sid = self.add("story", str(fid), "S")
        self.assertEqual(self.raw()[2]["parent"], fid)
        self.assertEqual(self.raw()[2]["kind"], "story")

    def test_task_parentless_and_under(self):
        tid = self.add("task", "loose end")
        eid = self.add("epic", "E")
        tid2 = self.add("task", "attached", "--under", str(eid))
        items = self.raw()
        self.assertIsNone(items[0]["parent"])
        self.assertEqual(items[2]["parent"], eid)

    # -- hierarchy refusals ------------------------------------------------

    def test_feature_refuses_nonepic_parent(self):
        tid = self.add("task", "T")
        code, _o, err = run(["feature", str(tid), "F"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot go under", err)
        self.assertEqual(len(self.raw()), 1)  # no write on refusal

    def test_story_refuses_epic_parent(self):
        eid = self.add("epic", "E")
        code, _o, err = run(["story", str(eid), "S"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot go under", err)

    def test_missing_parent_id(self):
        code, _o, err = run(["feature", "99", "F"])
        self.assertNotEqual(code, 0)
        self.assertIn("no item with id 99", err)

    # -- list --------------------------------------------------------------

    def test_list_tree_and_all(self):
        eid = self.add("epic", "E")
        fid = self.add("feature", str(eid), "F")
        tid = self.add("task", "loose")
        run(["done", str(fid)])
        code, out, _e = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("(epic) E", out)
        self.assertIn("(task) loose", out)
        self.assertNotIn("(feature) F", out)          # done hidden
        code, out, _e = run(["list", "--all"])
        self.assertIn("[x]", out)                      # done shown marked
        self.assertIn("(feature) F", out)
        # child indented under parent
        lines = out.splitlines()
        eline = next(l for l in lines if "(epic)" in l)
        fline = next(l for l in lines if "(feature)" in l)
        self.assertTrue(fline.startswith("  "))
        self.assertFalse(eline.startswith("  "))

    # -- done --------------------------------------------------------------

    def test_done_and_missing_id(self):
        tid = self.add("task", "T")
        code, _o, _e = run(["done", str(tid)])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw()[0]["status"], "done")
        code, _o, err = run(["done", "42"])
        self.assertNotEqual(code, 0)
        self.assertIn("no item with id 42", err)

    def test_done_refuses_open_children(self):
        eid = self.add("epic", "E")
        self.add("feature", str(eid), "F")
        code, _o, err = run(["done", str(eid)])
        self.assertNotEqual(code, 0)
        self.assertIn("open child", err)
        self.assertEqual(self.raw()[0]["status"], "open")  # unchanged

    def test_done_cascade(self):
        eid = self.add("epic", "E")
        fid = self.add("feature", str(eid), "F")
        sid = self.add("story", str(fid), "S")
        code, _o, _e = run(["done", str(eid), "--cascade"])
        self.assertEqual(code, 0)
        self.assertTrue(all(it["status"] == "done" for it in self.raw()))

    # -- clear -------------------------------------------------------------

    def test_clear_sweeps_done_only(self):
        a = self.add("task", "keep")
        b = self.add("task", "sweep")
        run(["done", str(b)])
        code, _o, _e = run(["clear"])
        self.assertEqual(code, 0)
        items = self.raw()
        self.assertEqual([it["text"] for it in items], ["keep"])

    def test_clear_keeps_done_parent_with_open_child(self):
        eid = self.add("epic", "E")
        fid = self.add("feature", str(eid), "F")
        # Close the parent via cascade, then reopen the child by hand to
        # construct a done-parent/open-child board.
        run(["done", str(eid), "--cascade"])
        items = self.raw()
        items[1]["status"] = "open"
        board.save_board(items)
        run(["clear"])
        texts = {it["text"] for it in self.raw()}
        self.assertEqual(texts, {"E", "F"})  # parent kept, child not orphaned

    def test_clear_all(self):
        self.add("task", "x")
        self.add("epic", "y")
        code, _o, _e = run(["clear", "--all"])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw(), [])

    # -- corrupt / missing file -------------------------------------------

    def test_missing_file_is_empty_board(self):
        code, out, _e = run(["list"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_corrupt_file_recovers(self):
        with open(self.path, "w") as fh:
            fh.write("{not json at all")
        code, out, _e = run(["list"])
        self.assertEqual(code, 0)
        tid = self.add("task", "fresh start")
        self.assertEqual(len(self.raw()), 1)  # rewrote clean

    def test_nonlist_json_recovers(self):
        with open(self.path, "w") as fh:
            json.dump({"oops": True}, fh)
        tid = self.add("task", "T")
        self.assertEqual(len(self.raw()), 1)

    # -- atomicity smoke ---------------------------------------------------

    def test_no_stray_tempfiles(self):
        self.add("task", "a")
        self.add("task", "b")
        strays = [f for f in os.listdir(self.tmp) if f.startswith(".board-")]
        self.assertEqual(strays, [])




class BoardV2Tests(unittest.TestCase):
    """v2: source precedence, owner/plate, backward compatibility."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boardv2-")
        self.path = os.path.join(self.tmp, "board.json")
        os.environ["TASKBOARD_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("TASKBOARD_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v1_board_upgrades(self):
        # A v1 board (no source/owner fields) must load with defaults.
        with open(self.path, "w") as fh:
            json.dump([{"id": 1, "kind": "task", "text": "old",
                        "parent": None, "status": "open",
                        "created": "2026-07-17T00:00:00Z"}], fh)
        items = board.load_board()
        self.assertEqual(items[0]["source"], "operator")
        self.assertIsNone(items[0]["owner"])

    def test_precedence_order_in_list(self):
        board.add_item("task", "agent first", None, source="agent:clark")
        board.add_item("task", "treebeard second", None, source="treebeard")
        board.add_item("task", "operator last", None, source="operator")
        code, out, _e = run(["list"])
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertIn("operator last", lines[0])
        self.assertIn("treebeard second", lines[1])
        self.assertIn("agent first", lines[2])

    def test_invalid_source_refused(self):
        with self.assertRaises(ValueError):
            board.add_item("task", "x", None, source="god")
        with self.assertRaises(ValueError):
            board.add_item("task", "x", None, source="agent:../etc")
        self.assertEqual(board.load_board(), [])  # no write on refusal

    def test_plate_and_assign(self):
        a = board.add_item("task", "mine", None, owner="clark")
        b = board.add_item("task", "unowned", None)
        code, out, _e = run(["plate", "clark"])
        self.assertIn("mine", out)
        self.assertNotIn("unowned", out)
        run(["assign", str(b), "clark"])
        code, out, _e = run(["plate", "clark"])
        self.assertIn("unowned", out)
        run(["assign", str(b), "--none"])
        code, out, _e = run(["plate", "clark"])
        self.assertNotIn("unowned", out)

    def test_done_item_leaves_plate(self):
        a = board.add_item("task", "finishme", None, owner="clark")
        run(["done", str(a)])
        code, out, _e = run(["plate", "clark"])
        self.assertEqual(out, "")


class PhaseTests(unittest.TestCase):
    """Frozen MVP board contract: phase + meeseeks_pid (2026-08-27)."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boardph-")
        self.path = os.path.join(self.tmp, "board.json")
        os.environ["TASKBOARD_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("TASKBOARD_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_board_path_is_absolute(self):
        # A relative default silently resolved against cwd and produced a
        # phantom empty board (three incidents 2026-08-27).
        os.environ.pop("TASKBOARD_PATH", None)
        p = board.board_path()
        self.assertTrue(os.path.isabs(p), "default board path must be absolute: %r" % p)
        self.assertEqual(p, board.DEFAULT_BOARD_PATH)
        os.environ["TASKBOARD_PATH"] = self.path   # restore isolation

    # --- state-root default + absent-vs-empty (installer-bootstrap-20260828) ---
    # These run in a CHILD process: DEFAULT_BOARD_PATH is computed at import
    # time from TREEBEARD_BASE, and the absent-file case must not resolve to
    # the real production board.

    def _child(self, code, **env):
        e = dict(os.environ, **env)
        e.pop("TASKBOARD_PATH", None) if "TASKBOARD_PATH" not in env else None
        pre = "import sys; sys.path.insert(0, %r); import board\n" % (
            os.path.dirname(os.path.abspath(board.__file__)),)
        r = subprocess.run([sys.executable, "-c", pre + code],
                           capture_output=True, text=True, env=e)
        return r

    def test_default_board_path_follows_state_root(self):
        # The board is STATE. A hard /opt literal pointed a relocated or freshly
        # installed host at a board that does not exist there.
        r = self._child("print(board.DEFAULT_BOARD_PATH)",
                        TREEBEARD_BASE="/tmp/tb-test-root")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         "/tmp/tb-test-root/shared/board/board.json")

    def test_absent_default_board_warns_and_returns_empty(self):
        # ABSENT is not EMPTY: announce it, but do not change the return value.
        r = self._child("print('R', board.load_board())",
                        TREEBEARD_BASE="/tmp/tb-test-absent-%d" % os.getpid())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "R []")
        self.assertIn("no board file", r.stderr)

    def test_absent_with_explicit_path_is_silent(self):
        # An explicit TASKBOARD_PATH is a deliberate choice, not a broken install.
        p = os.path.join(self.tmp, "definitely-absent.json")
        r = self._child("print('R', board.load_board())", TASKBOARD_PATH=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "R []")
        self.assertEqual(r.stderr.strip(), "")

    def test_new_item_null_phase_and_pid(self):
        iid = board.add_item("task", "t", None)
        it = board.find(board.load_board(), iid)
        self.assertIsNone(it["phase"])
        self.assertIsNone(it["meeseeks_pid"])

    def test_v1_board_upgrades_null_not_ready(self):
        with open(self.path, "w") as fh:
            json.dump([{"id": 1, "kind": "task", "text": "old",
                        "parent": None, "status": "open",
                        "created": "2026-07-17T00:00:00Z"}], fh)
        it = board.load_board()[0]
        self.assertIsNone(it["phase"])
        self.assertIsNone(it["meeseeks_pid"])

    def test_legal_transition_chain(self):
        iid = board.add_item("task", "t", None)
        # BOARD-TRUTH L: ACTIVE requires an exec_ref, so the card is linked
        # before it can reach in_progress.
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        for ph in ("ready", "in_progress", "review",
                   "returned", "in_progress", "blocked", "in_progress"):
            board.set_phase(iid, ph)
        it = board.find(board.load_board(), iid)
        self.assertEqual(it["phase"], "in_progress")

    def test_illegal_transitions_rejected_no_write(self):
        iid = board.add_item("task", "t", None)
        for bad in ("in_progress", "review", "blocked", "returned"):
            with self.assertRaises(ValueError):
                board.set_phase(iid, bad)   # from null, only ready is legal
        board.set_phase(iid, "ready")
        with self.assertRaises(ValueError):
            board.set_phase(iid, "returned")  # ready cannot jump to returned
        it = board.find(board.load_board(), iid)
        self.assertEqual(it["phase"], "ready")

    # --- S-BRIDGE-1 widened edges: one test per newly legal transition ---

    def _at(self, phase):
        """Drive a fresh card to `phase` by a legal path; return its id."""
        iid = board.add_item("task", "t", None)
        board.set_phase(iid, "ready")
        if phase == "ready":
            return iid
        board.set_meeseeks_pid(iid, "card-%d" % iid)   # L: ACTIVE needs a link
        board.set_phase(iid, "in_progress")
        if phase != "in_progress":
            board.set_phase(iid, phase)
        return iid

    def test_crash_recovery_ready_to_review(self):
        """CRASH-RECOVERY: commission() writes meeseeks_pid before the phase.
        A crash between those two writes leaves a card at `ready` that already
        owns a project. If that project has since reached the gate, the next
        reconciliation must be able to reflect `review` from `ready`.
        This transition is intentional. Do not remove it as an oddity."""
        iid = self._at("ready")
        board.set_meeseeks_pid(iid, "card-%d" % iid)   # the pid landed
        board.set_phase(iid, "review")                 # the phase had not
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "review")

    def test_crash_recovery_ready_to_blocked(self):
        """CRASH-RECOVERY, blocked variant. Same pid-before-phase window as
        test_crash_recovery_ready_to_review; the orphaned project may have
        blocked rather than reached the gate. Intentional. Do not remove."""
        iid = self._at("ready")
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        board.set_phase(iid, "blocked")
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "blocked")

    def test_review_to_blocked(self):
        """A gated section is retasked and then exhausts its cycles."""
        iid = self._at("review")
        board.set_phase(iid, "blocked")
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "blocked")

    def test_blocked_to_review(self):
        """Multi-section project: one BLOCKED section clears while another
        sits at AWAITING_TREEBEARD, so project_phase() moves blocked->review."""
        iid = self._at("blocked")
        board.set_phase(iid, "review")
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "review")

    def test_returned_to_review(self):
        iid = self._at("returned")
        board.set_phase(iid, "review")
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "review")

    def test_returned_to_blocked(self):
        iid = self._at("returned")
        board.set_phase(iid, "blocked")
        self.assertEqual(board.find(board.load_board(), iid)["phase"], "blocked")

    def test_no_edge_into_ready_from_any_phase(self):
        """Gate 1 is the ONLY entry to the lifecycle. Widening the table did
        not create a way back to `ready` from anywhere."""
        for src in ("ready", "in_progress", "review", "returned", "blocked"):
            iid = self._at(src)
            with self.assertRaises(ValueError,
                                   msg="%s -> ready must remain illegal" % src):
                board.set_phase(iid, "ready")
            self.assertEqual(board.find(board.load_board(), iid)["phase"], src)

    def test_negative_per_source_state_unapproved_edges_still_raise(self):
        """One explicit negative per source state, beyond the ready case
        asserted in test_illegal_transitions_rejected_no_write."""
        for src, bad in (("ready", "returned"),
                         ("in_progress", "in_progress"),
                         ("review", "review"),
                         ("returned", "returned"),
                         ("blocked", "blocked")):
            iid = self._at(src)
            with self.assertRaises(ValueError,
                                   msg="%s -> %s must raise" % (src, bad)):
                board.set_phase(iid, bad)
            self.assertEqual(board.find(board.load_board(), iid)["phase"], src)

    def test_unknown_phase_rejected(self):
        iid = board.add_item("task", "t", None)
        with self.assertRaises(ValueError):
            board.set_phase(iid, "done")    # done is a status, not a phase
        with self.assertRaises(ValueError):
            board.set_phase(iid, "READY")

    def test_done_clears_phase(self):
        iid = board.add_item("task", "t", None)
        board.set_meeseeks_pid(iid, "card-%d" % iid)
        board.set_phase(iid, "ready")
        board.set_phase(iid, "in_progress")
        board.set_phase(iid, "review")
        code, _o, _e = run(["done", str(iid)])
        self.assertEqual(code, 0)
        it = board.find(board.load_board(), iid)
        self.assertEqual(it["status"], "done")
        self.assertIsNone(it["phase"])

    def test_meeseeks_pid_valid_and_invalid(self):
        iid = board.add_item("task", "t", None)
        board.set_meeseeks_pid(iid, "card-42")
        it = board.find(board.load_board(), iid)
        self.assertEqual(it["meeseeks_pid"], "card-42")
        for bad in ("", None, "Card-42", "-x", "a"*50, "has space"):
            with self.assertRaises(ValueError):
                board.set_meeseeks_pid(iid, bad)
        it = board.find(board.load_board(), iid)
        self.assertEqual(it["meeseeks_pid"], "card-42")


# BoardCopySyncTests was LIFTED OUT 2026-08-31 (S-BRIDGE-1 S6) into
# shared/lib/internal_gates.py. It asserted byte-identity of the two
# maintained board.py copies by absolute path, one of which is the
# operator-only copy that no published edition creates. Keeping it here
# would have shipped a test that skips vacuously everywhere except the
# development host, and would have put an operator-only path into the
# public harness. The invariant is NOT dropped: it is enforced as an
# internal gate, run by hand, because it is a property of that host under
# Ruling A (2026-08-27), not of the software.
#
# This file deliberately names NO absolute install path -- that is what
# makes it publishable and relocatable.


if __name__ == "__main__":
    unittest.main(verbosity=2)
