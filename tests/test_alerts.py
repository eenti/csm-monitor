"""Tests for alert firing, dedupe, and silence when there is no history.

The dedupe behaviour is the one worth guarding hardest. An alert that re-fires every run for a
condition lasting a fortnight trains the reader to ignore the channel, which costs more than the alert
was ever worth.
"""

import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from csmbot import alerts
from csmbot.metrics import Performance, StrikeRisk, Strikes
from csmbot.store import Store

from tests.test_brief import make_capacity, make_gate, make_operators, make_report


@dataclass
class FakeConfig:
    assessment_rounds: tuple = ()


def days_from_today(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


class TestCapacityRunway(unittest.TestCase):
    def test_silent_without_history(self):
        report = make_report(capacity=make_capacity(runway_days=None))
        self.assertEqual(alerts.capacity_runway(report), [])

    def test_fires_only_the_steps_that_are_crossed(self):
        report = make_report(capacity=make_capacity(runway_days=20, rate_7d=5.0, window_days=7))
        keys = [a.key for a in alerts.capacity_runway(report)]
        self.assertEqual(keys, ["runway:30", "runway:14"][:1])  # 20 < 30 but not < 14

    def test_all_steps_when_very_short(self):
        report = make_report(capacity=make_capacity(runway_days=3, rate_7d=50.0, window_days=7))
        keys = [a.key for a in alerts.capacity_runway(report)]
        self.assertEqual(keys, ["runway:30", "runway:14", "runway:7"])

    def test_text_carries_the_numbers(self):
        report = make_report(capacity=make_capacity(runway_days=12, rate_7d=8.5, window_days=7))
        text = alerts.capacity_runway(report)[0].text
        self.assertIn("12d left", text)
        self.assertIn("+8.5/day", text)


class TestEjectionRisk(unittest.TestCase):
    def make(self, ejectable=(), at_risk=()):
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=100, struck_operators=10,
            ejectable=list(ejectable), at_risk=list(at_risk),
            thresholds={0: (6, 3), 2: (6, 4)},
        )
        return report

    def test_silent_when_unavailable(self):
        report = make_report()
        report.strikes = Strikes(available=False, struck_keys=0, struck_operators=0)
        self.assertEqual(alerts.ejection_risk(report), [])

    def test_names_ejectable_operators(self):
        report = self.make(ejectable=[StrikeRisk(315, 0, "0xaaa", 2, 4, 3, 5)])
        fired = alerts.ejection_risk(report)
        self.assertEqual(len(fired), 1)
        self.assertIn("#315", fired[0].text)
        self.assertIn("0xaaa", fired[0].text)  # the link target, so it is clickable
        self.assertEqual(fired[0].payload["ids"], [315])

    def test_separate_alerts_for_ejectable_and_at_risk(self):
        report = self.make(
            ejectable=[StrikeRisk(1, 0, "0xa", 1, 3, 3, 4)],
            at_risk=[StrikeRisk(2, 0, "0xb", 1, 2, 3, 4)],
        )
        keys = [a.key for a in alerts.ejection_risk(report)]
        self.assertTrue(any(k.startswith("strikes:ejectable:") for k in keys))
        self.assertTrue(any(k.startswith("strikes:at_risk:") for k in keys))


class TestDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "a.db")
        self.config = FakeConfig()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_condition_fires_once_then_stays_quiet(self):
        report = make_report(capacity=make_capacity(runway_days=12, rate_7d=8.0, window_days=7))
        first = alerts.evaluate(report, self.store, self.config)
        self.assertTrue(any(a.key == "runway:30" for a in first))

        second = alerts.evaluate(report, self.store, self.config)
        self.assertFalse(any(a.key == "runway:30" for a in second))

    def test_resolved_condition_can_fire_again(self):
        tight = make_report(capacity=make_capacity(runway_days=12, rate_7d=8.0, window_days=7))
        alerts.evaluate(tight, self.store, self.config)

        easy = make_report(capacity=make_capacity(runway_days=400, rate_7d=1.0, window_days=7))
        alerts.clear_resolved(easy, self.store)
        self.assertFalse(self.store.alert_is_active("runway:30"))

        again = alerts.evaluate(tight, self.store, self.config)
        self.assertTrue(any(a.key == "runway:30" for a in again))

    def test_escalation_produces_a_new_message(self):
        thirty = make_report(capacity=make_capacity(runway_days=20, rate_7d=8.0, window_days=7))
        alerts.evaluate(thirty, self.store, self.config)
        fourteen = make_report(capacity=make_capacity(runway_days=10, rate_7d=8.0, window_days=7))
        keys = [a.key for a in alerts.evaluate(fourteen, self.store, self.config)]
        self.assertIn("runway:14", keys)
        self.assertNotIn("runway:30", keys)


class TestUnclaimedBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "u.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_silent_before_the_age_threshold(self):
        added = [f"0x{i:040x}" for i in range(20)]
        self.store.put_gate_batch("ICS", "0xr", days_from_today(-3), "cid", added, added)
        report = make_report(gates=[make_gate(claimed=0, unclaimed=514, claim_rate=0.0,
                                              operators_on_curve=0)])
        self.assertEqual(
            alerts.unclaimed_batch(report, self.store, date.today().isoformat()), []
        )

    def test_fires_once_the_batch_has_aged(self):
        added = [f"0x{i:040x}" for i in range(128)]
        self.store.put_gate_batch("ICS", "0xr", days_from_today(-31), "cid1", added, added)
        report = make_report(gates=[make_gate(claimed=0, unclaimed=514, claim_rate=0.0,
                                              operators_on_curve=0)])
        fired = alerts.unclaimed_batch(report, self.store, date.today().isoformat())
        self.assertEqual(len(fired), 1)
        self.assertIn("0/128 claimed", fired[0].text)
        self.assertIn("31d ago", fired[0].text)

    def test_quiet_when_the_batch_did_claim(self):
        added = [f"0x{i:040x}" for i in range(10)]
        self.store.put_gate_batch("ICS", "0xr", days_from_today(-31), "cid1", added, added)
        # Every address in the batch has claimed, so there is nothing to report.
        gate = make_gate(claimed=10, operators_on_curve=10, claimed_addresses=added)
        report = make_report(gates=[gate])
        self.assertEqual(
            alerts.unclaimed_batch(report, self.store, date.today().isoformat()), []
        )

    def test_partial_claim_below_the_rate_still_fires(self):
        added = [f"0x{i:040x}" for i in range(100)]
        self.store.put_gate_batch("ICS", "0xr", days_from_today(-20), "cid1", added, added)
        # 5 of 100 is under the 10% reporting threshold.
        gate = make_gate(claimed=5, operators_on_curve=5, claimed_addresses=added[:5])
        fired = alerts.unclaimed_batch(
            make_report(gates=[gate]), self.store, date.today().isoformat()
        )
        self.assertEqual(len(fired), 1)
        self.assertIn("5/100 claimed", fired[0].text)

    def test_claim_rate_above_the_threshold_is_quiet(self):
        added = [f"0x{i:040x}" for i in range(100)]
        self.store.put_gate_batch("ICS", "0xr", days_from_today(-20), "cid1", added, added)
        gate = make_gate(claimed=40, operators_on_curve=40, claimed_addresses=added[:40])
        self.assertEqual(
            alerts.unclaimed_batch(make_report(gates=[gate]), self.store,
                                   date.today().isoformat()), []
        )


class TestRoundCutoff(unittest.TestCase):
    def test_fires_at_the_notice_windows_only(self):
        config = FakeConfig(assessment_rounds=(
            ("ICS Round 6", days_from_today(14)),
            ("IDVTC Round 2", days_from_today(3)),
            ("ICS Round 7", days_from_today(9)),
        ))
        keys = [a.key for a in alerts.round_cutoff(config, date.today().isoformat())]
        self.assertIn("round:ICS Round 6:14", keys)
        self.assertIn("round:IDVTC Round 2:3", keys)
        self.assertEqual(len(keys), 2)  # the 9-day one is not a notice window

    def test_no_rounds_configured_is_silent(self):
        self.assertEqual(alerts.round_cutoff(FakeConfig(), date.today().isoformat()), [])


class TestFrameAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "f.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_late_frame_fires(self):
        from tests.test_brief import make_frame
        report = make_report(frame=make_frame(is_late=True, hours_late=40))
        fired = alerts.frame_late(report)
        self.assertEqual(len(fired), 1)
        self.assertIn("40h past", fired[0].text)

    def test_on_time_frame_silent(self):
        self.assertEqual(alerts.frame_late(make_report()), [])

    def test_frame_published_announces_once(self):
        report = make_report()
        report.performance = Performance(
            frame_date="2026-08-03", previous_frame_date="2026-07-06", interval_days=28,
            operators_in_tree=567, earned=375, earned_nothing=159, first_time=33,
            stopped=19, resumed=7, idle_retired=154, idle_running=5,
        )
        fired = alerts.frame_published(report, self.store)
        self.assertEqual(len(fired), 1)
        self.assertIn("154 retired", fired[0].text)
        self.assertIn("5 still running", fired[0].text)

        self.store.record_delivery("alert", fired[0].key, fired[0].text)
        self.assertEqual(alerts.frame_published(report, self.store), [])

    def test_off_cadence_interval_is_flagged(self):
        report = make_report()
        report.performance = Performance(
            frame_date="2026-03-16", previous_frame_date="2026-02-13", interval_days=31,
            operators_in_tree=499, earned=368, earned_nothing=129, first_time=2,
            stopped=21, resumed=19, idle_retired=120, idle_running=9,
        )
        self.assertIn("+3d off cadence", alerts.frame_published(report, self.store)[0].text)


class TestExitAnomaly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "e.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self, values):
        run_id = self.store.start_run("2026-08-01", 1, 1)
        start = date.today() - timedelta(days=len(values) - 1)
        for offset, value in enumerate(values):
            self.store.put_metric(
                (start + timedelta(days=offset)).isoformat(), "csm_exited", value, run_id, 1
            )

    def test_silent_without_enough_history(self):
        self.seed([100, 101, 102])
        report = make_report()
        report.day = date.today().isoformat()
        self.assertEqual(alerts.exit_anomaly(report, self.store), [])

    def test_fires_on_a_spike(self):
        # +2/day for ten days, then +80 in one day.
        self.seed([100 + 2 * i for i in range(10)] + [118 + 80])
        report = make_report()
        report.day = date.today().isoformat()
        fired = alerts.exit_anomaly(report, self.store)
        self.assertEqual(len(fired), 1)
        self.assertIn("80 validators exited", fired[0].text)

    def test_small_absolute_numbers_do_not_fire(self):
        # A jump from 0/day to 3/day is a large multiple but not worth a message.
        self.seed([100] * 10 + [103])
        report = make_report()
        report.day = date.today().isoformat()
        self.assertEqual(alerts.exit_anomaly(report, self.store), [])


if __name__ == "__main__":
    unittest.main()
