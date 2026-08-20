"""Tests for the deterministic headline and brief rendering.

The headline is the one line most likely to be read in isolation and quoted at someone, and it is
required to be produced without a model. So what needs proving is that the priority ordering actually
holds — that a data-integrity failure outranks a late frame, which outranks a capacity warning — and
that the same report always yields the same sentence.
"""

import unittest
from datetime import datetime, timezone

from csmbot import brief
from csmbot.metrics import (
    Capacity, CohortStats, CurveChange, Frame, Funnel, GateFunnel, Operators, Performance, Report,
)


def make_capacity(**kwargs):
    defaults = dict(
        validator_share_pct=8.30, limit_pct=9.00, active=24729, total_active=297966,
        headroom=2087, depositable=0, rate_7d=None, runway_days=None,
        constraint="supply", window_days=0,
    )
    defaults.update(kwargs)
    return Capacity(**defaults)


def make_operators(**kwargs):
    defaults = dict(
        total=616, active=428, departed=162, never_funded=25, active_keys=24729,
        cohorts=[CohortStats(2, "ICS", 289, 243, 45, 1, 10057)],
        new_ids=None, departed_ids=None, new_by_curve={}, departed_by_curve={},
        curve_changes=[],
    )
    defaults.update(kwargs)
    return Operators(**defaults)


def make_gate(**kwargs):
    defaults = dict(
        label="ICS", curve_id=2, eligible=514, claimed=289, unclaimed=225,
        claim_rate=56.2, operators_on_curve=289,
        tree_root="0xabc", tree_cid="bafy", available=True, integrity_ok=True,
        claimed_addresses=[], unclaimed_addresses=[],
    )
    defaults.update(kwargs)
    return GateFunnel(**defaults)


def make_frame(**kwargs):
    defaults = dict(
        ref_time=datetime(2026, 8, 3, tzinfo=timezone.utc),
        deadline=datetime(2026, 8, 31, 13, 36, tzinfo=timezone.utc),
        frame_days=28.0, hours_remaining=268.0, is_late=False, hours_late=0.0,
    )
    defaults.update(kwargs)
    return Frame(**defaults)


_UNSET = object()


def make_report(capacity=None, operators=None, gates=None, frame=_UNSET, **kwargs):
    return Report(
        day="2026-08-20",
        block=25795491,
        capacity=capacity or make_capacity(),
        operators=operators or make_operators(),
        funnel=Funnel(gates=gates if gates is not None else [make_gate()]),
        frame=make_frame() if frame is _UNSET else frame,
        **kwargs,
    )


class TestHeadlineDeterminism(unittest.TestCase):
    def test_same_report_gives_same_headline(self):
        report = make_report()
        first = brief.headline(report)
        for _ in range(20):
            self.assertEqual(brief.headline(report), first)

    def test_equivalent_reports_give_identical_headlines(self):
        self.assertEqual(brief.headline(make_report()), brief.headline(make_report()))


