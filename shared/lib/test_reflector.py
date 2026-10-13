#!/usr/bin/env python3
"""Deterministic tests for the board <-> meeseeks reflector.

No model calls, no jail, no real ledger or board: MEESEEKS_ROOT and
TASKBOARD_PATH are redirected into a temp tree and step_once is stubbed.
"""
import functools
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# RC6 D-1 (operator ruling 2026-10-09 (a)): the receipt verifier lives in
# shared/ctl, which only containment provisioning installs. A receipt-path
# case runs UNCHANGED when shared/ctl is present; when it is absent the case
# cannot reach its receipt path, so it is skipped and UnprovisionedCtlTests
# asserts the fail-closed refusal instead. reflector.py is not modified.
def ctl_present(reflector):
    return os.path.isdir(os.path.dirname(reflector.TBRECEIPT))


def needs_ctl(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        if not ctl_present(self.reflector):
            self.skipTest("shared/ctl absent (not provisioned): receipt path "
                          "unreachable; UnprovisionedCtlTests asserts the "
                          "refusal")
        return fn(self, *a, **kw)
    return wrapper


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reflector-")
        os.environ["TASKBOARD_PATH"] = os.path.join(self.tmp, "board.json")
        os.environ["MEESEEKS_ROOT"] = os.path.join(self.tmp, "meeseeks")
        # BOARD-TRUTH L (Moe flag 1): tests never read the operator's real
        # migration switch; it is pinned to an absent fixture path.
        os.environ["BOARD_L_MIGRATED_FLAG"] = os.path.join(
            self.tmp, "l-migrated")
        for mod in ("board", "meeseeks", "reflector"):
            sys.modules.pop(mod, None)
        import reflector  # inserts its own dir + the meeseeks dir on sys.path
        self.reflector = reflector
        self.board = reflector.board
        self.meeseeks = reflector.meeseeks
        self._real_step = self.meeseeks.step_once
        self.meeseeks.step_once = lambda con, pid: (False, "stubbed: no progress")
        # CONTAINMENT T2 rev 6 ruling 1: the reflector drives only a linkage
        # backed by a USED commission receipt. The fixture store is a temp
        # file (treebeard-owned), so the root-ownership check is stubbed HERE
        # ONLY; DriveAuthorityTests exercise the real one. link_card() writes
        # the receipts tb-commission would.
        self.receipts = os.path.join(self.tmp, "board.receipts")
        open(self.receipts, "w").close()
        reflector.RECEIPTS = self.receipts
        self._real_root_only = reflector._root_only
        reflector._root_only = lambda path: None

    def tearDown(self):
        self.meeseeks.step_once = self._real_step
        for k in ("TASKBOARD_PATH", "MEESEEKS_ROOT",
                  "BOARD_L_MIGRATED_FLAG"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ready_card(self, text="Build a thing per the AC."):
        iid = self.board.add_item("task", text, None)
        self.board.set_phase(iid, "ready")
        return iid

    def link_card(self, iid):
        """FIXTURE ONLY: the state the operator's tb-commission leaves (project
        card-N, linked, started by the operator). CONTAINMENT T2 rev 4 ruling
        B: the reflector has NO equivalent; NoCommissionTests prove it."""
        import argparse
        pid = self.reflector.pid_for(iid)
        f = os.path.join(self.tmp, "task-%d.txt" % iid)
        with open(f, "w") as fh:
            fh.write(self.board.find(self.board.load_board(), iid)["text"])
        self.meeseeks.cmd_new(argparse.Namespace(
            name=pid, by="operator (tb-commission)", task=None, task_file=f))
        rev = self.board.find(self.board.load_board(), iid)["rev"]
        h = self.receipt("authorize-commission",
                         {"id": iid, "op": "authorize-commission", "rev": rev,
                          "to": "QUEUED", "project": pid,
                          "session": {"pid": 4242, "start": 1}})
        self.board.set_meeseeks_pid(iid, pid, actor="operator")
        self.board.set_phase(iid, "in_progress", actor="operator")
        self.receipt("commission-link", {"id": iid, "op": "commission-link",
                                         "project": pid, "consumes": h})
        return pid

    # T2 rev 8 (operator P1): the only authority class -- root-gated, recorded
    OPERATOR = {"exec_uid": 981, "exec_user": "tbctl", "invoker_uid": 0,
                "invoker_user": "root", "principal": "root-gated",
                "authority": "recorded", "actor": "operator",
                "actor_basis": "recorded: root-gated"}
    # the pre-rev-8 shape: claims "authenticated"; must NEVER count
    LEGACY = {"exec_uid": 981, "exec_user": "tbctl", "invoker_uid": 1001,
              "invoker_user": "tbop", "principal": "operator",
              "actor": "operator", "actor_basis": "authenticated"}

    def receipt(self, op, result, decision="accepted", identity=None,
                ts="2026-10-02T00:00:00Z"):
        """Append a record exactly as board_writer/tbreceipt write it (hash
        chained, verify_chain-compatible). Returns its hash."""
        import hashlib
        import json
        prev = "0" * 64
        with open(self.receipts) as fh:
            for line in fh:
                if line.strip():
                    prev = json.loads(line)["hash"]
        payload = {"ts": ts, "op": op,
                   "decision": decision, "caller_uid": 1001,
                   "args_hash": "0" * 64, "before": None, "after": None,
                   "detail": "fixture", "prev": prev,
                   "identity": dict(self.OPERATOR if identity is None
                                    else identity),
                   "result": result}
        pj = json.dumps(payload, sort_keys=True)
        h = hashlib.sha256(prev.encode("ascii") + b"\n"
                           + pj.encode("utf-8")).hexdigest()
        with open(self.receipts, "a") as fh:
            fh.write(json.dumps(dict(payload, hash=h), sort_keys=True) + "\n")
        return h

    def projects(self, pid):
        con = self.meeseeks.connect()
        return con.execute("SELECT COUNT(*) c FROM projects WHERE pid=?",
                           (pid,)).fetchone()["c"]

    def set_states(self, pid, states):
        """Force section states directly; the factory is not under test."""
        con = self.meeseeks.connect()
        con.execute("DELETE FROM sections WHERE pid=?", (pid,))
        for i, st in enumerate(states, 1):
            con.execute(
                "INSERT INTO sections(pid,sid,parent,depth,title,state,"
                "ladder,cycles,misfires,created,updated) "
                "VALUES(?,?,?,?,?,?,0,0,0,?,?)",
                (pid, "s%02d" % i, None, 0, "t", st,
                 self.meeseeks.utcnow(), self.meeseeks.utcnow()))


class NoCommissionTests(Base):
    """CONTAINMENT T2 rev 4, ruling B (Tex): the reflector cannot commission.
    Commissioning is the operator's tb-commission only; board state of any
    shape -- ready, forged, rewound -- never makes the reflector create or
    link a project."""

    def untouched(self, iid):
        it = self.board.find(self.board.load_board(), iid)
        self.assertIsNone(it.get("meeseeks_pid"))
        self.assertIsNone(it.get("exec_ref"))
        self.assertEqual(self.projects(self.reflector.pid_for(iid)), 0)

    def test_no_commissioning_surface(self):
        for name in ("commission", "eligible", "commission_gate",
                     "load_receipts", "COMMISSION_CAP", "CommissionRefused",
                     "COMMISSIONER"):
            self.assertFalse(hasattr(self.reflector, name), name)
        src = open(self.reflector.__file__).read()
        for call in ("cmd_new(", "set_meeseeks_pid(", '"link"',
                     '"queue"', "tempfile", "import subprocess", "sudo",
                     "commission_op", "board_writer", "os.system"):
            self.assertFalse(call in src, "reflector source has %r" % call)

    def test_start_refuses_a_ready_card(self):
        iid = self.ready_card()
        with self.assertRaises(self.reflector.ReflectorError) as cm:
            self.reflector.run_card(iid, do_drive=True)
        self.assertIn("tb-commission", str(cm.exception))
        self.untouched(iid)
        self.assertEqual(self.board.find(self.board.load_board(), iid)["phase"],
                         "ready")

    def test_cli_start_refuses(self):
        iid = self.ready_card()
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.main(["start", str(iid)])
        self.untouched(iid)

    def test_tick_reports_and_never_commissions(self):
        ids = [self.ready_card() for _ in range(3)]
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        for iid in ids:
            self.untouched(iid)
            self.assertTrue(any("card %d QUEUED without a project: awaiting "
                                "the operator's tb-commission" % iid in ln
                                for ln in lines), lines)

    def test_forged_ready_card_never_commissioned(self):
        import json
        iid = self.board.add_item("task", "t", None)
        path = os.environ["TASKBOARD_PATH"]
        b = json.load(open(path))
        for c in b:
            if c["id"] == iid:
                c.update(phase="ready", by="operator", source="operator")
        json.dump(b, open(path, "w"))
        self.reflector.tick()
        self.untouched(iid)

    def test_rewound_board_is_never_recommissioned(self):
        # Ruling B proof item 2 (reflector side): the card was commissioned;
        # board.json is then rewound to its QUEUED, unlinked bytes.
        import json
        iid = self.ready_card()
        path = os.environ["TASKBOARD_PATH"]
        queued = open(path).read()
        pid = self.link_card(iid)
        with open(path, "w") as fh:
            fh.write(queued)
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        it = self.board.find(self.board.load_board(), iid)
        self.assertIsNone(it.get("meeseeks_pid"))
        self.assertEqual(self.projects(pid), 1)        # the original only
        self.reflector.tick(dry_run=True)
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.run_card(iid)
        self.assertIsNone(self.board.find(self.board.load_board(),
                                          iid).get("meeseeks_pid"))
        self.assertEqual(json.loads(open(path).read()), json.loads(queued))

    def test_dry_run_reports_awaiting_operator(self):
        iid = self.ready_card()
        lines, faults = self.reflector.tick(dry_run=True)
        self.assertEqual(faults, 0)
        self.assertTrue(any("card %d QUEUED without a project" % iid in ln
                            for ln in lines), lines)
        self.untouched(iid)


class DriveAuthorityTests(Base):
    """CONTAINMENT T2 rev 6, ruling 1 (Tex): a linked project is DRIVEN only
    when its exact (card, project pid) linkage is backed by a USED commission
    receipt. Every other case is not driven, is reported, and changes
    nothing; unreadable receipts are UNKNOWN (a fault) and drive nothing."""

    def _trap_drive(self):
        calls = []
        real = self.reflector.drive
        self.addCleanup(setattr, self.reflector, "drive", real)
        self.reflector.drive = lambda pid, steps=None: calls.append(
            (pid, steps)) or 1
        return calls

    def self_link(self, iid, pid=None):
        """The D1 bypass: treebeard creates a project and links it by a
        direct board write -- no tb-commission, no writer."""
        import argparse
        import json
        pid = pid or self.reflector.pid_for(iid)
        f = os.path.join(self.tmp, "self-%d.txt" % iid)
        open(f, "w").write("self-assigned work")
        if not self.projects(pid):
            self.meeseeks.cmd_new(argparse.Namespace(
                name=pid, by="treebeard", task=None, task_file=f))
        path = os.environ["TASKBOARD_PATH"]
        b = json.load(open(path))
        for c in b:
            if c["id"] == iid:
                c.update(phase="in_progress", meeseeks_pid=pid,
                         exec_ref={"kind": "meeseeks", "id": pid},
                         by="operator")
        json.dump(b, open(path, "w"))
        return pid

    def not_driven(self, iid, why):
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(calls, [], lines)
        self.assertEqual(faults, 0, lines)
        self.assertTrue(any(ln.startswith("[tick] NOT driven card %d: linkage "
                                          "has no USED commission receipt" % iid)
                            and why in ln for ln in lines), lines)
        with self.assertRaises(self.reflector.ReflectorError) as cm:
            self.reflector.run_card(iid, do_drive=True)
        self.assertIn("NOT driven", str(cm.exception))
        self.assertEqual(calls, [])
        return lines

    @needs_ctl
    def test_properly_commissioned_card_is_driven(self):
        iid = self.ready_card()
        self.link_card(iid)
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(calls, [("card-%d" % iid, 4)], lines)
        self.assertEqual(faults, 0)
        lines, _f = self.reflector.tick(dry_run=True)
        self.assertTrue(any("WOULD drive card %d" % iid in ln for ln in lines))

    @needs_ctl
    def test_self_linked_project_without_receipt(self):
        iid = self.ready_card()
        self.self_link(iid)
        self.not_driven(iid, "no commission receipt for card %d" % iid)
        lines, _f = self.reflector.tick(dry_run=True)
        self.assertFalse(any("WOULD drive card %d" % iid in ln for ln in lines))

    @needs_ctl
    def test_receipt_for_the_same_card_but_a_different_pid(self):
        iid = self.ready_card()
        self.link_card(iid)                       # receipts for card-N ...
        self.self_link(iid, pid="selfmade-%d" % iid)   # ... linked elsewhere
        self.not_driven(iid, "is not card-%d" % iid)

    @needs_ctl
    def test_used_receipt_names_another_project(self):
        iid = self.ready_card()
        pid = self.reflector.pid_for(iid)
        h = self.receipt("authorize-commission", {"id": iid, "rev": 2,
                                                  "project": "card-999"})
        self.receipt("commission-link", {"id": iid, "project": "card-999",
                                         "consumes": h})
        self.self_link(iid)
        self.not_driven(iid, "is for project 'card-999'")
        self.assertEqual(self.reflector.pid_for(iid), pid)

    @needs_ctl
    def test_unused_receipt(self):
        iid = self.ready_card()
        self.receipt("authorize-commission", {"id": iid, "rev": 2,
                                              "project": "card-%d" % iid})
        self.self_link(iid)
        self.not_driven(iid, "no USED commission receipt")

    @needs_ctl
    def test_malformed_receipts(self):
        svc = dict(self.OPERATOR, principal="service", actor="reflector",
                   actor_basis="recorded", invoker_user="treebeard")
        cases = (
            ("no consume named", lambda iid, pid, h: {"id": iid, "project": pid}, None),
            ("not an earlier receipt", lambda iid, pid, h: {"id": iid, "project": pid,
                                                            "consumes": "f" * 64}, None),
            ("not a root-gated writer receipt", lambda iid, pid, h: {
                "id": iid, "project": pid, "consumes": h}, svc),
            ("not a root-gated writer receipt", lambda iid, pid, h: {
                "id": iid, "project": pid, "consumes": h}, self.LEGACY),
            ("not a root-gated writer receipt", lambda iid, pid, h: {
                "id": iid, "project": pid, "consumes": h},
             dict(self.OPERATOR, authority="authenticated")),
        )
        for why, link, ident in cases:
            with self.subTest(why):
                iid = self.ready_card()
                pid = self.reflector.pid_for(iid)
                h = self.receipt("authorize-commission",
                                 {"id": iid, "rev": 2, "project": pid})
                self.receipt("commission-link", link(iid, pid, h), identity=ident)
                self.self_link(iid)
                self.not_driven(iid, why)
        with self.subTest("consume of another card"):
            other = self.ready_card()
            iid = self.ready_card()
            pid = self.reflector.pid_for(iid)
            h = self.receipt("authorize-commission",
                             {"id": other, "rev": 2, "project": pid})
            self.receipt("commission-link", {"id": iid, "project": pid,
                                             "consumes": h})
            self.self_link(iid)
            self.not_driven(iid, "is not this card's root-gated authorization")
        with self.subTest("legacy authenticated-shaped consume"):
            iid = self.ready_card()
            pid = self.reflector.pid_for(iid)
            h = self.receipt("authorize-commission",
                             {"id": iid, "rev": 2, "project": pid},
                             identity=self.LEGACY)
            self.receipt("commission-link", {"id": iid, "project": pid,
                                             "consumes": h})
            self.self_link(iid)
            self.not_driven(iid, "is not this card's root-gated authorization")
        with self.subTest("one consume spent twice (single use)"):
            iid = self.ready_card()
            pid = self.reflector.pid_for(iid)
            h = self.receipt("authorize-commission",
                             {"id": iid, "rev": 2, "project": pid})
            for _ in range(2):
                self.receipt("commission-link", {"id": iid, "project": pid,
                                                 "consumes": h})
            self.self_link(iid)
            self.not_driven(iid, "REUSE")
        with self.subTest("link dated before its consume (order/time)"):
            iid = self.ready_card()
            pid = self.reflector.pid_for(iid)
            h = self.receipt("authorize-commission",
                             {"id": iid, "rev": 2, "project": pid},
                             ts="2026-10-03T00:00:00Z")
            self.receipt("commission-link", {"id": iid, "project": pid,
                                             "consumes": h},
                         ts="2026-10-02T23:59:59Z")
            self.self_link(iid)
            self.not_driven(iid, "before the authorization it spends")
        with self.subTest("denied link"):
            iid = self.ready_card()
            pid = self.reflector.pid_for(iid)
            h = self.receipt("authorize-commission",
                             {"id": iid, "rev": 2, "project": pid})
            self.receipt("commission-link", {"id": iid, "project": pid,
                                             "consumes": h}, decision="denied")
            self.self_link(iid)
            self.not_driven(iid, "no USED commission receipt")

    def unknown(self, iid, why):
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(calls, [], lines)
        self.assertGreaterEqual(faults, 1)
        self.assertTrue(any(ln.startswith("[tick] NOT driven card %d: drive "
                                          "authority UNKNOWN" % iid)
                            and why in ln for ln in lines), lines)
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.run_card(iid, do_drive=True)
        self.assertEqual(calls, [])

    def test_unreadable_receipts_are_unknown(self):
        iid = self.ready_card()
        self.link_card(iid)
        os.unlink(self.receipts)
        self.reflector._root_only = self._real_root_only
        self.unknown(iid, "unreadable")

    def test_untrusted_store_is_unknown(self):
        iid = self.ready_card()
        self.link_card(iid)
        self.reflector._root_only = self._real_root_only    # the REAL check
        self.unknown(iid, "not root-owned")

    @needs_ctl
    def test_broken_chain_is_unknown(self):
        import json
        iid = self.ready_card()
        self.link_card(iid)
        lines = open(self.receipts).read().splitlines()
        r = json.loads(lines[-1])
        r["detail"] = "tampered"
        open(self.receipts, "w").write("\n".join(lines[:-1] + [json.dumps(r, sort_keys=True)]) + "\n")
        self.unknown(iid, "BROKEN")

    def test_refusal_writes_nothing(self):
        iid = self.ready_card()
        self.self_link(iid)
        before = open(os.environ["TASKBOARD_PATH"], "rb").read()
        rb = open(self.receipts, "rb").read()
        self._trap_drive()
        self.reflector.tick()
        self.assertEqual(open(self.receipts, "rb").read(), rb)
        self.assertEqual(open(os.environ["TASKBOARD_PATH"], "rb").read(), before)


class MappingTests(Base):
    def _commissioned(self):
        iid = self.ready_card()
        return iid, self.link_card(iid)

    def test_empty_sections_refuses(self):
        iid, pid = self._commissioned()
        con = self.meeseeks.connect()
        con.execute("DELETE FROM sections WHERE pid=?", (pid,))
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.project_phase(pid)
        it = self.board.find(self.board.load_board(), iid)
        self.assertEqual(it["phase"], "in_progress")  # unmutated

    def test_blocked_never_review(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["BLOCKED", "AWAITING_TREEBEARD"])
        self.assertEqual(self.reflector.project_phase(pid), "blocked")
        self.assertEqual(self.reflector.reflect(iid), "blocked")

    def test_all_awaiting_is_review(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["AWAITING_TREEBEARD", "DONE"])
        self.assertEqual(self.reflector.project_phase(pid), "review")
        self.assertEqual(self.reflector.reflect(iid), "review")

    def test_unknown_state_never_review(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["SOME_FUTURE_STATE"])
        self.assertEqual(self.reflector.project_phase(pid), "in_progress")

    def test_running_states_in_progress(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["TESTING", "AWAITING_TREEBEARD"])
        self.assertEqual(self.reflector.project_phase(pid), "in_progress")

    def test_all_done_closes_card(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["DONE", "DONE"])
        self.assertEqual(self.reflector.reflect(iid), "done")
        it = self.board.find(self.board.load_board(), iid)
        self.assertEqual(it["status"], "done")
        self.assertIsNone(it["phase"])

    def test_reflect_idempotent(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["AWAITING_TREEBEARD"])
        self.assertEqual(self.reflector.reflect(iid), "review")
        self.assertEqual(self.reflector.reflect(iid), "review")

    @needs_ctl
    def test_run_card_reentrant(self):
        iid = self.ready_card()
        pid = self.link_card(iid)
        self.reflector.run_card(iid)
        self.set_states(pid, ["AWAITING_TREEBEARD"])
        self.assertEqual(self.reflector.run_card(iid), "review")
        con = self.meeseeks.connect()
        n = con.execute("SELECT COUNT(*) c FROM projects WHERE pid=?",
                        (pid,)).fetchone()["c"]
        self.assertEqual(n, 1)


class SyncSafetyTests(Base):
    """sync is reflect-only: it must never commission (2026-08-27 fix).
    Since T2 rev 4 ruling B, neither does start."""

    def _commissioned(self):
        iid = self.ready_card()
        return iid, self.link_card(iid)

    def test_sync_does_not_commission_ready_card(self):
        iid = self.ready_card()
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.run_card(iid, do_drive=False)
        con = self.meeseeks.connect()
        n = con.execute("SELECT COUNT(*) c FROM projects WHERE pid=?",
                        (self.reflector.pid_for(iid),)).fetchone()["c"]
        self.assertEqual(n, 0)
        it = self.board.find(self.board.load_board(), iid)
        self.assertIsNone(it["meeseeks_pid"])
        self.assertEqual(it["phase"], "ready")

    def test_sync_reflects_commissioned_card(self):
        iid, pid = self._commissioned()
        self.set_states(pid, ["AWAITING_TREEBEARD"])
        self.assertEqual(self.reflector.run_card(iid, do_drive=False),
                         "review")

    def test_start_does_not_commission(self):
        iid = self.ready_card()
        with self.assertRaises(self.reflector.ReflectorError):
            self.reflector.run_card(iid, do_drive=True)
        it = self.board.find(self.board.load_board(), iid)
        self.assertIsNone(it["meeseeks_pid"])


class TickTests(Base):
    # S-BRIDGE-1 autonomous tick. Every drive-set ruling gets a test, and
    # every write path gets a negative.

    def _ready(self):
        iid = self.board.add_item("task", "t", None)
        self.board.set_phase(iid, "ready")
        return iid

    def _linked(self, section_state=None, phase=None):
        iid = self._ready()
        self.link_card(iid)                     # tb-commission's result
        if section_state:
            con = self.meeseeks.connect()
            con.execute("UPDATE sections SET state=? WHERE pid=?",
                        (section_state, self.reflector.pid_for(iid)))
            con.commit()
        if phase:
            self.board.set_phase(iid, phase)
        return iid

    def _state(self, iid):
        it = self.board.find(self.board.load_board(), iid)
        return it.get("status"), it.get("phase")

    def _trap_drive(self):
        calls = []
        real = self.reflector.drive
        self.addCleanup(setattr, self.reflector, "drive", real)

        def fake(pid, steps=None):
            calls.append((pid, steps))
            return 1
        self.reflector.drive = fake
        return calls

    def test_tick_commissions_none_of_many_ready_cards(self):
        ids = [self._ready() for _ in range(3)]
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        linked = [i for i in ids if self.board.find(
            self.board.load_board(), i).get("meeseeks_pid")]
        self.assertEqual(linked, [])
        self.assertEqual(calls, [])
        self.assertEqual(faults, 0)
        self.assertTrue(any("3 awaiting operator" in ln for ln in lines))

    @needs_ctl
    def test_linked_card_is_driven_with_the_default_budget(self):
        os.environ.pop("REFLECTOR_TICK_STEPS", None)
        iid = self._linked()
        calls = self._trap_drive()
        self.reflector.tick()
        self.assertEqual(calls, [("card-%d" % iid, 4)])

    @needs_ctl
    def test_drive_set_excludes_review_blocked_and_returned(self):
        rv = self._linked("AWAITING_TREEBEARD")
        bl = self._linked("BLOCKED")
        rt = self._linked(None, phase="returned")
        ip = self._linked()
        calls = self._trap_drive()
        self.reflector.tick()
        self.assertEqual([c[0] for c in calls], ["card-%d" % ip])
        self.assertEqual(self._state(rv)[1], "review")
        self.assertEqual(self._state(bl)[1], "blocked")
        self.assertEqual(self._state(rt)[1], "returned")

    def test_returned_card_is_never_auto_resumed(self):
        # project_phase() cannot return `returned`; without the hold, an
        # unguarded reflect() would write in_progress and resume spending.
        rt = self._linked(None, phase="returned")
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(self._state(rt)[1], "returned")
        self.assertEqual(calls, [])
        self.assertEqual(faults, 0)
        self.assertTrue(any("(held)" in ln for ln in lines))

    def test_review_card_reaches_done_after_gate_two(self):
        # The human approved at Gate 2 and the section is DONE. The next tick
        # must carry the card to done with NO manual sync.
        iid = self._linked("AWAITING_TREEBEARD")
        self._trap_drive()
        self.reflector.tick()
        self.assertEqual(self._state(iid), ("open", "review"))
        con = self.meeseeks.connect()
        con.execute("UPDATE sections SET state=? WHERE pid=?",
                    ("DONE", self.reflector.pid_for(iid)))
        con.commit()
        self.reflector.tick()
        self.assertEqual(self._state(iid), ("done", None))

    @needs_ctl
    def test_post_drive_reconcile_reflects_what_the_tick_caused(self):
        iid = self._linked()

        def step(con, pid):
            con.execute("UPDATE sections SET state=? WHERE pid=?",
                        ("AWAITING_TREEBEARD", pid))
            con.commit()
            return False, "gated"
        self.meeseeks.step_once = step
        self.reflector.tick()
        self.assertEqual(self._state(iid), ("open", "review"))

    @needs_ctl
    def test_step_cap_default_four_and_env_override(self):
        os.environ.pop("REFLECTOR_TICK_STEPS", None)
        self.assertEqual(self.reflector.tick_steps(), 4)
        os.environ["REFLECTOR_TICK_STEPS"] = "2"
        self.addCleanup(os.environ.pop, "REFLECTOR_TICK_STEPS", None)
        self.assertEqual(self.reflector.tick_steps(), 2)
        iid = self._linked()
        calls = self._trap_drive()
        self.reflector.tick()
        self.assertEqual(calls, [("card-%d" % iid, 2)])

    @needs_ctl
    def test_explicit_steps_argument_wins(self):
        iid = self._linked()
        calls = self._trap_drive()
        self.reflector.tick(steps=7)
        self.assertEqual(calls, [("card-%d" % iid, 7)])

    @needs_ctl
    def test_dry_run_writes_nothing_and_drives_nothing(self):
        self._ready()
        self._linked()
        before = open(os.environ["TASKBOARD_PATH"], "rb").read()
        calls = self._trap_drive()
        lines, faults = self.reflector.tick(dry_run=True)
        self.assertEqual(open(os.environ["TASKBOARD_PATH"], "rb").read(),
                         before)
        self.assertEqual(calls, [])
        self.assertEqual(faults, 0)
        self.assertTrue(any("awaiting the operator" in ln for ln in lines))
        self.assertTrue(any("WOULD drive" in ln for ln in lines))

    @needs_ctl
    def test_tick_never_crosses_gate_two(self):
        def forbidden(*a, **k):
            raise AssertionError("tick crossed Gate 2")
        for name in ("cmd_review", "cmd_retask", "cmd_retro", "cmd_close"):
            if hasattr(self.meeseeks, name):
                self.addCleanup(setattr, self.meeseeks, name,
                                getattr(self.meeseeks, name))
                setattr(self.meeseeks, name, forbidden)
        self._linked("AWAITING_TREEBEARD")
        self._linked()
        self._trap_drive()
        _lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)

    def test_a_fault_on_one_card_does_not_stop_the_others(self):
        bad = self._linked()
        good = self._linked("AWAITING_TREEBEARD")
        con = self.meeseeks.connect()
        con.execute("DELETE FROM projects WHERE pid=?",
                    (self.reflector.pid_for(bad),))
        con.commit()
        self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertGreaterEqual(faults, 1)
        self.assertTrue(any("FAULT reconcile card %d" % bad in ln
                            for ln in lines))
        self.assertEqual(self._state(good)[1], "review")

    def test_done_and_unphased_cards_are_ignored(self):
        done = self._linked("DONE")
        self._trap_drive()
        self.reflector.tick()
        self.assertEqual(self._state(done), ("done", None))
        plain = self.board.add_item("task", "no phase", None)
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        self.assertFalse(any("card %d" % plain in ln for ln in lines))
        self.assertFalse(any("card %d" % done in ln for ln in lines))


class LifecycleTests(Base):
    """BOARD-TRUTH L: M4, M5, the gated fail-close, transition routing, the
    reconcile stamp. Reuses TickTests' fixture helpers (not its tests)."""
    _ready = TickTests._ready
    _linked = TickTests._linked
    _state = TickTests._state
    _trap_drive = TickTests._trap_drive

    def _set(self, iid, state, reason=None, project=None):
        con = self.meeseeks.connect()
        con.execute("UPDATE sections SET state=?, blocked_reason=? WHERE "
                    "pid=?", (state, reason, self.reflector.pid_for(iid)))
        if project:
            con.execute("UPDATE projects SET state=? WHERE pid=?",
                        (project, self.reflector.pid_for(iid)))
        con.commit()

    def _bytes(self):
        with open(os.environ["TASKBOARD_PATH"], "rb") as fh:
            return fh.read()

    # -- M4 ---------------------------------------------------------------
    def test_m4_unreviewable_is_terminal_and_never_driven(self):
        iid = self._linked()
        self._set(iid, "UNREVIEWABLE")
        self.assertEqual(self.reflector.project_phase(
            self.reflector.pid_for(iid)), "failed")
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0, lines)
        self.assertEqual(calls, [], "a terminal project was driven")
        self.assertEqual(self._state(iid), ("open", "in_progress"))
        self.assertTrue(any("fail-close OFF" in ln for ln in lines), lines)

    # -- M5 ---------------------------------------------------------------
    def test_m5_finished_parent_with_open_child_does_not_kill_the_tick(self):
        parent = self._linked()
        self.board.add_item("task", "open child", parent)
        self._set(parent, "DONE")
        later = self._linked("AWAITING_TREEBEARD")     # reconciled AFTER it
        self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0, lines)
        self.assertEqual(self._state(parent), ("open", "in_progress"))
        self.assertTrue(any("card %d" % parent in ln and "held" in ln
                            for ln in lines), lines)
        self.assertEqual(self._state(later)[1], "review",
                         "a later card was skipped")

    def test_m5_any_board_refusal_is_per_card(self):
        bad = self._linked()
        good = self._linked("AWAITING_TREEBEARD")
        real = self.board.set_phase

        def boom(iid, *a, **k):
            if iid == bad:
                raise self.board.TransitionRefused("injected refusal")
            return real(iid, *a, **k)
        self.board.set_phase = boom
        self.addCleanup(setattr, self.board, "set_phase", real)
        self._set(bad, "AWAITING_TREEBEARD")
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 1, lines)
        self.assertEqual(self._state(good)[1], "review")

    # -- Moe flag 1: fail-close gated OFF until the migration ---------------
    def test_fail_close_off_does_not_move_a_card58_shaped_card(self):
        iid = self._linked(phase="blocked")
        self._set(iid, "BLOCKED",
                  "FACTORY_EXHAUSTED: stages exhausted at github")
        self.assertFalse(self.reflector.fail_close_enabled())
        before = self._bytes()
        self._trap_drive()
        for _ in range(3):
            lines, faults = self.reflector.tick()
            self.assertEqual(faults, 0, lines)
        self.assertEqual(self._bytes(), before, "card 58 moved pre-migration")
        self.assertIsNone(self.board.read_meta(),
                          "reconcile stamp written before the migration")

    def test_fail_close_on_writes_failed_with_reason_and_exec_ref(self):
        iid = self._linked(phase="blocked")
        self._set(iid, "BLOCKED", "FACTORY_EXHAUSTED: x")
        open(os.environ["BOARD_L_MIGRATED_FLAG"], "w").close()
        self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0, lines)
        c = self.board.find(self.board.load_board(), iid)
        self.assertEqual((c["status"], c["outcome"], c["by"]),
                         ("closed", "failed", "reflector"))
        self.assertIn("FACTORY_EXHAUSTED", c["reason"])
        self.assertEqual(c["exec_ref"]["id"], self.reflector.pid_for(iid))
        # FAILED is finished: never reconciled again (linked_active == open)
        lines, _f = self.reflector.tick()
        self.assertFalse(any("card %d" % iid in ln for ln in lines), lines)

    def test_terminal_project_state_is_failed_not_blocked(self):
        iid = self._linked()
        self._set(iid, "QUEUED", project="CLOSED_BLOCKED")
        self.assertEqual(self.reflector.project_phase(
            self.reflector.pid_for(iid)), "failed")

    # -- routing + stamp ------------------------------------------------------
    def test_reflector_writes_are_transitions_stamped_reflector(self):
        iid = self._linked("AWAITING_TREEBEARD")
        self._trap_drive()
        self.reflector.tick()
        c = self.board.find(self.board.load_board(), iid)
        self.assertEqual(c["by"], "reflector")
        self.assertEqual([h["op"] for h in c["history"]][-3:],
                         ["link", "start", "request_review"])

    def test_closed_card_is_not_reconciled(self):
        iid = self._linked()
        self.board.transition(iid, "close", "operator", outcome="superseded",
                              reason="r")
        lines, _f = self.reflector.tick()
        self.assertFalse(any("card %d" % iid in ln for ln in lines))

    def test_reconcile_stamp(self):
        held = self._linked(phase="blocked")
        self._set(held, "BLOCKED", "FACTORY_EXHAUSTED: x")
        self._linked("AWAITING_TREEBEARD")
        self._trap_drive()
        self.reflector.tick()
        self.assertIsNone(self.board.read_meta(), "stamp before migration")
        open(os.environ["BOARD_L_MIGRATED_FLAG"], "w").close()
        self.reflector.tick(dry_run=True)
        self.assertIsNone(self.board.read_meta(), "dry run wrote the stamp")
        lines, _f = self.reflector.tick()
        m = self.board.read_meta()
        self.assertEqual(m["written_by"], "reflector")
        self.assertEqual(m["board_sha256"], self.board.board_digest())
        self.assertTrue(set(m) <= set(self.board.META_KEYS) | {
            "schema", "board", "written_by"}, m)
        self.assertEqual(m["faults"], 0)
        self.assertIsInstance(m["inconsistent"], list)


