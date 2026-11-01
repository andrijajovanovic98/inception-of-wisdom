"""Part 3 - the five hard bounds, all driven by iow.config.yml."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

from p3 import safety
from p3.safety import SafetyManager

BASE = """grace_period: 5
cooldown: 300
rate_limit_per_hour: 3
hard_cap: 4
boot_timeout: 30
kill_switch: false
"""


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="iow-safety-")
        self.path = os.path.join(self.dir, "iow.config.yml")
        self._write(BASE)
        self.sm = SafetyManager(self.path)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        # mtime-driven reload: make sure the change is visible even within a tick.
        stamp = time.time() + getattr(self, "_bump", 0)
        self._bump = getattr(self, "_bump", 0) + 1
        os.utime(self.path, (stamp, stamp))

    def test_timings_come_from_the_file(self) -> None:
        self.assertEqual(self.sm.get_grace_period(), 5.0)
        self.assertEqual(self.sm.get_boot_timeout(), 30.0)
        self._write(BASE.replace("grace_period: 5", "grace_period: 2"))
        self.assertEqual(self.sm.get_grace_period(), 2.0)

    def test_single_flight(self) -> None:
        self.assertTrue(self.sm.acquire_heal_slot("a")[0])
        allowed, reason = self.sm.acquire_heal_slot("b")
        self.assertFalse(allowed)
        self.assertIn("single-flight", reason or "")
        self.sm.release_heal_slot("a")
        # "b" arrived during a's heal, so it is part of that incident (see the sibling
        # test below); a genuinely new signature is free to go.
        self.assertTrue(self.sm.acquire_heal_slot("c")[0])

    def test_cooldown_per_signature(self) -> None:
        self.sm.acquire_heal_slot("sig")
        self.sm.release_heal_slot("sig")
        allowed, reason = self.sm.acquire_heal_slot("sig")
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason or "")
        self.assertTrue(self.sm.acquire_heal_slot("other")[0])

    def test_sibling_signatures_of_the_running_heal_share_its_cooldown(self) -> None:
        """One crash raises exit + log + probe signatures; a failed heal must not be
        retried straight away through one of the siblings."""
        self.assertTrue(self.sm.acquire_heal_slot("exit")[0])
        self.assertFalse(self.sm.acquire_heal_slot("log-pattern")[0])     # refused: single-flight
        self.assertFalse(self.sm.acquire_heal_slot("unreachable")[0])
        self.sm.release_heal_slot("exit")
        for sig in ("exit", "log-pattern", "unreachable"):
            allowed, reason = self.sm.acquire_heal_slot(sig)
            self.assertFalse(allowed)
            self.assertIn("cooldown", reason or "")
        self.assertTrue(self.sm.acquire_heal_slot("a-new-incident")[0])

    def test_rate_limit_per_hour(self) -> None:
        for sig in ("a", "b", "c"):
            self.assertTrue(self.sm.acquire_heal_slot(sig)[0])
            self.sm.release_heal_slot(sig)
        allowed, reason = self.sm.acquire_heal_slot("d")
        self.assertFalse(allowed)
        self.assertIn("rate limit", reason or "")

    def test_the_rate_limit_counts_the_last_hour_only(self) -> None:
        """T4: subject V.3, "a rate limit per hour". Three heals half an hour ago fill the hour;
        three heals two hours ago do not (a mutation run widened the window to ten hours and
        every suite stayed green)."""
        self._write(BASE.replace("hard_cap: 4", "hard_cap: 100").replace("cooldown: 300", "cooldown: 0"))
        self.sm._heal_timestamps_history = [time.time() - 1800.0] * 3
        allowed, reason = self.sm.acquire_heal_slot("recent")
        self.assertFalse(allowed)
        self.assertIn("rate limit", reason or "")
        self.sm._heal_timestamps_history = [time.time() - 7200.0] * 3
        self.assertTrue(self.sm.acquire_heal_slot("old")[0])

    def test_the_cooldown_runs_from_the_end_of_the_heal(self) -> None:
        """T4: a heal that ran longer than the cooldown must not be out of its cooldown the
        moment it ends - the cooldown is there to keep a patch from retriggering the loop."""
        self._write(BASE.replace("cooldown: 300", "cooldown: 1"))
        self.assertTrue(self.sm.acquire_heal_slot("sig")[0])
        time.sleep(0.7)                                      # the heal runs
        self.sm.release_heal_slot("sig")
        time.sleep(0.5)                                      # 1.2 s after it started, 0.5 s after it ended
        allowed, reason = self.sm.acquire_heal_slot("sig")
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason or "")
        time.sleep(0.6)
        self.assertTrue(self.sm.acquire_heal_slot("sig")[0])

    def test_hard_cap(self) -> None:
        self._write(BASE.replace("rate_limit_per_hour: 3", "rate_limit_per_hour: 100"))
        for sig in ("a", "b", "c", "d"):
            self.assertTrue(self.sm.acquire_heal_slot(sig)[0])
            self.sm.release_heal_slot(sig)
        allowed, reason = self.sm.acquire_heal_slot("e")
        self.assertFalse(allowed)
        self.assertIn("hard cap", reason or "")

    def test_kill_switch_flips_live(self) -> None:
        self._write(BASE.replace("kill_switch: false", "kill_switch: true"))
        allowed, reason = self.sm.acquire_heal_slot("a")
        self.assertFalse(allowed)
        self.assertIn("kill_switch", reason or "")
        self.assertEqual(self.sm.last_refusal and self.sm.last_refusal["signature"], "a")

    def test_a_typo_never_disarms_the_kill_switch(self) -> None:
        self._write(BASE.replace("kill_switch: false", "kill_switch: true"))
        self.sm.reload_config()
        self._write(BASE.replace("kill_switch: false", "kill_switch: ture"))
        self.assertTrue(self.sm.reload_config()["kill_switch"])

    def test_an_empty_file_keeps_the_last_good_values(self) -> None:
        self._write(BASE.replace("kill_switch: false", "kill_switch: true"))
        self.sm.reload_config()
        self._write("")
        self.assertTrue(self.sm.reload_config()["kill_switch"])

    def test_a_deleted_file_keeps_the_values_and_is_reported_once(self) -> None:
        """It used to log a warning on every status poll - every 2-3 s."""
        self._write(BASE.replace("kill_switch: false", "kill_switch: true"))
        self.sm.reload_config()
        os.remove(self.path)
        with mock.patch.object(safety.logger, "warning") as warning:
            for _ in range(20):
                self.sm.get_status()
        self.assertEqual(warning.call_count, 1)
        self.assertTrue(self.sm.get_status()["config"]["kill_switch"])   # still armed

    def test_a_deleted_key_goes_back_to_its_default(self) -> None:
        self._write(BASE.replace("cooldown: 300\n", ""))
        self.assertEqual(self.sm.reload_config()["cooldown"], 300.0)
        self._write(BASE.replace("cooldown: 300", "cooldown: 10"))
        self.assertEqual(self.sm.reload_config()["cooldown"], 10.0)
        self._write(BASE.replace("cooldown: 300\n", ""))
        self.assertEqual(self.sm.reload_config()["cooldown"], 300.0)

    def test_invalid_numbers_are_rejected(self) -> None:
        broken = BASE.replace("hard_cap: 4", "hard_cap: -1")
        self._write(broken.replace("grace_period: 5", "grace_period: soon"))
        cfg = self.sm.reload_config()
        self.assertEqual(cfg["hard_cap"], 4)
        self.assertEqual(cfg["grace_period"], 5.0)

    def test_a_bound_of_zero_refuses_every_heal(self) -> None:
        """L21, seen live: an operator lowering a bound as far as it goes got the last value
        back - `rate_limit_per_hour: 0` was 5 and `hard_cap: 0` was 20, and the heal ran."""
        for line, words in (("rate_limit_per_hour: 0", "rate limit"), ("hard_cap: 0", "hard cap")):
            key = line.split(":")[0]
            self._write(re.sub(rf"{key}: \d+", line, BASE))
            allowed, reason = self.sm.acquire_heal_slot("sig")
            self.assertFalse(allowed, line)
            self.assertIn(words, reason or "")

    def test_values_that_are_not_finite_are_rejected(self) -> None:
        """L21: YAML reads .inf and .nan as floats. A grace period of .inf never ends, so one
        heal held the single-flight slot for good; .nan compares false with every minimum."""
        for key, keep in (("grace_period", 5.0), ("cooldown", 300.0), ("boot_timeout", 30.0),
                          ("rate_limit_per_hour", 3), ("hard_cap", 4)):
            for word in (".inf", ".nan"):
                self._write(re.sub(rf"{key}: \d+", f"{key}: {word}", BASE))
                self.assertEqual(self.sm.reload_config()[key], keep, f"{key}: {word}")

    def test_a_live_setting_that_is_not_finite_keeps_the_last_value(self) -> None:
        """L21: `top_k: .inf` raised OverflowError where only ValueError was expected, in the
        constructor: one word in the file kept the agent from starting."""
        self._write(BASE + "analyst:\n  top_k: .inf\ntarget:\n  probe_interval_seconds: .inf\n")
        settings = self.sm.read_live_settings()
        self.assertEqual(settings["top_k"], 3)
        self.assertEqual(settings["probe_interval_seconds"], 3.0)

    def test_a_heal_that_found_nothing_to_do_costs_nothing(self) -> None:
        """L22, seen live: two real heals and three checks of a crash already gone read 5/5, and
        a real crash was refused. No model, no commit, no restart: no count and no cooldown."""
        self.assertTrue(self.sm.acquire_heal_slot("gone")[0])
        self.sm.acquire_heal_slot("sibling")                 # refused: single-flight
        self.sm.refund_heal("gone")
        self.sm.release_heal_slot("gone")
        status = self.sm.get_status()
        self.assertEqual((status["hourly_heal_count"], status["lifetime_heal_count"]), (0, 0))
        self.assertEqual(status["active_signatures_in_cooldown"], 0)
        self.assertFalse(status["single_flight_active"])
        self.assertTrue(self.sm.acquire_heal_slot("gone")[0])
        self.sm.release_heal_slot("gone")                    # a real heal counts again
        status = self.sm.get_status()
        self.assertEqual((status["lifetime_heal_count"], status["active_signatures_in_cooldown"]), (1, 1))

    def test_a_failed_heal_holds_the_whole_outage(self) -> None:
        """L23, seen live with the model stopped and a 300 s cooldown: a heal that fails fast is
        over before the sibling signatures arrive, and one of them started the cycle again six
        seconds later - the same symptom retriggering the loop on its own."""
        self.sm.acquire_heal_slot("unreachable")
        self.sm.hold_incident()
        self.sm.release_heal_slot("unreachable")
        allowed, reason = self.sm.acquire_heal_slot("restart-loop")     # first seen only now
        self.assertFalse(allowed)
        self.assertIn("outage the last heal failed on", reason or "")
        self.sm.release_incident()                           # it recovered: a new crash is new
        self.assertTrue(self.sm.acquire_heal_slot("restart-loop")[0])

    def test_the_hold_on_a_failed_outage_ends_with_the_cooldown(self) -> None:
        """L23: `cooldown` in the file is what bounds it, live."""
        self.sm.hold_incident()
        self.assertFalse(self.sm.acquire_heal_slot("sibling")[0])
        self._write(BASE.replace("cooldown: 300", "cooldown: 0"))
        self.assertTrue(self.sm.acquire_heal_slot("sibling")[0])

    def test_an_operator_landing_is_single_flight_but_not_counted(self) -> None:
        """Landing an approved PR: kill switch and single-flight, not the heal counters."""
        ok, _ = self.sm.acquire_exclusive_slot("pr-merge:1")
        self.assertTrue(ok)
        self.assertFalse(self.sm.acquire_heal_slot("sig")[0])               # single-flight
        self.sm.release_heal_slot()
        status = self.sm.get_status()
        self.assertEqual((status["hourly_heal_count"], status["lifetime_heal_count"]), (0, 0))
        self._write(BASE.replace("kill_switch: false", "kill_switch: true"))
        refused, reason = self.sm.acquire_exclusive_slot("pr-merge:2")
        self.assertFalse(refused)
        self.assertIn("kill_switch", reason or "")

    def test_observer_and_analyst_settings_are_live_and_validated(self) -> None:
        """A typo in target: used to raise in the dashboard's constructor (no agent at
        all), and no edit of the section took effect before a restart."""
        target = (
            "target:\n  probe_interval_seconds: 3\n  crash_dedup_window_seconds: 60\n"
            "  probe_urls: ['/', '/healthz']\nanalyst:\n  top_k: 3\n"
        )
        self._write(BASE + target)
        version = self.sm.version
        first = self.sm.read_live_settings()
        self.assertEqual((first["probe_interval_seconds"], first["top_k"]), (3.0, 3))

        self._write(BASE + target.replace("probe_interval_seconds: 3", "probe_interval_seconds: fast")
                    .replace("top_k: 3", "top_k: 5").replace("['/', '/healthz']", "'/'"))
        self.sm.reload_config()
        self.assertGreater(self.sm.version, version)                 # an edit is noticed
        second = self.sm.read_live_settings(previous=first)
        self.assertEqual(second["top_k"], 5)                         # applied
        self.assertEqual(second["probe_interval_seconds"], 3.0)      # a typo keeps the last value
        self.assertEqual(second["probe_urls"], ["/", "/healthz"])    # so does a non-list

    def test_the_patch_strategy_is_a_live_choice(self) -> None:
        """J1: a peer can have the model write the whole JSON patch, mid-defense."""
        self._write(BASE + "analyst:\n  patch_strategy: full_file\n")
        first = self.sm.read_live_settings()
        self.assertEqual(first["patch_strategy"], "full_file")
        self._write(BASE + "analyst:\n  patch_strategy: fullfile\n")   # a typo
        self.sm.reload_config()
        self.assertEqual(self.sm.read_live_settings(previous=first)["patch_strategy"], "full_file")
        self._write(BASE)                                               # removed: the default
        self.sm.reload_config()
        self.assertEqual(self.sm.read_live_settings(previous=first)["patch_strategy"], "region")


if __name__ == "__main__":
    unittest.main()
