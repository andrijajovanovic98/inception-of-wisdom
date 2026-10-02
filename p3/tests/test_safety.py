"""Part 3 - the five hard bounds, all driven by iow.config.yml."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
