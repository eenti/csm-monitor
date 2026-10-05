"""Entry point: the CLI and the run loop.

One process, one thread, one SQLite file. Collection, the weekly brief, alert evaluation and command
handling all take turns in the same loop, so there is no concurrency to reason about and no lock to
get wrong. At this volume — one collection a day, one brief a week, a handful of commands — there is
nothing to gain by making it concurrent and a fair amount to lose.

Commands:
    collect     take one snapshot and store it
    brief       assemble and send this week's brief
    alerts      evaluate alerts and send any that fired
    backfill    recover frame history from csm-rewards
    verify      re-derive a stored metric and show its provenance
    health      exit non-zero if the last collection is stale (used by the Docker healthcheck)
    run         the loop: collect daily, brief weekly, answer commands continuously
"""

from __future__ import annotations

import json
import html
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import alerts as alerts_mod
from . import backfill as backfill_mod
from . import brief as brief_mod
from . import metrics as metrics_mod
from . import sources
from .chain import Chain, to_uint, words
from .config import Config, ConfigError
from .ipfs import Ipfs
from .settings import RuntimeSettings, SettingsError
from .store import Store
from .telegram import Telegram

HEALTH_STALE_HOURS = 30
POLL_TIMEOUT = 30

# The brief is weekly, so everything it labels "this week" is diffed against a snapshot this many
# days back. History is searched a week beyond that, so a few missed collections still land on a
# real comparison rather than dropping every delta.
COMPARISON_WINDOW_DAYS = 7
COMPARISON_SEARCH_DAYS = COMPARISON_WINDOW_DAYS + 7


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}", flush=True)


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db_path)
        self.settings = RuntimeSettings(config, self.store)
        self.chain = Chain(endpoints=config.rpc_urls)
        self.ipfs = Ipfs(cache_dir=Path(config.ipfs_cache_dir), pin_token=config.pin_token)
        self.telegram = Telegram(
            token=config.telegram_token,
            chat_id=config.telegram_chat_id,
            dry_run=config.dry_run,
        )
        # The most recent snapshot, kept so a command can answer immediately. A full collection is
        # ~1,800 RPC calls and takes minutes; making /status wait for that would make the commands
        # useless. Collection is daily, so answering from cache costs at most a day of staleness —
        # which the reply states rather than hides.
        self.last_snapshot: sources.Snapshot | None = None
        self.last_snapshot_at: datetime | None = None

    # -- collection --------------------------------------------------------

    def collect(self) -> sources.Snapshot:
        """One snapshot, stored raw. Records the day as a gap if it fails, so the brief can say so."""
        day = datetime.now(timezone.utc).date().isoformat()
        try:
            snapshot = sources.collect(self.chain, self.config, self.ipfs)
        except Exception as exc:
            self.store.note_gap(day, f"{type(exc).__name__}: {exc}")
            raise

        self.last_snapshot = snapshot
        self.last_snapshot_at = datetime.now(timezone.utc)

        run_id = self.store.start_run(snapshot.day, snapshot.block, snapshot.timestamp)
        self.store.record_calls(run_id, self.chain.calls)
        self.chain.calls.clear()

        module = snapshot.module(self.config.module_id)
        put = self.store.put_metric
        put(snapshot.day, "csm_active", module.active, run_id, snapshot.block)
        put(snapshot.day, "csm_exited", module.exited, run_id, snapshot.block)
        put(snapshot.day, "csm_deposited", module.deposited, run_id, snapshot.block)
        put(snapshot.day, "csm_depositable", module.depositable, run_id, snapshot.block)
        put(snapshot.day, "csm_share_limit_bp", module.share_limit_bp, run_id, snapshot.block)
        put(snapshot.day, "total_active", snapshot.total_active, run_id, snapshot.block)
        put(snapshot.day, "operators", len(snapshot.operators), run_id, snapshot.block)

        # Per-operator curve and counters, so next week can diff against it. This is what makes
        # joins, departures and cohort moves computable at all.
        put(snapshot.day, "operator_state", None, run_id, snapshot.block, detail={
            str(o.id): {"curve_id": o.curve_id, **o.counters} for o in snapshot.operators
        })

        capacity = metrics_mod.capacity(snapshot, self.config.module_id, [])
        put(snapshot.day, "constraint", None, run_id, snapshot.block, detail=capacity.constraint)
        put(snapshot.day, "capacity_state", None, run_id, snapshot.block, detail={
            "share_pct": capacity.validator_share_pct,
            "headroom": capacity.headroom,
            "active": capacity.active,
        })

        # Who has consumed each gate, so next week's claim counts can be differenced. Sets rather
        # than counts: a count subtraction cannot tell a new claim from an address that stopped
        # being claimed, and the funnel is the block this bot exists for.
        # Gates whose tree did not verify are skipped, so a bad read cannot become next week's
        # baseline and manufacture a delta.
        put(snapshot.day, "gate_state", None, run_id, snapshot.block, detail={
            g.label: {"claimed": list(g.claimed), "eligible": len(g.eligible), "root": g.tree_root}
            for g in snapshot.gates if g.root_verified
        })

        strike_state = metrics_mod.strikes(snapshot)
        if strike_state.available:
            put(snapshot.day, "strike_state", None, run_id, snapshot.block, detail={
                "keys": strike_state.struck_keys, "operators": strike_state.struck_operators,
            })

        from . import competitors
        put(snapshot.day, "pools", None, run_id, snapshot.block,
            detail=competitors.to_record(snapshot.pools))

        self._record_gate_batches(snapshot, snapshot.day)

        status = "partial" if snapshot.warnings else "ok"
        self.store.finish_run(run_id, status, "; ".join(snapshot.warnings) or None)
        self.store.clear_gap(snapshot.day)
        log(f"collected block {snapshot.block} ({status}), {len(snapshot.operators)} operators")
        return snapshot

    def _record_gate_batches(self, snapshot: sources.Snapshot, day: str) -> None:
        """Note each new eligibility tree and which addresses it added.

        The diff is against the previous root we recorded, so the very first sighting of a gate has no
        baseline and records zero added rather than claiming the whole tree is new.
        """
        for gate in snapshot.gates:
            if not gate.eligible or not gate.root_verified:
                continue
            if self.store.has_gate_root(gate.label, gate.tree_root):
                continue
            previous = self.store.latest_gate_batch(gate.label)
            if previous is None:
                # First sighting: there is no baseline, so nothing is "added". Claiming the whole
                # tree is new would fire an unclaimed-batch alert about addresses that have been
                # eligible for months.
                added: list[str] = []
            else:
                baseline = set(self.store.batch_eligible(previous))
                added = [a for a in gate.eligible if a not in baseline]
            self.store.put_gate_batch(
                gate.label, gate.tree_root, day, gate.tree_cid, added, gate.eligible,
            )
            log(f"gate {gate.label}: new tree {gate.tree_root[:14]} (+{len(added)} addresses)")

    # -- reporting ---------------------------------------------------------

    def build_report(
        self, snapshot: sources.Snapshot | None = None, allow_cached: bool = False
    ) -> metrics_mod.Report:
        if snapshot is None and allow_cached and self.last_snapshot is not None:
            snapshot = self.last_snapshot
        if snapshot is None:
            snapshot = sources.collect(self.chain, self.config, self.ipfs)
            self.last_snapshot = snapshot
            self.last_snapshot_at = datetime.now(timezone.utc)

        history = [
            (row["day"], row["value"])
            for row in self.store.metric_series("csm_active", _days_ago(8), snapshot.day)
            if row["value"] is not None
        ]

        # One comparison day for the whole report. `operator_state` is the oldest series and the
        # one every brief needs, so it picks the day; every other series is then read at that same
        # day, or not at all. Letting each block find its own nearest snapshot would put a six-day
        # delta next to an eight-day one under a single "this week" heading.
        since = _days_ago(COMPARISON_SEARCH_DAYS)
        comparison = _pick_comparison(
            self.store.metric_series("operator_state", since, snapshot.day), snapshot.day
        )
        comparison_day = comparison["day"] if comparison else None
        previous_operators = (
            {int(k): v for k, v in json.loads(comparison["detail"]).items()} if comparison else None
        )

        def at_comparison(name: str):
            if comparison_day is None:
                return None
            row = self.store.get_metric(comparison_day, name)
            return json.loads(row["detail"]) if row and row["detail"] else None

        missing = [row["day"] for row in self.store.gaps_between(_days_ago(8), snapshot.day)]

        return metrics_mod.build(
            snapshot,
            self.config.module_id,
            active_history=history,
            previous_operators=previous_operators,
            comparison_day=comparison_day,
            missing_days=missing,
            frame_deltas=backfill_mod.deltas(self.store),
            previous_pools=at_comparison("pools"),
            previous_gates=at_comparison("gate_state"),
            previous_capacity=at_comparison("capacity_state"),
            previous_strikes=at_comparison("strike_state"),
        )

    def send_brief(self, force: bool = False) -> bool:
        key = brief_mod.week_key()
        # The scheduler checks every loop tick after the configured Monday hour. Avoid rebuilding the
        # report (and repeating thousands of RPC reads) once this week's brief is already delivered.
        if not force and self.store.already_delivered("brief", key):
            return False

        report = self.build_report()
        text = brief_mod.render(report)
        if force:
            self.telegram.send(text)
            self.store.record_delivery("brief", f"{key}:forced:{report.block}", text)
            return True
        sent = self.telegram.send_once(self.store, "brief", key, text)
        log(f"brief {key}: {'sent' if sent else 'already delivered'}")
        return sent

    def send_alerts(self, snapshot: sources.Snapshot | None = None) -> int:
        report = self.build_report(snapshot)
        alerts_mod.clear_resolved(report, self.store)
        fired = alerts_mod.evaluate(report, self.store, self.config)
        for alert in fired:
            if alert.key.startswith("frame:published:"):
                self.telegram.send_once(self.store, "alert", alert.key, alert.text)
            else:
                self.telegram.send(alert.text)
        if fired:
            log(f"sent {len(fired)} alerts: {[a.key for a in fired]}")
        return len(fired)

    # -- commands ----------------------------------------------------------

    def handle_command(self, text: str) -> str:
        command, _, argument = text.strip().partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        argument = argument.strip()

        if command in ("start", "help"):
            return (
                "📊 <b>CSM bot</b>\n"
                "/status — headline plus capacity\n"
                "/capacity — share, headroom, what is limiting it\n"
                "/funnel — claim funnel per gate\n"
                "/types — NOs by type\n"
                "/strikes — ejection risk\n"
                "/op &lt;id&gt; — one operator\n"
                "/brief — this week's brief now\n"
                "/settings — schedules and assessment rounds"
            )

        if command in ("settings", "weekly", "collect", "round", "rounds"):
            try:
                return self.settings.handle_command(command, argument)
            except SettingsError as exc:
                return f"⚠️ <b>Settings</b>\n{html.escape(str(exc))}"

        report_commands = {
            "status", "capacity", "funnel", "types", "cohorts", "strikes", "brief", "op",
        }
        if command not in report_commands:
            shown = f"<code>/{html.escape(command)}</code>" if command else "that command"
            return f"Unknown command: {shown}. Use /help to see what I can do."
        if command == "op" and not argument.isdigit():
            return "Usage: /op &lt;operator id&gt;"

        report = self.build_report(allow_cached=True)
        stamp = self._freshness()

        if command == "status":
            return "\n\n".join([
                f"📊 <b>CSM · {report.day}</b>\n{brief_mod.headline(report)}",
                "\n".join(brief_mod._capacity_block(report)),
                stamp,
            ])
        if command == "capacity":
            return "\n".join(brief_mod._capacity_block(report)) + f"\n\n{stamp}"
        if command == "funnel":
            return "\n".join(brief_mod._funnel_block(report)) + f"\n\n{stamp}"
        # /cohorts kept as an alias: it is what this was called first and muscle memory is real.
        if command in ("types", "cohorts"):
            return "\n".join(brief_mod._operators_block(report)) + f"\n\n{stamp}"
        if command == "strikes":
            block = brief_mod._strikes_block(report)
            return ("\n".join(block) + f"\n\n{stamp}") if block else "No strike data."
        if command == "brief":
            return brief_mod.render(report)
        if command == "op":
            return self._operator_detail(argument)
        raise AssertionError(f"unhandled report command: {command}")

    def _freshness(self) -> str:
        """Every cached answer says how old it is. A stale number presented as current is the exact
        failure this bot is built to avoid."""
        if self.last_snapshot is None or self.last_snapshot_at is None:
            return "<i>fresh read</i>"
        age = (datetime.now(timezone.utc) - self.last_snapshot_at).total_seconds() / 3600
        if age < 1:
            return f"<i>block {self.last_snapshot.block:,} · {age * 60:.0f}m old</i>"
        return f"<i>block {self.last_snapshot.block:,} · {age:.1f}h old</i>"

    def _operator_detail(self, argument: str) -> str:
        if not argument.isdigit():
            return "Usage: /op &lt;operator id&gt;"
        wanted = int(argument)
        snapshot = self.last_snapshot or sources.collect(self.chain, self.config, self.ipfs)
        match = next((o for o in snapshot.operators if o.id == wanted), None)
        if match is None:
            return f"Operator #{wanted} was not found."

        counters = match.counters
        struck = []
        if snapshot.strikes and snapshot.strikes.available:
            struck = [k.strikes for k in snapshot.strikes.keys if k.operator_id == wanted]

        lines = [
            f"👤 <b>Operator {wanted}</b> · {metrics_mod.curve_name(match.curve_id)}",
            f"{match.active_keys} active · {counters['totalAddedKeys']} added · "
            f"{counters['totalDepositedKeys']} funded · {counters['totalExitedKeys']} exited · "
            f"{counters['totalWithdrawnKeys']} withdrawn",
            f"{counters['depositableValidatorsCount']} depositable · "
            f"{counters['enqueuedCount']} queued",
        ]
        if struck:
            threshold = (snapshot.strikes.params.get(match.curve_id) or (0, 0))[1]
            lines.append(f"⚡ {len(struck)} struck keys · worst {max(struck)}/{threshold}")
        if match.has_departed:
            lines.append("↘ all funded keys withdrawn")
        lines.append(
            brief_mod._link(
                brief_mod.ETHERSCAN_ADDRESS.format(match.reward_address), "reward address"
            )
        )
        lines.append(self._freshness())
        return "\n".join(lines)

    # -- verification ------------------------------------------------------

    def verify(self, metric: str, day: str) -> str:
        row = self.store.get_metric(day, metric)
        if row is None:
            return f"No stored value for {metric} on {day}."
        raw = self.store.raw_for_run(row["run_id"])
        ok = sum(1 for r in raw if r["ok"])
        lines = [
            f"metric   {metric}",
            f"day      {day}",
            f"value    {row['value'] if row['value'] is not None else row['detail']}",
            f"block    {row['block_number']}",
            f"run      {row['run_id']} · {len(raw)} recorded calls ({ok} ok)",
            "",
            "sample of the reads behind it:",
        ]
        for record in raw[:8]:
            lines.append(
                f"  {record['selector']} {record['target'][:10]}…{record['target'][-4:]} "
                f"@ {record['block_number']} -> {_summarise(record)}"
            )
        if len(raw) > 8:
            lines.append(f"  … {len(raw) - 8} more")
        return "\n".join(lines)

    # -- loop --------------------------------------------------------------

    def _collection_due(self, now: datetime) -> bool:
        """One collection per UTC day, at a fixed hour.

        Driven by what the store already holds rather than by elapsed time. An interval-based trigger
        drifts — 20 hours between runs walks the reading backwards through the day — and a daily rate
        computed from readings taken at different times of day carries that drift as noise. Since the
        rate feeds the capacity runway alert, that noise ends up in a threshold.

        Reading from the store also makes restarts and downtime self-correcting: if the container was
        down at the collection hour, the next tick sees no run for today and collects immediately.
        """
        if now.hour < self.config.collect_hour_utc:
            return False
        today = now.date().isoformat()
        existing = [
            row for row in self.store.runs_between(today, today)
            if row["status"] in ("ok", "partial")
        ]
        return not existing

    def health(self) -> int:
        last = self.store.last_successful_run()
        if last is None:
            log("health: no successful run yet")
            return 1
        started = datetime.fromisoformat(last["started_at"])
        age = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if age > HEALTH_STALE_HOURS:
            log(f"health: last run {age:.1f}h ago, stale")
            return 1
        log(f"health: last run {age:.1f}h ago")
        return 0

    def run(self) -> None:
        log(f"starting · bot @{self.telegram.check()} · db {self.config.db_path}")
        log(f"brief on weekday {self.config.brief_weekday} at {self.config.brief_hour_utc:02d}:00 UTC")

        try:
            summary = backfill_mod.run(self.store)
            log(f"backfill: {summary['added']} frames added, {summary['already_held']} held")
        except Exception as exc:
            log(f"backfill failed (continuing): {exc}")

        offset = 0
        retry_after = datetime.now(timezone.utc)
        log(f"collection at {self.config.collect_hour_utc:02d}:00 UTC, once per day")

        while True:
            now = datetime.now(timezone.utc)

            if self._collection_due(now) and now >= retry_after:
                try:
                    snapshot = self.collect()
                    self.send_alerts(snapshot)
                except Exception as exc:
                    log(f"collection failed: {exc}\n{traceback.format_exc()}")
                    # Back off for an hour. A failing RPC will still be failing in a minute, and a
                    # tight retry loop turns one outage into a rate-limit ban. The gap is already
                    # recorded, so the brief will say the day is missing if it never succeeds.
                    retry_after = now + timedelta(hours=1)

            if (
                now.weekday() == self.config.brief_weekday
                and now.hour >= self.config.brief_hour_utc
            ):
                try:
                    self.send_brief()
                except Exception as exc:
                    log(f"brief failed: {exc}")

            try:
                offset, events = self.telegram.poll(offset, timeout=POLL_TIMEOUT)
                for event in events:
                    if event["kind"] == "callback":
                        self._handle_callback(event["callback"])
                        continue

                    message = event["message"]
                    text = message.get("text", "")
                    if not text.startswith("/"):
                        continue
                    sender = str(message.get("chat", {}).get("id"))
                    # Only the configured chat is answered. Anyone who finds the bot otherwise gets
                    # nothing back.
                    if sender != str(self.config.telegram_chat_id):
                        log(f"ignoring command from chat {sender}")
                        continue
                    # Arguments may contain operational data. Log the command name, never its body.
                    command_name = text.split(maxsplit=1)[0].split("@")[0]
                    log(f"command: {command_name}")
                    try:
                        is_settings = command_name.lstrip("/").lower() in (
                            "settings", "weekly", "collect", "round", "rounds"
                        )
                        self.telegram.send(
                            self.handle_command(text),
                            reply_markup=self.settings.keyboard() if is_settings else None,
                        )
                    except Exception as exc:
                        log(f"command failed: {exc}")
                        self.telegram.send(f"⚠️ {type(exc).__name__}: {exc}")
            except Exception as exc:
                log(f"poll failed: {exc}")
                time.sleep(5)

    def _handle_callback(self, query: dict) -> None:
        """Apply a settings-button action, but only in the configured chat."""
        callback_id = str(query.get("id", ""))
        message = query.get("message") or {}
        sender = str(message.get("chat", {}).get("id"))
        if sender != str(self.config.telegram_chat_id):
            log(f"ignoring settings callback from chat {sender}")
            if callback_id:
                self.telegram.answer_callback(callback_id, "Not authorised in this chat")
            return

        try:
            text, markup = self.settings.handle_callback(str(query.get("data", "")))
            # Telegram keeps a progress spinner visible until answerCallbackQuery is called.
            self.telegram.answer_callback(callback_id)
            self.telegram.edit(sender, int(message["message_id"]), text, markup)
        except SettingsError as exc:
            self.telegram.answer_callback(callback_id, str(exc))
        except Exception as exc:
            log(f"settings callback failed: {exc}")
            if callback_id:
                try:
                    self.telegram.answer_callback(callback_id, "Could not update settings")
                except Exception:
                    pass


