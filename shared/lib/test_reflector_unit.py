#!/usr/bin/env python3
"""The autonomous unit must actually carry the pinned ladder.

Without this, a silent regression to the library defaults (local,free,cheap
/ 5 / 24000) would change what the timer spends and on which providers, and
nothing would turn red. Asserted against the INSTALLED unit file, not a
template. Absence of the unit is a FAILURE, never a skip.
"""
import os
import re
import unittest

UNIT = os.environ.get("REFLECTOR_UNIT",
                      "/etc/systemd/system/treebeard-reflector.service")
TIMER = os.environ.get("REFLECTOR_TIMER",
                       "/etc/systemd/system/treebeard-reflector.timer")
CADENCE = "*:00/15"
REQUIRED = {
    "MEESEEKS_TIERS": "free,cheap,cheap",
    "MEESEEKS_CYCLE_CAP": "8",
    "MEESEEKS_PROMPT_CAP": "32000",
    "REFLECTOR_TICK_STEPS": "4",
}


class UnitRenderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(UNIT), "unit file absent: %s" % UNIT)
        self.text = open(UNIT).read()
        self.env = dict(re.findall(r"^Environment=([A-Z_]+)=(.*)$",
                                   self.text, re.M))

    def test_pinned_ladder_values_are_rendered(self):
        for name, want in REQUIRED.items():
            self.assertIn(name, self.env, "%s not set in the unit" % name)
            self.assertEqual(self.env[name], want,
                             "%s is %r, expected %r"
                             % (name, self.env.get(name), want))

    def test_library_defaults_are_not_what_the_unit_carries(self):
        # Positive control on the assertion above: these are the values a
        # regression would drift back to.
        self.assertNotEqual(self.env.get("MEESEEKS_TIERS"), "local,free,cheap")
        self.assertNotEqual(self.env.get("MEESEEKS_CYCLE_CAP"), "5")
        self.assertNotEqual(self.env.get("MEESEEKS_PROMPT_CAP"), "24000")

    def test_environment_file_precedes_explicit_environment(self):
        ef = self.text.find("EnvironmentFile=")
        first_env = self.text.find("Environment=TREEBEARD_BASE")
        self.assertNotEqual(ef, -1, "no EnvironmentFile= line")
        self.assertLess(ef, first_env,
                        "EnvironmentFile must precede Environment= so the "
                        "pinned values win")

    def test_runs_as_the_service_account_and_invokes_the_canonical_module(self):
        """Relational, not literal: the unit is RENDERED from a template, so
        @TB_USER@ and @TB_BASE@ become whatever the installer was given. The
        property is that the unit runs the canonical module under the base it
        declares, as the account it declares -- which survives relocation.
        (2026-08-31: was asserting this host's literals, which would have
        failed on any non-default install once the unit shipped.)"""
        base = self.env.get("TREEBEARD_BASE")
        self.assertIsNotNone(base, "unit declares no TREEBEARD_BASE")
        m = re.search(r"^ExecStart=(\S+)\s+(\S+)\s+(\S+)\s*$",
                      self.text, re.M)
        self.assertIsNotNone(m, "no single-line ExecStart with interpreter, "
                                "script and subcommand")
        interp, script, sub = m.groups()
        self.assertEqual(script,
                         os.path.join(base, "shared", "lib", "reflector.py"),
                         "ExecStart runs %r, not the canonical module under "
                         "the declared base" % script)
        self.assertEqual(interp,
                         os.path.join(base, "shared", "venv", "bin", "python3"),
                         "interpreter %r is outside the declared base" % interp)
        self.assertEqual(sub, "tick")
        user = re.search(r"^User=(\S+)$", self.text, re.M)
        group = re.search(r"^Group=(\S+)$", self.text, re.M)
        self.assertIsNotNone(user, "no User= line")
        self.assertIsNotNone(group, "no Group= line")
        self.assertEqual(user.group(1), group.group(1),
                         "User and Group differ; the rendered unit should run "
                         "as the single service account")
        self.assertNotEqual(user.group(1), "root",
                            "the tick must never run as root")
        self.assertIn("Type=oneshot", self.text)



class TimerRenderTests(unittest.TestCase):
    """The SCHEDULE is as much a safety property as the worker config: an
    edit to the cadence changes how fast the system spends, and the service
    assertions above would all still pass. Absence of the timer is a
    FAILURE, never a skip."""

    def setUp(self):
        self.assertTrue(os.path.exists(TIMER), "timer file absent: %s" % TIMER)
        self.text = open(TIMER).read()

    def _value(self, key):
        m = re.search(r"^%s=(.*)$" % re.escape(key), self.text, re.M)
        return m.group(1).strip() if m else None

    def test_cadence_is_the_ruled_fifteen_minutes(self):
        self.assertEqual(self._value("OnCalendar"), CADENCE,
                         "cadence is %r, expected %r -- a schedule change is "
                         "a spend-rate change" % (self._value("OnCalendar"),
                                                  CADENCE))

    def test_persistent_is_false(self):
        # true would fire a catch-up burst after any downtime.
        self.assertEqual(self._value("Persistent"), "false")

    def test_timer_targets_the_reflector_service_by_name(self):
        self.assertEqual(self._value("Unit"), "treebeard-reflector.service")

    def test_the_targeted_unit_file_exists_and_is_the_one_asserted_above(self):
        target = self._value("Unit")
        path = os.path.join(os.path.dirname(TIMER), target)
        self.assertTrue(os.path.exists(path),
                        "timer targets %s but no such unit file" % path)
        self.assertEqual(os.path.abspath(path), os.path.abspath(UNIT),
                         "the timer targets a DIFFERENT unit than the one "
                         "the service assertions cover")


if __name__ == "__main__":
    unittest.main(verbosity=2)
