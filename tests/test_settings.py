"""Runtime settings: validation, persistence and Telegram-facing commands."""

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from csmbot.main import Bot
from csmbot.settings import RuntimeSettings, SettingsError
from csmbot.store import Store
from csmbot.telegram import Telegram


@dataclass
class FakeConfig:
    brief_weekday: int = 0
    brief_hour_utc: int = 7
    collect_hour_utc: int = 6
    assessment_rounds: tuple[tuple[str, str], ...] = (
        ("ICS Round 6", "2026-09-07"),
        ("IDVTC Round 2", "2026-09-21"),
    )


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "settings.db")
        self.config = FakeConfig()
        self.settings = RuntimeSettings(self.config, self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestPersistence(SettingsCase):
    def test_changes_survive_a_new_runtime_instance(self):
        self.settings.handle_command("weekly", "tue 09:00")
        self.settings.handle_command("collect", "08")

        restarted = FakeConfig()
        RuntimeSettings(restarted, self.store)
        self.assertEqual(restarted.brief_weekday, 1)
        self.assertEqual(restarted.brief_hour_utc, 9)
        self.assertEqual(restarted.collect_hour_utc, 8)

    def test_reset_restores_environment_defaults_and_clears_overrides(self):
        self.settings.handle_command("weekly", "fri 20")
        self.settings.handle_command("round", "remove 1")
        self.settings.reset()

        self.assertEqual(self.config.brief_weekday, 0)
        self.assertEqual(self.config.brief_hour_utc, 7)
        self.assertEqual(self.config.assessment_rounds, FakeConfig().assessment_rounds)
        count = self.store.db.execute("SELECT COUNT(*) c FROM runtime_settings").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_store_setting_round_trips_json(self):
        value = [["Round A", "2026-10-01"]]
        self.store.set_setting("assessment_rounds", value)
        self.assertEqual(self.store.get_setting("assessment_rounds"), value)


class TestScheduleCommands(SettingsCase):
    def test_weekly_accepts_names_numbers_and_whole_hours(self):
        self.settings.handle_command("weekly", "sun 23:00")
        self.assertEqual((self.config.brief_weekday, self.config.brief_hour_utc), (6, 23))
        self.settings.handle_command("weekly", "2 04")
        self.assertEqual((self.config.brief_weekday, self.config.brief_hour_utc), (2, 4))

    def test_invalid_time_does_not_mutate_the_schedule(self):
        before = (self.config.brief_weekday, self.config.brief_hour_utc)
        with self.assertRaises(SettingsError):
            self.settings.handle_command("weekly", "tue 09:30")
        self.assertEqual((self.config.brief_weekday, self.config.brief_hour_utc), before)

    def test_buttons_wrap_weekdays_and_hours(self):
        self.settings.handle_callback("cfg:brief_day:-1")
        self.settings.handle_callback("cfg:brief_hour:-1")
        self.settings.handle_callback("cfg:collect_hour:-1")
        self.assertEqual(self.config.brief_weekday, 6)
        self.assertEqual(self.config.brief_hour_utc, 6)
        self.assertEqual(self.config.collect_hour_utc, 5)

        self.config.brief_hour_utc = 23
        self.settings.handle_callback("cfg:brief_hour:1")
        self.assertEqual(self.config.brief_hour_utc, 0)

    def test_render_shows_only_utc_schedule(self):
        self.settings.handle_command("weekly", "mon 20")
        rendered = self.settings.render()
        self.assertIn("Monday 20:00 UTC", rendered)
        self.assertIn("Daily collection</b>  06:00 UTC", rendered)
        self.assertNotIn("UTC+7", rendered)


class TestRoundCommands(SettingsCase):
    def test_add_edit_and_remove(self):
        self.settings.handle_command("round", "add 2026-10-05 New Round")
        self.assertIn(("New Round", "2026-10-05"), self.config.assessment_rounds)

        index = [label for label, _ in self.config.assessment_rounds].index("New Round") + 1
        self.settings.handle_command("round", f"set {index} 2026-10-06 Renamed Round")
        self.assertIn(("Renamed Round", "2026-10-06"), self.config.assessment_rounds)

        index = [label for label, _ in self.config.assessment_rounds].index("Renamed Round") + 1
        self.settings.handle_command("round", f"remove {index}")
        self.assertNotIn("Renamed Round", [label for label, _ in self.config.assessment_rounds])

    def test_invalid_date_and_duplicate_label_are_rejected(self):
        before = self.config.assessment_rounds
        with self.assertRaises(SettingsError):
            self.settings.handle_command("round", "add 2026-02-30 Impossible")
        with self.assertRaises(SettingsError):
            self.settings.handle_command("round", "add 2026-10-01 ics round 6")
        self.assertEqual(self.config.assessment_rounds, before)

    def test_labels_are_html_escaped(self):
        self.settings.handle_command("round", "add 2026-10-01 A < B")
        rendered = self.settings.render()
        self.assertIn("A &lt; B", rendered)
        self.assertNotIn("A < B", rendered)


class TestBotIntegration(SettingsCase):
    def test_settings_command_never_triggers_a_chain_read(self):
        bot = Bot.__new__(Bot)
        bot.settings = self.settings

        def unexpected_report(*args, **kwargs):
            self.fail("/settings triggered a live report build")

        bot.build_report = unexpected_report
        self.assertIn("CSM settings", bot.handle_command("/settings"))

    def test_settings_callback_is_limited_to_the_configured_chat(self):
        bot = Bot.__new__(Bot)
        bot.settings = self.settings
        bot.config = type("Config", (), {"telegram_chat_id": "123"})()

        class FakeTelegram:
            def __init__(self):
                self.answers = []
                self.edits = []

            def answer_callback(self, callback_id, text=None):
                self.answers.append((callback_id, text))

            def edit(self, chat_id, message_id, text, markup):
                self.edits.append((chat_id, message_id, text, markup))

        bot.telegram = FakeTelegram()
        original = self.config.brief_hour_utc
        bot._handle_callback({
            "id": "outside",
            "data": "cfg:brief_hour:1",
            "message": {"message_id": 7, "chat": {"id": 999}},
        })
        self.assertEqual(self.config.brief_hour_utc, original)
        self.assertEqual(bot.telegram.edits, [])
        self.assertIn("Not authorised", bot.telegram.answers[0][1])

        bot._handle_callback({
            "id": "inside",
            "data": "cfg:brief_hour:1",
            "message": {"message_id": 8, "chat": {"id": 123}},
        })
        self.assertEqual(self.config.brief_hour_utc, original + 1)
        self.assertEqual(bot.telegram.edits[0][0:2], ("123", 8))


class TestTelegramSettingsTransport(unittest.TestCase):
    def test_send_serializes_inline_keyboard(self):
        telegram = Telegram("token", "123")
        calls = []

        def fake_api(method, payload):
            calls.append((method, payload))
            return {"ok": True}

        telegram._api = fake_api
        markup = {"inline_keyboard": [[{"text": "Next", "callback_data": "cfg:show"}]]}
        telegram.send("settings", reply_markup=markup)

        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(json.loads(calls[0][1]["reply_markup"]), markup)

    def test_poll_returns_messages_and_callbacks_as_typed_events(self):
        telegram = Telegram("token", "123")

        def fake_api(method, payload):
            self.assertEqual(method, "getUpdates")
            self.assertIn("callback_query", payload["allowed_updates"])
            return {"ok": True, "result": [
                {"update_id": 4, "message": {"text": "/settings", "chat": {"id": 123}}},
                {"update_id": 5, "callback_query": {
                    "id": "cb", "data": "cfg:show",
                    "message": {"message_id": 8, "chat": {"id": 123}},
                }},
            ]}

        telegram._api = fake_api
        offset, events = telegram.poll(0)
        self.assertEqual(offset, 6)
        self.assertEqual([event["kind"] for event in events], ["message", "callback"])

    def test_edit_and_callback_use_the_expected_api_methods(self):
        telegram = Telegram("token", "123")
        calls = []
        telegram._api = lambda method, payload: calls.append((method, payload)) or {"ok": True}

        telegram.answer_callback("cb")
        telegram.edit("123", 8, "updated", RuntimeSettings.keyboard())
        self.assertEqual([method for method, _ in calls], ["answerCallbackQuery", "editMessageText"])


if __name__ == "__main__":
    unittest.main()