class TestHeadlinePriority(unittest.TestCase):
    """Each case asserts that a higher-priority condition wins even when lower ones also hold."""

    def test_integrity_failure_outranks_everything(self):
        report = make_report(
            gates=[make_gate(integrity_ok=False, operators_on_curve=250)],
            frame=make_frame(is_late=True, hours_late=40),
            capacity=make_capacity(runway_days=5, rate_7d=20.0, window_days=7),
        )
        headline = brief.headline(report)
        self.assertIn("integrity failed", headline)
        self.assertIn("unverified", headline)

    def test_unavailable_tree_outranks_late_frame(self):
        report = make_report(
            gates=[make_gate(available=False, eligible=0, claimed=0, unclaimed=0)],
            frame=make_frame(is_late=True, hours_late=40),
        )
        self.assertIn("unreadable", brief.headline(report))

    def test_late_frame_outranks_runway(self):
        report = make_report(
            frame=make_frame(is_late=True, hours_late=40),
            capacity=make_capacity(runway_days=5, rate_7d=20.0, window_days=7),
        )
        self.assertIn("past deadline", brief.headline(report))

    def test_runway_outranks_operator_movement(self):
        report = make_report(
            capacity=make_capacity(runway_days=12, rate_7d=20.0, window_days=7),
            operators=make_operators(new_ids=[1], departed_ids=[2, 3, 4]),
        )
        headline = brief.headline(report)
        self.assertIn("12 days of capacity left", headline)

    def test_zero_claims_is_reported_when_nothing_more_urgent(self):
        # The originating case: a batch made eligible and nobody claiming.
        report = make_report(gates=[make_gate(claimed=0, unclaimed=514, claim_rate=0.0,
                                              operators_on_curve=0)])
        self.assertIn("None of the 514 addresses", brief.headline(report))

    def test_operator_decline_names_ics_count(self):
        report = make_report(
            operators=make_operators(new_ids=[1], departed_ids=[2, 3, 4],
                                     departed_by_curve={2: 3}),
        )
        headline = brief.headline(report)
        self.assertIn("down 2", headline)
        self.assertIn("3 ICS", headline)

    def test_quiet_week_falls_through_to_unclaimed_summary(self):
        report = make_report(operators=make_operators(new_ids=[], departed_ids=[]))
        self.assertIn("225 of 514", brief.headline(report))

    def test_fully_quiet_week_says_so(self):
        report = make_report(
            operators=make_operators(new_ids=[], departed_ids=[]),
            gates=[make_gate(eligible=514, claimed=514, unclaimed=0, claim_rate=100.0,
                             operators_on_curve=514)],
        )
        self.assertEqual(brief.headline(report), "No material change.")


class TestPhrasing(unittest.TestCase):
    def test_key_count_reads_correctly_at_zero_one_and_many(self):
        self.assertEqual(brief._waiting(0), "no keys waiting")
        self.assertEqual(brief._waiting(1), "1 key waiting")
        self.assertEqual(brief._waiting(2087), "2,087 keys waiting")

    def test_supply_constraint_sentence_is_grammatical_at_one(self):
        report = make_report(capacity=make_capacity(depositable=1, constraint="supply"))
        rendered = brief.render(report)
        self.assertIn("1 key waiting", rendered)
        self.assertNotIn("1 keys", rendered)


class TestRendering(unittest.TestCase):
    def test_brief_fits_a_single_telegram_message(self):
        report = make_report(
            operators=make_operators(
                new_ids=list(range(8)), departed_ids=list(range(8, 14)),
                new_by_curve={0: 3, 2: 5}, departed_by_curve={1: 4, 2: 2},
                cohorts=[
                    CohortStats(0, "Permissionless", 171, 125, 29, 17, 7353),
                    CohortStats(1, "Legacy early adoption", 154, 58, 88, 8, 7202),
                    CohortStats(2, "ICS", 289, 243, 45, 1, 10057),
                    CohortStats(3, "IDVTC", 2, 2, 0, 0, 117),
                ],
            ),
            comparison_day="2026-08-13",
        )
        self.assertLess(len(brief.render(report)), 4096)

    def test_missing_days_are_surfaced_not_hidden(self):
        report = make_report(missing_days=["2026-08-15", "2026-08-16"])
        rendered = brief.render(report)
        self.assertIn("no data", rendered)
        self.assertIn("2026-08-15", rendered)

    def test_warnings_are_surfaced(self):
        report = make_report(warnings=["ICS eligibility tree unavailable"])
        self.assertIn("ICS eligibility tree unavailable", brief.render(report))

    def test_first_run_admits_it_has_no_history(self):
        rendered = brief.render(make_report())
        self.assertIn("first run", rendered)
        self.assertIn("needs a week of history", rendered)

    def test_empty_frame_block_is_omitted(self):
        report = make_report(frame=None)
        self.assertNotIn("Rewards frame", brief.render(report))

    def test_performance_uses_explicit_reward_and_validator_language(self):
        report = make_report()
        report.performance = Performance(
            frame_date="2026-08-03", previous_frame_date="2026-07-06", interval_days=28,
            operators_in_tree=567, earned=375, earned_nothing=159, first_time=33,
            stopped=19, resumed=7, idle_retired=148, idle_running=11,
        )
        lines = brief._performance_block(report)
        self.assertIn("375 returning operators earned rewards", lines)
        self.assertIn("33 operators earned rewards for the first time", lines)
        self.assertIn("19 operators earned rewards last frame, not this one", lines)
        self.assertIn("7 operators earned rewards this frame after missing the last", lines)
        self.assertIn("148 operators earned no rewards · no active validators now", lines)
        self.assertIn("11 operators earned no rewards · active validators now", lines)
        self.assertTrue(any("Next frame due 31 Aug" in line for line in lines))
        self.assertFalse(any("stopped" in line or "resumed" in line for line in lines))

    def test_week_key_is_stable_within_a_week(self):
        monday = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
        friday = datetime(2026, 8, 21, 23, tzinfo=timezone.utc)
        self.assertEqual(brief.week_key(monday), brief.week_key(friday))
        next_monday = datetime(2026, 8, 24, 9, tzinfo=timezone.utc)
        self.assertNotEqual(brief.week_key(monday), brief.week_key(next_monday))