class TickLockTests(Base):
    # The no-overlap invariant belongs to the OPERATION, not to systemd:
    # the same command must have the same safety properties whoever ran it.
    # flock() treats two open file descriptions on one file independently
    # even within a process, so a second open() here is a faithful stand-in
    # for a competing tick.

    def _hold(self):
        """Take the lock the way a competing tick would; release on cleanup."""
        import fcntl
        fh = open(self.reflector.lock_path(), "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fh.close)
        return fh

    def _free(self):
        """True if the lock is acquirable right now."""
        import fcntl
        with open(self.reflector.lock_path(), "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return True
            except OSError:
                return False

    def _ready(self):
        """A drivable card: commissioned by the operator (fixture)."""
        iid = self.ready_card("t")
        self.link_card(iid)
        return iid

    def _trap_drive(self):
        calls = []
        real = self.reflector.drive
        self.addCleanup(setattr, self.reflector, "drive", real)
        self.reflector.drive = lambda pid, steps=None: calls.append(
            (pid, steps)) or 1
        return calls

    @needs_ctl
    def test_first_tick_acquires_the_lock_and_works(self):
        iid = self._ready()
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        self.assertEqual(calls, [("card-%d" % iid, 4)])
        self.assertFalse(any("already running" in ln for ln in lines))
        self.assertTrue(os.path.exists(self.reflector.lock_path()))

    def test_second_acquisition_is_a_busy_no_op_at_zero_faults(self):
        self._hold()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)          # busy is NOT a failure
        self.assertTrue(any("already running" in ln for ln in lines))
        self.assertEqual(len(lines), 1)      # and nothing else was reported

    def test_busy_tick_drives_nothing_writes_nothing(self):
        iid = self.ready_card("t")
        self._ready()
        before = open(os.environ["TASKBOARD_PATH"], "rb").read()
        self._hold()
        calls = self._trap_drive()
        _lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        self.assertEqual(calls, [])
        self.assertEqual(open(os.environ["TASKBOARD_PATH"], "rb").read(),
                         before)
        self.assertIsNone(self.board.find(
            self.board.load_board(), iid).get("meeseeks_pid"))

    def test_dry_run_takes_the_same_lock(self):
        # Otherwise a dry run reports on state a live tick is mutating.
        self._hold()
        lines, faults = self.reflector.tick(dry_run=True)
        self.assertEqual(faults, 0)
        self.assertTrue(any("already running" in ln for ln in lines))

    @needs_ctl
    def test_lock_is_acquirable_again_once_the_holder_exits(self):
        import fcntl
        fh = self._hold()
        lines, _f = self.reflector.tick()
        self.assertTrue(any("already running" in ln for ln in lines))
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
        self.assertTrue(self._free())
        iid = self._ready()
        calls = self._trap_drive()
        lines, faults = self.reflector.tick()
        self.assertEqual(faults, 0)
        self.assertEqual(calls, [("card-%d" % iid, 4)])

    def test_exception_inside_a_tick_does_not_strand_the_lock(self):
        boom = RuntimeError("deliberate fault inside the locked section")

        def explode(_items):
            raise boom
        real = self.reflector.linked_active
        self.addCleanup(setattr, self.reflector, "linked_active", real)
        self.reflector.linked_active = explode
        with self.assertRaises(RuntimeError):
            self.reflector.tick()
        self.assertTrue(self._free(), "lock stranded after an exception")


class UnprovisionedCtlTests(Base):
    """RC6 D-1: with shared/ctl absent (an unprovisioned install) the drive
    path refuses fail-closed, K1-class: no unhandled FileNotFoundError, drive
    authority UNKNOWN naming the missing verifier, a fault counted, nothing
    driven and nothing written. Skipped when shared/ctl is present, where the
    receipt-path cases above run instead."""

    def setUp(self):
        super().setUp()
        if ctl_present(self.reflector):
            self.skipTest("shared/ctl present: the receipt-path cases run")

    def _trap_drive(self):
        calls = []
        real = self.reflector.drive
        self.addCleanup(setattr, self.reflector, "drive", real)
        self.reflector.drive = lambda pid, steps=None: calls.append(
            (pid, steps)) or 1
        return calls

    def _snapshot(self):
        return (open(os.environ["TASKBOARD_PATH"], "rb").read(),
                open(self.receipts, "rb").read())

    def _refused(self, iid, why, dry_run=False):
        calls = self._trap_drive()
        before = self._snapshot()
        lines, faults = self.reflector.tick(dry_run=dry_run)
        self.assertEqual(calls, [], lines)
        self.assertGreaterEqual(faults, 1, lines)
        hit = [ln for ln in lines
               if ln.startswith("[tick] NOT driven card %d: drive authority "
                                "UNKNOWN" % iid)]
        self.assertEqual(len(hit), 1, lines)
        self.assertIn(why, hit[0])
        self.assertFalse(any(ln.startswith("[tick] WOULD drive")
                             or ln.startswith("[tick] drive card")
                             for ln in lines), lines)
        with self.assertRaises(self.reflector.DriveAuthorityUnknown) as cm:
            self.reflector.run_card(iid, do_drive=True)
        self.assertIn(why, str(cm.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_commissioned_card_is_not_driven(self):
        iid = self.ready_card()
        self.link_card(iid)
        self._refused(iid, self.reflector.TBRECEIPT)

    def test_dry_run_is_not_driven(self):
        iid = self.ready_card()
        self.link_card(iid)
        self._refused(iid, self.reflector.TBRECEIPT, dry_run=True)

    def test_real_paths_and_root_check_refuse(self):
        import importlib
        iid = self.ready_card()
        self.link_card(iid)
        importlib.reload(self.reflector)    # the shipped paths and check
        self.assertFalse(os.path.exists(self.reflector.RECEIPTS))
        self._refused(iid, "unreadable")

    def test_no_unhandled_file_not_found(self):
        try:
            self.reflector.drive_receipts()
        except self.reflector.DriveAuthorityUnknown as e:
            self.assertIn(self.reflector.TBRECEIPT, str(e))
        else:
            self.fail("drive_receipts() returned without shared/ctl")


class MeeseeksDirResolutionTests(unittest.TestCase):
    """PO ruling 3 (2026-08-31): TREEBEARD_BASE decides the DEFAULT Meeseeks
    location; an explicit MEESEEKS_DIR still wins. Asserted against the
    resolver, not the module-level constant, so no re-import is needed -- and
    a relocated base can be tested even though no meeseeks exists there.

    Placed ABOVE the __main__ guard deliberately: classes below it have
    silently never run twice in this codebase (test_career_packets,
    bin/test_board BoardV2Tests)."""

    def setUp(self):
        import reflector
        self.reflector = reflector
        self.saved = {k: os.environ.get(k)
                      for k in ("TREEBEARD_BASE", "MEESEEKS_DIR")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _resolve(self, **env):
        for k in ("TREEBEARD_BASE", "MEESEEKS_DIR"):
            os.environ.pop(k, None)
        os.environ.update(env)
        return self.reflector._meeseeks_dir()

    def test_default_is_the_documented_fallback(self):
        self.assertEqual(self._resolve(), "/opt/treebeard/shared/meeseeks")

    def test_default_tracks_treebeard_base(self):
        self.assertEqual(self._resolve(TREEBEARD_BASE="/srv/tb"),
                         os.path.join("/srv/tb", "shared", "meeseeks"))

    def test_explicit_override_wins_over_base(self):
        self.assertEqual(self._resolve(TREEBEARD_BASE="/srv/tb",
                                       MEESEEKS_DIR="/explicit/mk"),
                         "/explicit/mk")

    def test_override_wins_with_no_base_set(self):
        self.assertEqual(self._resolve(MEESEEKS_DIR="/explicit/mk"),
                         "/explicit/mk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
