#!/usr/bin/env python3
"""R6 regression: persona_writer --agent validation (F3b).

Run as treebeard, WITHOUT sudo:
    PW_PATH=/opt/treebeard/persona/persona_writer.py python3 -m unittest -v test_persona_writer_agent

Every CLI case invokes the writer directly (not via sudo), so a rejected
name must fail before any store is opened or any file is written. A name
that got past validation shows up as a different error (store access,
missing approval), which these tests treat as a failure.
"""
import importlib.util, json, os, subprocess, sys, tempfile, unittest

PW_PATH = os.environ.get("PW_PATH", "/opt/treebeard/persona/persona_writer.py")
PY = "/usr/bin/python3"

TRAVERSAL = ["../../../tmp/x", "clark/../han", "..", ".", "clark/", "a/b",
             "..%2f..%2ftmp%2fx", "clark\\..\\han"]
ABSOLUTE = ["/tmp/x", "/etc/treebeard/clark", "//tmp/x"]
UNKNOWN = ["mallory", "Clark", "clark\n", "clark ", "", "clark\x00",
           "x" * 64, "-clark", "_clark"]
VALID = "clark"


def _load():
    spec = importlib.util.spec_from_file_location("pw_under_test", PW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([PY, PW_PATH, *args], capture_output=True, text=True,
                       env=env, timeout=30)
    out = r.stdout.strip().splitlines()
    try:
        body = json.loads(out[-1]) if out else {}
    except ValueError:
        body = {"raw": r.stdout, "stderr": r.stderr}
    return r.returncode, body


def _forward(agent):
    return _run(f"--agent={agent}", "--dimension", "depth", "--value", "0.5",
                "--hypothesis", "h-r6-test", "--rationale", "r6", "--actor", "r6")


def _rollback(agent):
    return _run("--rollback", "--to-revision", "1", f"--agent={agent}",
                "--rationale", "r6", "--actor", "r6")


def _rejected(rc, body):
    return rc == 2 and "invalid agent name" in str(body.get("error", ""))


class ValidatorUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = _load()
        if not hasattr(cls.pw, "valid_agent"):
            raise unittest.SkipTest("writer has no valid_agent (unpatched)")

    def test_traversal_rejected(self):
        for a in TRAVERSAL:
            self.assertFalse(self.pw.valid_agent(a), a)

    def test_absolute_rejected(self):
        for a in ABSOLUTE:
            self.assertFalse(self.pw.valid_agent(a), a)

    def test_unknown_rejected(self):
        for a in UNKNOWN:
            self.assertFalse(self.pw.valid_agent(a), repr(a))

    def test_valid_accepted(self):
        self.assertTrue(self.pw.valid_agent(VALID))

    def test_non_string_rejected(self):
        for a in (None, 0, b"clark", ["clark"]):
            self.assertFalse(self.pw.valid_agent(a), repr(a))

    def test_fixed_charter_dir_not_env(self):
        self.assertEqual(self.pw.CHARTER_DIR, "/etc/treebeard")

    def test_fails_closed_on_bad_charter_entries(self):
        with tempfile.TemporaryDirectory() as d:
            # symlink charter -> rejected even though target exists
            os.symlink("/etc/treebeard/clark.charter.txt",
                       os.path.join(d, "sym.charter.txt"))
            self.assertFalse(self.pw.valid_agent("sym", d, require_root=False))
            # directory named like a charter -> rejected
            os.mkdir(os.path.join(d, "dirr.charter.txt"))
            self.assertFalse(self.pw.valid_agent("dirr", d, require_root=False))
            # regular charter not owned by root -> rejected when root required
            open(os.path.join(d, "mine.charter.txt"), "w").close()
            self.assertFalse(self.pw.valid_agent("mine", d, require_root=True))
            self.assertTrue(self.pw.valid_agent("mine", d, require_root=False))
            # unreadable/missing charter dir -> rejected, no exception
            self.assertFalse(self.pw.valid_agent("clark", os.path.join(d, "nope")))


class WriterCLI(unittest.TestCase):
    """End-to-end: the real script rejects before any store or file access."""

    def _assert_rejected_both_paths(self, agent):
        for name, fn in (("forward", _forward), ("rollback", _rollback)):
            rc, body = fn(agent)
            self.assertTrue(_rejected(rc, body),
                            f"{name} {agent!r} not rejected: rc={rc} body={body}")

    def test_traversal(self):
        for a in TRAVERSAL:
            if "\x00" not in a:
                self._assert_rejected_both_paths(a)

    def test_absolute(self):
        for a in ABSOLUTE:
            self._assert_rejected_both_paths(a)

    def test_unknown(self):
        for a in UNKNOWN:
            if "\x00" not in a:
                self._assert_rejected_both_paths(a)

    def test_valid_passes_validation(self):
        # A valid name must get PAST validation: the failure, if any, comes
        # from a later stage (no approval / store access), never the name.
        for name, fn in (("forward", _forward), ("rollback", _rollback)):
            rc, body = fn(VALID)
            self.assertFalse(_rejected(rc, body),
                             f"{name} valid agent wrongly rejected: {body}")


class SourceShape(unittest.TestCase):
    def setUp(self):
        with open(PW_PATH, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_dead_guard_gone(self):
        self.assertNotIn('hasattr(P, "_AGENT_RE")', self.src)

    def test_validation_precedes_rollback_branch(self):
        i_val = self.src.find("if not valid_agent(a.agent)")
        i_rb = self.src.find("if a.rollback:")
        self.assertGreater(i_val, 0, "valid_agent call missing in main()")
        self.assertLess(i_val, i_rb, "validation must run before rollback")


if __name__ == "__main__":
    unittest.main(verbosity=2)