class TestCorrectness(unittest.TestCase):
    """Guards for the two things that were wrong before and must not regress."""

    def test_share_is_labelled_as_validator_count_not_stake(self):
        # The limit is enforced on validator counts, but other modules run 0x02 validators holding
        # more than 32 ETH, so this ratio is not a share of stake and must never read as one.
        rendered = brief.render(make_report())
        self.assertIn("of 9.00% cap", rendered)

    def test_csm_stake_is_reported_in_eth_separately(self):
        rendered = brief.render(make_report())
        self.assertIn("791,328 ETH", rendered)

    def test_cohorts_carry_no_percentage(self):
        # A departure count over a mutable cohort's current size is a selected population, not a
        # rate. Counts only.
        report = make_report(
            operators=make_operators(cohorts=[
                CohortStats(1, "Legacy early adoption", 154, 58, 88, 8, 7202),
            ])
        )
        rendered = brief.render(report)
        self.assertIn("Legacy", rendered)
        self.assertNotIn("(57%)", rendered)

    def test_curve_changes_reported_apart_from_joins_and_departures(self):
        report = make_report(
            operators=make_operators(
                new_ids=[], departed_ids=[],
                curve_changes=[CurveChange(5, 1, 2), CurveChange(9, 1, 2), CurveChange(11, 2, 3)],
            ),
            comparison_day="2026-08-13",
        )
        rendered = brief.render(report)
        self.assertIn("3 changed type", rendered)
        self.assertIn("Legacy→ICS 2", rendered)
        self.assertIn("ICS→IDVTC 1", rendered)

    def test_no_advice_in_the_capacity_block(self):
        # The bot reports the binding constraint; it does not recommend what to do about it.
        rendered = brief.render(make_report())
        for phrase in ("would change nothing", "should ", " you ", "consider ", "recommend"):
            self.assertNotIn(phrase, rendered)


if __name__ == "__main__":
    unittest.main()


class TestPluralisation(unittest.TestCase):
    """These strings get forwarded to other people; "1 operators" undermines the rest."""

    def test_plural_helper(self):
        self.assertEqual(brief._plural(1, "operator"), "1 operator")
        self.assertEqual(brief._plural(0, "operator"), "0 operators")
        self.assertEqual(brief._plural(3, "operator"), "3 operators")
        self.assertEqual(brief._plural(1, "entry", "entries"), "1 entry")
        self.assertEqual(brief._plural(2, "entry", "entries"), "2 entries")

    def test_single_ejectable_operator_reads_singular(self):
        from csmbot.metrics import StrikeRisk, Strikes
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=908, struck_operators=89,
            ejectable=[StrikeRisk(367, 0, "0xa", 12, 3, 3, 9)],
            thresholds={0: (6, 3)},
        )
        headline = brief.headline(report)
        self.assertIn("1 operator at the ejection threshold", headline)
        self.assertNotIn("1 operators", headline)

    def test_multiple_ejectable_reads_plural(self):
        from csmbot.metrics import StrikeRisk, Strikes
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=908, struck_operators=89,
            ejectable=[StrikeRisk(367, 0, "0xa", 12, 3, 3, 9),
                       StrikeRisk(559, 0, "0xb", 1, 3, 3, 4)],
            thresholds={0: (6, 3)},
        )
        self.assertIn("2 operators at the ejection threshold", brief.headline(report))


