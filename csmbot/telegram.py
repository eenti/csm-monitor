"""Telegram delivery.

Plain HTTPS against the Bot API, no client library. The bot sends a handful of messages a week; a
dependency would add more surface area than it removes.

Two behaviours matter more than they look:

**Splitting is explicit.** Telegram rejects messages over 4096 characters. A brief that grows past
that must be split at a block boundary and labelled, never truncated — a brief that silently loses
its last section is worse than one that arrives in two parts.

**Delivery is recorded before it is claimed.** `send_once` consults the store, so a container that
restarts on a Monday morning does not send the same brief twice.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_MESSAGE = 4096
# Leave headroom for the "(1/2)" continuation marker appended when a message is split.
SPLIT_TARGET = 3900


class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = SPLIT_TARGET) -> list[str]:
    """Split on blank lines, then single lines, so a message breaks between blocks.

    Falls back to a hard character split only for a single line longer than the limit, which should
    not happen in practice but must not raise if it does.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""

    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            parts.append(current)
            current = ""

        if len(block) <= limit:
            current = block
            continue

        # A single oversized block: break it on lines, then on characters.
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    parts.append(current)
                while len(line) > limit:
                    parts.append(line[:limit])
                    line = line[limit:]
                current = line

    if current:
        parts.append(current)

    total = len(parts)
    if total > 1:
        parts = [f"{p}\n\n({i + 1}/{total})" for i, p in enumerate(parts)]
    return parts


@dataclass
class Telegram:
    token: str
    chat_id: str
    dry_run: bool = False
    timeout: int = 40
    retries: int = 3

    def _api(self, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            url, data=data, headers={"User-Agent": "csm-bot/1.0"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def send(self, text: str, reply_markup: dict | None = None) -> None:
        """Send a message, splitting if needed. Raises if any part cannot be delivered."""
        parts = split_message(text)
        for index, part in enumerate(parts):
            # An inline keyboard belongs on the final part, where the user finishes reading.
            markup = reply_markup if index == len(parts) - 1 else None
            self._send_one(part, markup)

    def _send_one(self, text: str, reply_markup: dict | None = None) -> None:
        if self.dry_run:
            print("--- telegram (dry run) ---")
            print(text)
            if reply_markup:
                print(json.dumps(reply_markup))
            print("--- end ---")
            return

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))

        last_error = None
        for attempt in range(self.retries):
            try:
                result = self._api("sendMessage", payload)
                if result.get("ok"):
                    return
                last_error = result.get("description", str(result))
                # 4xx from Telegram means the message itself is bad; retrying sends it again
                # unchanged and would only delay the failure.
                if not result.get("parameters", {}).get("retry_after"):
                    break
                time.sleep(float(result["parameters"]["retry_after"]))
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.read()[:200]!r}"
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
                time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)
                time.sleep(2 ** attempt)

        raise TelegramError(f"could not deliver message: {last_error}")

    def edit(
        self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None:
        """Edit one of the bot's messages after an inline-keyboard action."""
        if self.dry_run:
            print("--- telegram edit (dry run) ---")
            print(text)
            print("--- end ---")
            return
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        result = self._api("editMessageText", payload)
        if not result.get("ok"):
            raise TelegramError(f"could not edit settings message: {result}")

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        """Stop Telegram's button spinner, optionally with a short notification."""
        if self.dry_run:
            return
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        result = self._api("answerCallbackQuery", payload)
        if not result.get("ok"):
            raise TelegramError(f"could not answer callback: {result}")

    def send_once(self, store, kind: str, key: str, text: str) -> bool:
        """Send only if this (kind, key) has not been delivered before.

        Returns True if the message went out. The delivery is recorded after a successful send, so a
        crash mid-send results in a duplicate rather than a silent loss — the safer direction for a
        weekly brief.
        """
        if store.already_delivered(kind, key):
            return False
        self.send(text)
        store.record_delivery(kind, key, text)
        return True

    def poll(self, offset: int, timeout: int = 30) -> tuple[int, list[dict]]:
        """Long-poll for commands and buttons. Returns (next_offset, typed events).

        Long polling rather than a webhook: no inbound port, no TLS certificate, nothing to expose
        from the container. At a handful of commands a week the efficiency difference is irrelevant
        and the operational difference is not.
        """
        if self.dry_run:
            return offset, []
        try:
            result = self._api("getUpdates", {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": '["message","callback_query"]',
            })
        except (urllib.error.URLError, TimeoutError, urllib.error.HTTPError):
            # A failed poll is normal (timeouts, brief outages) and must not take the loop down.
            return offset, []

        if not result.get("ok"):
            return offset, []

        events, highest = [], offset
        for update in result.get("result", []):
            highest = max(highest, update["update_id"] + 1)
            message = update.get("message")
            if message and message.get("text"):
                events.append({"kind": "message", "message": message})
            callback = update.get("callback_query")
            if callback and callback.get("data"):
                events.append({"kind": "callback", "callback": callback})
        return highest, events

    def check(self) -> str:
        """Verify the token and chat are usable. Called at startup so misconfiguration fails fast."""
        if self.dry_run:
            return "dry-run"
        result = self._api("getMe", {})
        if not result.get("ok"):
            raise TelegramError(f"getMe failed: {result}")
        return result["result"].get("username", "unknown")
