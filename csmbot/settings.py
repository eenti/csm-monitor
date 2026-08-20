"""Runtime configuration controlled from the authorised Telegram chat.

Coolify environment variables remain the deployment defaults. Changes made through `/settings`
are deliberately limited to non-secret operational values and persisted in SQLite on `/data`, so a
redeploy does not silently undo them. Tokens and RPC credentials never pass through this module.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Any


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_ALIASES = {
    alias: index
    for index, aliases in enumerate((
        ("0", "mon", "monday"),
        ("1", "tue", "tues", "tuesday"),
        ("2", "wed", "wednesday"),
        ("3", "thu", "thur", "thurs", "thursday"),
        ("4", "fri", "friday"),
        ("5", "sat", "saturday"),
        ("6", "sun", "sunday"),
    ))
    for alias in aliases
}

SETTING_KEYS = (
    "brief_weekday",
    "brief_hour_utc",
    "collect_hour_utc",
    "assessment_rounds",
)
MAX_ROUNDS = 20
MAX_LABEL_LENGTH = 80


class SettingsError(ValueError):
    pass


class RuntimeSettings:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.defaults = {
            "brief_weekday": config.brief_weekday,
            "brief_hour_utc": config.brief_hour_utc,
            "collect_hour_utc": config.collect_hour_utc,
            "assessment_rounds": tuple(config.assessment_rounds),
        }
        self._load()

    def _load(self) -> None:
        for key in SETTING_KEYS:
            value = self.store.get_setting(key)
            if value is not None:
                self._apply(key, value, persist=False)

    def _apply(self, key: str, value: Any, persist: bool = True) -> None:
        if key in ("brief_weekday", "brief_hour_utc", "collect_hour_utc"):
            if type(value) is not int:
                raise SettingsError(f"stored {key} must be an integer")
            upper = 6 if key == "brief_weekday" else 23
            if not 0 <= value <= upper:
                raise SettingsError(f"stored {key} must be 0-{upper}")
            clean: Any = value
        elif key == "assessment_rounds":
            clean = self._validate_rounds(value)
        else:
            raise SettingsError(f"unknown setting {key!r}")

        setattr(self.config, key, clean)
        if persist:
            self.store.set_setting(key, clean)

    @staticmethod
    def _validate_rounds(value: Any) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, (list, tuple)):
            raise SettingsError("assessment rounds must be a list")
        if len(value) > MAX_ROUNDS:
            raise SettingsError(f"at most {MAX_ROUNDS} assessment rounds are allowed")

        clean: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SettingsError("each assessment round must contain a label and date")
            label, cutoff = item
            if not isinstance(label, str) or not label.strip():
                raise SettingsError("assessment round label cannot be empty")
            label = label.strip()
            if len(label) > MAX_LABEL_LENGTH:
                raise SettingsError(f"assessment round label is limited to {MAX_LABEL_LENGTH} chars")
            if label.casefold() in seen:
                raise SettingsError(f"duplicate assessment round label: {label}")
            seen.add(label.casefold())
            if not isinstance(cutoff, str):
                raise SettingsError(f"assessment round date for {label} must be YYYY-MM-DD")
            try:
                date.fromisoformat(cutoff)
            except ValueError as exc:
                raise SettingsError(f"assessment round date must be YYYY-MM-DD: {cutoff}") from exc
            clean.append((label, cutoff))
        return tuple(sorted(clean, key=lambda item: (item[1], item[0].casefold())))

    def reset(self) -> None:
        self.store.clear_settings(SETTING_KEYS)
        for key, value in self.defaults.items():
            setattr(self.config, key, value)

    # -- display ------------------------------------------------------------

    def render(self, notice: str | None = None) -> str:
        lines = [
            "⚙️ <b>CSM settings</b>",
            "",
            f"<b>Weekly brief</b>  {WEEKDAYS[self.config.brief_weekday]} "
            f"{self.config.brief_hour_utc:02d}:00 UTC",
            f"<b>Daily collection</b>  {self.config.collect_hour_utc:02d}:00 UTC",
            "",
            "<b>Assessment rounds</b>",
        ]
        if self.config.assessment_rounds:
            lines += [
                f"{index}. {html.escape(label)} · {cutoff}"
                for index, (label, cutoff) in enumerate(self.config.assessment_rounds, start=1)
            ]
        else:
            lines.append("None configured.")
        if notice:
            lines += ["", f"✅ {html.escape(notice)}"]
        lines += [
            "",
            "Use the buttons below, or:",
            "<code>/weekly tue 09:00</code>",
            "<code>/collect 08:00</code>",
            "<code>/round add 2026-09-07 ICS Round 6</code>",
            "<code>/round set 1 2026-09-08 ICS Round 6</code>",
            "<code>/round remove 1</code>",
            "",
            "<i>Changes apply immediately and survive redeploys.</i>",
        ]
        return "\n".join(lines)

    @staticmethod
    def keyboard() -> dict:
        return {"inline_keyboard": [
            [
                {"text": "◀ Brief day", "callback_data": "cfg:brief_day:-1"},
                {"text": "Brief day ▶", "callback_data": "cfg:brief_day:1"},
            ],
            [
                {"text": "− Brief time", "callback_data": "cfg:brief_hour:-1"},
                {"text": "+ Brief time", "callback_data": "cfg:brief_hour:1"},
            ],
            [
                {"text": "− Collection time", "callback_data": "cfg:collect_hour:-1"},
                {"text": "+ Collection time", "callback_data": "cfg:collect_hour:1"},
            ],
            [
                {"text": "Rounds help", "callback_data": "cfg:rounds"},
                {"text": "Reset defaults…", "callback_data": "cfg:reset"},
            ],
        ]}

    @staticmethod
    def reset_keyboard() -> dict:
        return {"inline_keyboard": [[
            {"text": "Cancel", "callback_data": "cfg:show"},
            {"text": "Reset all", "callback_data": "cfg:reset_confirm"},
        ]]}

    @staticmethod
    def back_keyboard() -> dict:
        return {"inline_keyboard": [[
            {"text": "‹ Back to settings", "callback_data": "cfg:show"},
        ]]}

    def rounds_help(self) -> str:
        return (
            "📅 <b>Assessment rounds</b>\n\n"
            "Add: <code>/round add YYYY-MM-DD Label</code>\n"
            "Edit: <code>/round set NUMBER YYYY-MM-DD Label</code>\n"
            "Remove: <code>/round remove NUMBER</code>\n\n"
            "The NUMBER is the one shown in <code>/settings</code>. Dates are UTC calendar dates."
        )

    # -- commands and callbacks --------------------------------------------

    def handle_command(self, command: str, argument: str) -> str:
        if command == "settings":
            if not argument or argument.lower() == "show":
                return self.render()
            if argument.lower() == "reset":
                self.reset()
                return self.render("Restored Coolify defaults")
            raise SettingsError("usage: /settings or /settings reset")
        if command == "weekly":
            return self._weekly(argument)
        if command == "collect":
            return self._collect(argument)
        if command == "rounds":
            return self.render()
        if command == "round":
            return self._round(argument)
        raise SettingsError(f"unknown settings command: {command}")

    def handle_callback(self, data: str) -> tuple[str, dict]:
        if data == "cfg:show":
            return self.render(), self.keyboard()
        if data == "cfg:rounds":
            return self.rounds_help(), self.back_keyboard()
        if data == "cfg:reset":
            return (
                "⚠️ <b>Reset all runtime settings?</b>\n\n"
                "Weekly schedule, collection hour and assessment rounds will return to the values "
                "from Coolify.",
                self.reset_keyboard(),
            )
        if data == "cfg:reset_confirm":
            self.reset()
            return self.render("Restored Coolify defaults"), self.keyboard()

        try:
            _, field, raw_delta = data.split(":", 2)
            delta = int(raw_delta)
        except (ValueError, TypeError) as exc:
            raise SettingsError("invalid settings button") from exc
        if delta not in (-1, 1):
            raise SettingsError("invalid settings adjustment")

        if field == "brief_day":
            value = (self.config.brief_weekday + delta) % 7
            self._apply("brief_weekday", value)
            notice = f"Weekly brief moved to {WEEKDAYS[value]}"
        elif field == "brief_hour":
            value = (self.config.brief_hour_utc + delta) % 24
            self._apply("brief_hour_utc", value)
            notice = f"Weekly brief moved to {value:02d}:00 UTC"
        elif field == "collect_hour":
            value = (self.config.collect_hour_utc + delta) % 24
            self._apply("collect_hour_utc", value)
            notice = f"Daily collection moved to {value:02d}:00 UTC"
        else:
            raise SettingsError("unknown settings button")
        return self.render(notice), self.keyboard()

    def _weekly(self, argument: str) -> str:
        parts = argument.lower().split()
        if len(parts) != 2 or parts[0] not in WEEKDAY_ALIASES:
            raise SettingsError("usage: /weekly mon 07:00 (weekday mon-sun or 0-6)")
        weekday = WEEKDAY_ALIASES[parts[0]]
        hour = _parse_hour(parts[1])
        self._apply("brief_weekday", weekday)
        self._apply("brief_hour_utc", hour)
        return self.render(f"Weekly brief set to {WEEKDAYS[weekday]} {hour:02d}:00 UTC")

    def _collect(self, argument: str) -> str:
        hour = _parse_hour(argument)
        self._apply("collect_hour_utc", hour)
        return self.render(f"Daily collection set to {hour:02d}:00 UTC")

    def _round(self, argument: str) -> str:
        action, _, rest = argument.strip().partition(" ")
        action = action.lower()
        if action == "add":
            cutoff, _, label = rest.partition(" ")
            if not cutoff or not label:
                raise SettingsError("usage: /round add YYYY-MM-DD Label")
            rounds = list(self.config.assessment_rounds) + [(label, cutoff)]
            self._apply("assessment_rounds", rounds)
            return self.render(f"Added assessment round {label}")

        if action == "remove":
            index = _parse_index(rest, len(self.config.assessment_rounds))
            rounds = list(self.config.assessment_rounds)
            label, _ = rounds.pop(index)
            self._apply("assessment_rounds", rounds)
            return self.render(f"Removed assessment round {label}")

        if action == "set":
            number, _, remainder = rest.partition(" ")
            cutoff, _, label = remainder.partition(" ")
            if not number or not cutoff or not label:
                raise SettingsError("usage: /round set NUMBER YYYY-MM-DD Label")
            index = _parse_index(number, len(self.config.assessment_rounds))
            rounds = list(self.config.assessment_rounds)
            old_label, _ = rounds[index]
            rounds[index] = (label, cutoff)
            self._apply("assessment_rounds", rounds)
            return self.render(f"Updated assessment round {old_label}")

        raise SettingsError(
            "usage: /round add YYYY-MM-DD Label, /round set NUMBER YYYY-MM-DD Label, "
            "or /round remove NUMBER"
        )


def _parse_hour(raw: str) -> int:
    clean = raw.strip()
    if clean.endswith(":00"):
        clean = clean[:-3]
    if not clean.isdigit():
        raise SettingsError("time must be a whole UTC hour, for example 07:00")
    hour = int(clean)
    if not 0 <= hour <= 23:
        raise SettingsError("hour must be 00-23 UTC")
    return hour


def _parse_index(raw: str, count: int) -> int:
    clean = raw.strip()
    if not clean.isdigit() or not 1 <= int(clean) <= count:
        raise SettingsError(f"round number must be between 1 and {count}")
    return int(clean) - 1