class TestTerminology(unittest.TestCase):
    """Team vocabulary. "NOs" and "type" are what Isaac's team actually says."""

    def test_operators_are_NOs_not_ops(self):
        from csmbot.competitors import PoolDelta
        report = make_report()
        report.pools = [PoolDelta("Rocket Pool", 4152, 3, 14297, 12, 457504, "")]
        rendered = brief.render(report)
        self.assertIn("4,152 NOs", rendered)
        self.assertIn("val/NO", rendered)
        self.assertNotIn(" ops ", rendered)

    def test_cohort_column_is_called_type(self):
        rendered = brief.render(make_report())
        self.assertIn("type", rendered)
        self.assertNotIn("cohort", rendered)

    def test_no_disclaimers_in_the_rendered_brief(self):
        """He wrote the rules; restating them weekly is a line to skip past. They live in the
        docstrings and the handoff instead."""
        from csmbot.metrics import StrikeRisk, Strikes
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=908, struck_operators=89,
            ejectable=[StrikeRisk(367, 0, "0xa", 12, 3, 3, 9)],
            thresholds={0: (6, 3), 2: (6, 4)},
        )
        rendered = brief.render(report)
        for phrase in ("Membership is mutable", "rolling window", "ejection limit is per"):
            self.assertNotIn(phrase, rendered)

    def test_strikes_table_has_no_limit_column(self):
        from csmbot.metrics import StrikeRisk, Strikes
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=908, struck_operators=89,
            ejectable=[StrikeRisk(367, 0, "0xa", 12, 3, 3, 9)],
            thresholds={0: (6, 3)},
        )
        block = "\n".join(brief._strikes_block(report))
        self.assertIn("worst key", block)
        self.assertNotIn("limit", block)


class TestCollapsedSummaryLines(unittest.TestCase):
    """The first line of a collapsed blockquote is the only one most readings see. It has to carry
    information, not column names."""

    def _first_blockquote_line(self, block_lines):
        import re
        m = re.search(r"<blockquote expandable><pre>(.*?)\n", "\n".join(block_lines), re.S)
        return m.group(1) if m else ""

    def test_cohorts_lead_with_a_summary_not_the_header(self):
        report = make_report(operators=make_operators(cohorts=[
            CohortStats(0, "Permissionless", 171, 125, 29, 17, 7353),
            CohortStats(2, "ICS", 289, 243, 45, 1, 10057),
        ]))
        first = self._first_blockquote_line(brief._operators_block(report))
        self.assertIn("active by type", first)
        self.assertIn("Default 125", first)
        self.assertIn("ICS 243", first)
        self.assertNotIn("gone", first)

    def test_strikes_lead_with_who_is_closest(self):
        from csmbot.metrics import StrikeRisk, Strikes
        report = make_report()
        report.strikes = Strikes(
            available=True, struck_keys=908, struck_operators=89,
            ejectable=[StrikeRisk(367, 0, "0xa", 12, 3, 3, 9)],
            at_risk=[StrikeRisk(524, 2, "0xb", 351, 3, 4, 402)],
            thresholds={0: (6, 3), 2: (6, 4)},
        )
        first = self._first_blockquote_line(brief._strikes_block(report))
        self.assertIn("closest to ejection", first)
        self.assertIn("#367 3/3", first)
        self.assertIn("#524 3/4", first)
        self.assertNotIn("worst key", first)

    def test_pools_lead_with_the_delta(self):
        from csmbot.competitors import PoolDelta
        report = make_report()
        report.pools = [PoolDelta("Rocket Pool", 4152, 3, 14297, 12, 457504, "")]
        first = self._first_blockquote_line(brief._pools_block(report))
        self.assertIn("this week", first)
        self.assertIn("NOs +3", first)
        self.assertIn("val +12", first)
        self.assertNotIn("457,504", first)

    def test_pools_fall_back_to_levels_without_history(self):
        from csmbot.competitors import PoolDelta
        report = make_report()
        report.pools = [PoolDelta("Rocket Pool", 4152, None, 14297, None, 457504, "")]
        first = self._first_blockquote_line(brief._pools_block(report))
        self.assertIn("14,297 val", first)
