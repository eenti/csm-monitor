"""Tests for when collection happens.

Worth testing because the failure is silent: an interval-based trigger drifts backwards through the
day, and a daily growth rate built from readings taken at different hours carries that drift as noise
into the capacity runway alert.
"""

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from csmbot.store import Store


@dataclass
class FakeConfig:
    collect_hour_utc: int = 6


class FakeBot:
    """Only the scheduling logic, without a network or a Telegram token."""

    def __init__(self, store, config):
        self.store, self.config = store, config

    _collection_due = None  # bound below


from csmbot.main import Bot
FakeBot._collection_due = Bot._collection_due


class TestCollectionSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.db")
        self.bot = FakeBot(self.store, FakeConfig(collect_hour_utc=6))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def at(self, day: str, hour: int) -> datetime:
        return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00")

    def record(self, day: str, status: str = "ok") -> None:
        run_id = self.store.start_run(day, 1, 1)
        self.store.finish_run(run_id, status)

    def test_waits_for_the_hour(self):
        self.assertFalse(self.bot._collection_due(self.at("2026-08-20", 5)))
        self.assertTrue(self.bot._collection_due(self.at("2026-08-20", 6)))

    def test_once_per_day(self):
        self.assertTrue(self.bot._collection_due(self.at("2026-08-20", 6)))
        self.record("2026-08-20")
        # Later the same day: already collected, nothing more to do — no drift.
        self.assertFalse(self.bot._collection_due(self.at("2026-08-20", 7)))
        self.assertFalse(self.bot._collection_due(self.at("2026-08-20", 23)))

    def test_new_day_is_due_again(self):
        self.record("2026-08-20")
        self.assertTrue(self.bot._collection_due(self.at("2026-08-21", 6)))

    def test_late_start_collects_immediately(self):
        # Container came up at 22:00 having missed the 06:00 window; do not skip the day.
        self.assertTrue(self.bot._collection_due(self.at("2026-08-20", 22)))

    def test_failed_run_does_not_count_as_done(self):
        run_id = self.store.start_run("2026-08-20", 1, 1)
        self.store.finish_run(run_id, "failed")
        self.assertTrue(self.bot._collection_due(self.at("2026-08-20", 8)))

    def test_partial_run_counts_as_done(self):
        # A partial run produced usable numbers with a warning attached; re-collecting would just
        # overwrite them with the same warning.
        self.record("2026-08-20", status="partial")
        self.assertFalse(self.bot._collection_due(self.at("2026-08-20", 8)))

    def test_no_drift_across_a_week(self):
        """The point of the whole thing: seven days, seven collections, all at the same hour."""
        hours = []
        for offset in range(7):
            day = (datetime(2026, 8, 20, tzinfo=timezone.utc) + timedelta(days=offset))
            for hour in range(24):
                moment = day.replace(hour=hour)
                if self.bot._collection_due(moment):
                    hours.append(hour)
                    self.record(moment.date().isoformat())
        self.assertEqual(hours, [6] * 7)


class TestBriefSchedule(unittest.TestCase):
    def test_delivered_week_does_not_rebuild_the_report(self):
        """The loop ticks all Monday; dedupe must happen before another expensive live read."""
        bot = Bot.__new__(Bot)

        class DeliveredStore:
            @staticmethod
            def already_delivered(kind, key):
                return kind == "brief"

        bot.store = DeliveredStore()

        def unexpected_rebuild():
            self.fail("an already-delivered brief triggered another report build")

        bot.build_report = unexpected_rebuild
        self.assertFalse(bot.send_brief())


if __name__ == "__main__":
    unittest.main()


class TestCommandNames(unittest.TestCase):
    """The command surface, asserted so a rename cannot silently drop one."""

    def test_expected_commands_are_all_handled(self):
        import inspect
        from csmbot.main import Bot
        source = inspect.getsource(Bot.handle_command)
        for command in ("help", "status", "capacity", "funnel", "types", "cohorts", "strikes",
                        "brief", "op", "settings", "weekly", "collect", "round", "rounds"):
            self.assertIn(f'"{command}"', source, f"/{command} is not handled")
