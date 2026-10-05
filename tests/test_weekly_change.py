"""Tests for the week-over-week half of the brief.

Two things went wrong here and both were silent, which is why they get their own file.

The comparison snapshot was chosen as the newest one before today. Collection is daily, so that was
always yesterday, and every line the brief labelled "this week" was a one-day delta. Nothing about
the output looked wrong — a quiet week and a week whose movement happened on Monday render
identically.

The funnel integrity check required gate claims to equal operators on the gate's curve. That
equality is not an invariant: an operator can consume a gate and later move to another curve. When
one did, the check declared the funnel unverified and that warning took the top of the brief.

The numbers in `TestFunnelInvariant` and `TestComparisonWindow` are the mainnet readings from the
week of 2026-08-24, kept as fixtures so a regression has to argue with real data.
"""

import unittest

from csmbot.main import _pick_comparison
from csmbot.metrics import funnel
from csmbot.sources import GateState, OperatorState, Snapshot


def rows(*days):
    """Mimic `store.metric_series`: ascending by day, `detail` present."""
    return [{"day": d, "detail": "{}"} for d in days]


class TestComparisonWindow(unittest.TestCase):
    def test_picks_a_week_back_not_yesterday(self):
        # The bug: with a daily collection the newest prior row is always yesterday.
        series = rows(*[f"2026-08-{d:02d}" for d in range(17, 32)])
        picked = _pick_comparison(series, "2026-08-31")
        self.assertEqual(picked["day"], "2026-08-24")

    def test_lands_on_the_newest_day_at_or_before_the_target(self):
        # 24 Aug missing: 23 Aug is an eight-day window, which beats a six-day one silently
        # labelled as a week — and the span gets stated either way.
        series = rows("2026-08-22", "2026-08-23", "2026-08-25", "2026-08-30")
        self.assertEqual(_pick_comparison(series, "2026-08-31")["day"], "2026-08-23")

    def test_falls_back_to_the_oldest_when_history_is_short(self):
        series = rows("2026-08-29", "2026-08-30")
        self.assertEqual(_pick_comparison(series, "2026-08-31")["day"], "2026-08-29")

    def test_today_is_never_its_own_comparison(self):
        self.assertIsNone(_pick_comparison(rows("2026-08-31"), "2026-08-31"))

    def test_no_history_gives_no_comparison(self):
        self.assertIsNone(_pick_comparison([], "2026-08-31"))

    def test_rows_without_detail_are_skipped(self):
        series = [{"day": "2026-08-24", "detail": None}, {"day": "2026-08-23", "detail": "{}"}]
        self.assertEqual(_pick_comparison(series, "2026-08-31")["day"], "2026-08-23")


def make_snapshot(curve_counts, gates):
    operators, oid = [], 0
    for curve_id, count in curve_counts.items():
        for _ in range(count):
            operators.append(OperatorState(
                id=oid, curve_id=curve_id, manager="0x0", reward_address="0x0",
                counters={k: 0 for k in (
                    "totalAddedKeys", "totalWithdrawnKeys", "totalDepositedKeys", "totalVettedKeys",
                    "stuckValidatorsCount", "depositableValidatorsCount", "targetLimit",
                    "targetLimitMode", "totalExitedKeys", "enqueuedCount")},
            ))
            oid += 1
    return Snapshot(block=25852700, timestamp=0, day="2026-08-28",
                    operators=operators, gates=gates)


def make_gate(label, curve_id, eligible, claimed):
    return GateState(label=label, address="0x0", curve_id=curve_id, tree_root="0xroot",
                     tree_cid="bafy", eligible=eligible, claimed=claimed, root_verified=True)


def addresses(n, offset=0):
    return [f"0x{i + offset:040x}" for i in range(n)]


class TestFunnelInvariant(unittest.TestCase):
    """Mainnet, 2026-08-28: the ICS gate held 291 claims while 290 operators sat on curve 2.

    Operator 327 consumed ICS months ago and claimed IDVTC on 2026-08-26, which moved it off curve 2
    without releasing the gate. Both numbers are correct.
    """

    def ics_gate(self):
        return make_gate("ICS", 2, addresses(514), addresses(291))

    def test_a_cohort_move_is_reported_not_alarmed(self):
        report = funnel(make_snapshot({2: 290, 3: 3}, [self.ics_gate()]))
        gate = report.gates[0]
        self.assertTrue(gate.integrity_ok)
        self.assertEqual(gate.off_curve, 1)

    def test_more_operators_than_claims_still_fails(self):
        # The direction claims cannot explain: every consumption sets the operator's curve, so
        # membership outrunning claims means a misread or a curve set some other way.
        report = funnel(make_snapshot({2: 292}, [self.ics_gate()]))
        self.assertFalse(report.gates[0].integrity_ok)

    def test_agreement_is_still_agreement(self):
        report = funnel(make_snapshot({2: 291}, [self.ics_gate()]))
        self.assertTrue(report.gates[0].integrity_ok)
        self.assertEqual(report.gates[0].off_curve, 0)


class TestFunnelDeltas(unittest.TestCase):
    def test_new_claims_are_named_not_just_counted(self):
        snapshot = make_snapshot({2: 291}, [make_gate("ICS", 2, addresses(514), addresses(291))])
        previous = {"ICS": {"claimed": addresses(289), "eligible": 514}}
        gate = funnel(snapshot, previous).gates[0]
        self.assertEqual(gate.claimed_change, 2)
        self.assertEqual(gate.new_claims, addresses(291)[289:])

    def test_a_quiet_gate_reports_zero_rather_than_nothing(self):
        snapshot = make_snapshot({2: 289}, [make_gate("ICS", 2, addresses(514), addresses(289))])
        previous = {"ICS": {"claimed": addresses(289), "eligible": 514}}
        gate = funnel(snapshot, previous).gates[0]
        self.assertEqual(gate.claimed_change, 0)
        self.assertEqual(gate.new_claims, [])

    def test_a_grown_eligibility_tree_is_a_separate_number(self):
        snapshot = make_snapshot({2: 289}, [make_gate("ICS", 2, addresses(544), addresses(289))])
        previous = {"ICS": {"claimed": addresses(289), "eligible": 514}}
        gate = funnel(snapshot, previous).gates[0]
        self.assertEqual(gate.eligible_change, 30)
        self.assertEqual(gate.claimed_change, 0)

    def test_claims_are_differenced_as_sets_not_counts(self):
        # One address released and two added nets to +1 on a count subtraction. The set diff still
        # names both new claims, so the funnel cannot quietly under-report movement.
        snapshot = make_snapshot({2: 290}, [make_gate(
            "ICS", 2, addresses(514), addresses(288) + addresses(2, offset=900))])
        previous = {"ICS": {"claimed": addresses(289), "eligible": 514}}
        gate = funnel(snapshot, previous).gates[0]
        self.assertEqual(gate.new_claims, addresses(2, offset=900))

    def test_no_history_means_no_delta_rather_than_a_zero(self):
        snapshot = make_snapshot({2: 291}, [make_gate("ICS", 2, addresses(514), addresses(291))])
        gate = funnel(snapshot).gates[0]
        self.assertIsNone(gate.claimed_change)
        self.assertIsNone(gate.eligible_change)


if __name__ == "__main__":
    unittest.main()