def _summarise(record) -> str:
    """A one-line, actually informative view of a stored response.

    Truncating the raw hex is useless: ABI returns are left-padded, so the first forty characters of
    every call are indistinguishable zeros. Decoding the leading words instead shows the values the
    metric was built from, which is the whole point of being able to verify one.
    """
    if not record["ok"]:
        return f"FAILED {record['error']}"
    response = record["response"] or ""
    try:
        parts = words(response)
    except ValueError:
        return f"{len(response)} chars (not word-aligned)"
    if not parts:
        return "empty"
    head = ", ".join(str(to_uint(w)) for w in parts[:3])
    suffix = f" (+{len(parts) - 3} words)" if len(parts) > 3 else ""
    return f"[{head}]{suffix}"


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


def _pick_comparison(rows, day: str, window_days: int = COMPARISON_WINDOW_DAYS):
    """The snapshot to diff against: the newest one at or before `day - window_days`.

    **This used to take the newest prior row, which made every "this week" line a day-over-day
    delta.** Collection runs daily, so the newest prior row is always yesterday. Checked against the
    week of 2026-08-24, when two operators joined on the Monday, one moved ICS to IDVTC on the
    Wednesday, and three gate claims landed between them: a Monday brief comparing against Sunday
    would have reported no movement at all, in the one block this bot exists for.

    Falls back to the oldest snapshot in range when history is shorter than the window. The report
    carries the span it actually covers, so a short or stretched window is stated rather than
    presented as a week.
    """
    usable = [r for r in rows if r["detail"] and r["day"] != day]
    if not usable:
        return None
    target = (date.fromisoformat(day) - timedelta(days=window_days)).isoformat()
    at_or_before = [r for r in usable if r["day"] <= target]
    return at_or_before[-1] if at_or_before else usable[0]


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "run"

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    bot = Bot(config)

    if command == "collect":
        bot.collect()
        return 0
    if command == "brief":
        bot.send_brief(force="--force" in argv)
        return 0
    if command == "alerts":
        bot.send_alerts()
        return 0
    if command == "backfill":
        print(json.dumps(backfill_mod.run(bot.store), indent=2))
        return 0
    if command == "verify":
        metric = argv[argv.index("--metric") + 1] if "--metric" in argv else "csm_active"
        day = argv[argv.index("--date") + 1] if "--date" in argv else date.today().isoformat()
        print(bot.verify(metric, day))
        return 0
    if command == "health":
        return bot.health()
    if command == "run":
        bot.run()
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
