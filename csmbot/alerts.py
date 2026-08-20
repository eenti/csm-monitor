"""Threshold alerts.

An alert fires on the transition into a condition and stays quiet until the condition has cleared —
`store.raise_alert` handles that, so something true for a fortnight produces one message rather than
fourteen. Escalating thresholds (30 → 14 → 7 days of runway) are separate keys, so each step gets its
own message while the underlying condition holds.

Same rules as the brief: data, no advice, written to be read at a glance.

Alerts that need history say nothing until they have it. An alert that cannot tell "no change" from
"no data" is worse than no alert, because it reads as reassurance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .brief import _link, _operator_link, _plural, ETHERSCAN_BLOCK, IPFS_VIEW
from .metrics import Report, curve_name

RUNWAY_STEPS = (30, 14, 7)
CONSTRAINT_FLIP_DAYS = 3          # consecutive days on the new side before it counts as a flip
EXIT_ANOMALY_MULTIPLE = 3.0       # daily exits above this multiple of the 30-day mean
EXIT_ANOMALY_FLOOR = 10           # ignore small absolute numbers, where a multiple means nothing
UNCLAIMED_AGE_DAYS = 14           # how long a batch may sit before its claim rate is reported
UNCLAIMED_RATE_PCT = 10.0
ROUND_NOTICE_DAYS = (14, 3)


@dataclass
class Alert:
    key: str
    text: str
    payload: dict = field(default_factory=dict)


# -- individual checks -----------------------------------------------------


def capacity_runway(report: Report) -> list[Alert]:
    runway = report.capacity.runway_days
    if runway is None:
        return []
    out = []
    for step in RUNWAY_STEPS:
        if runway < step:
            out.append(Alert(
                key=f"runway:{step}",
                text=(
                    f"⚠️ <b>Capacity runway under {step} days</b>\n"
                    f"{runway:.0f}d left · {report.capacity.rate_7d:+.1f}/day · "
                    f"headroom {report.capacity.headroom:,} val"
                ),
                payload={"runway_days": runway, "step": step},
            ))
    return out


def constraint_flip(report: Report, store) -> list[Alert]:
    """Fires when what limits CSM changes side and stays there.

    Supply-limited means room under the cap and nothing queued; capacity-limited means keys waiting
    with no room. The sustained requirement keeps a single quiet day between deposit cycles from
    reading as a regime change.
    """
    current = report.capacity.constraint
    if current == "unknown":
        return []

    history = store.metric_series("constraint", _days_ago(CONSTRAINT_FLIP_DAYS + 4), report.day)
    recent = [row["detail"] for row in history][-CONSTRAINT_FLIP_DAYS:]
    if len(recent) < CONSTRAINT_FLIP_DAYS:
        return []
    import json
    values = [json.loads(v) for v in recent if v]
    if len(values) < CONSTRAINT_FLIP_DAYS or any(v != current for v in values):
        return []

    other = "capacity" if current == "supply" else "supply"
    if not store.alert_is_active(f"constraint:{other}"):
        # First time we have ever established a side; nothing to call a flip from.
        if not any(store.alert_is_active(f"constraint:{s}") for s in ("supply", "capacity")):
            store.raise_alert(f"constraint:{current}", {"since": report.day})
            return []
    store.clear_alert(f"constraint:{other}")

    label = "key supply" if current == "supply" else "the share limit"
    return [Alert(
        key=f"constraint:{current}",
        text=(
            f"🔀 <b>CSM is now limited by {label}</b>\n"
            f"{CONSTRAINT_FLIP_DAYS} days running · headroom {report.capacity.headroom:,} val · "
            f"{report.capacity.depositable:,} keys waiting"
        ),
        payload={"constraint": current},
    )]


def frame_late(report: Report) -> list[Alert]:
    frame = report.frame
    if frame is None or not frame.is_late:
        return []
    return [Alert(
        key="frame:late",
        text=(
            f"⏰ <b>Rewards frame overdue</b>\n"
            f"{frame.hours_late:.0f}h past {frame.deadline:%d %b %H:%M} UTC"
        ),
        payload={"hours_late": frame.hours_late},
    )]


def frame_published(report: Report, store) -> list[Alert]:
    """Fires once per frame, when a new rewards tree appears."""
    performance = report.performance
    if performance is None:
        return []
    key = f"frame:published:{performance.frame_date}"
    if store.already_delivered("alert", key):
        return []

    off = (
        "" if performance.interval_days == 28
        else f" · {performance.interval_days - 28:+d}d off cadence"
    )
    lines = [
        f"💰 <b>Frame {performance.frame_date} published</b> ({performance.interval_days}d{off})",
        f"{performance.earned} earned · {performance.first_time} first time",
    ]
    if performance.stopped is not None:
        lines.append(f"{performance.stopped} stopped · {performance.resumed} resumed")
    lines.append(
        f"{performance.idle_retired + performance.idle_running} earned nothing — "
        f"{performance.idle_retired} retired · {performance.idle_running} still running"
    )
    return [Alert(key=key, text="\n".join(lines), payload={"frame": performance.frame_date})]


def ejection_risk(report: Report) -> list[Alert]:
    """Names the operators, because the point is to be able to contact them.

    The threshold is per cohort — ICS ejects at 4 strikes, everything else at 3 — so membership of
    these lists is computed against each operator's own limit.
    """
    strikes = report.strikes
    if strikes is None or not strikes.available:
        return []
    out = []

    if strikes.ejectable:
        names = " ".join(
            _operator_link(r.operator_id, r.reward_address) for r in strikes.ejectable[:12]
        )
        out.append(Alert(
            key=f"strikes:ejectable:{len(strikes.ejectable)}",
            text=(
                f"🔴 <b>{_plural(len(strikes.ejectable), 'operator')} at the "
                f"ejection threshold</b>\n"
                f"{names}"
            ),
            payload={"ids": [r.operator_id for r in strikes.ejectable]},
        ))

    if strikes.at_risk:
        names = " ".join(
            _operator_link(r.operator_id, r.reward_address) for r in strikes.at_risk[:12]
        )
        out.append(Alert(
            key=f"strikes:at_risk:{len(strikes.at_risk)}",
            text=(
                f"🟡 <b>{_plural(len(strikes.at_risk), 'operator')} within one strike "
                f"of ejection</b>\n"
                f"{names}"
            ),
            payload={"ids": [r.operator_id for r in strikes.at_risk]},
        ))
    return out


def exit_anomaly(report: Report, store) -> list[Alert]:
    """Daily validator exits well above the recent baseline."""
    series = store.metric_series("csm_exited", _days_ago(31), report.day)
    if len(series) < 8:
        return []

    values = [row["value"] for row in series if row["value"] is not None]
    if len(values) < 8:
        return []
    daily = [b - a for a, b in zip(values, values[1:])]
    latest = daily[-1]
    baseline = sum(daily[:-1]) / len(daily[:-1])

    if latest < EXIT_ANOMALY_FLOOR or baseline <= 0 or latest < baseline * EXIT_ANOMALY_MULTIPLE:
        return []
    return [Alert(
        key="exits:anomaly",
        text=(
            f"📤 <b>Exit spike</b>\n"
            f"{latest:.0f} validators exited · {baseline:.1f}/day baseline over "
            f"{len(daily) - 1}d"
        ),
        payload={"exits": latest, "baseline": baseline},
    )]


def unclaimed_batch(report: Report, store, today: str) -> list[Alert]:
    """The originating case: addresses made eligible, and nobody claiming.

    A gate's tree root changes when governance adds addresses. The first day a new root is seen dates
    the batch, and once it has had `UNCLAIMED_AGE_DAYS` to be acted on its claim rate is reported.
    """
    out = []
    for gate in report.funnel.gates:
        if not gate.available:
            continue
        batch = store.latest_gate_batch(gate.label)
        if batch is None or batch["added_count"] == 0:
            continue

        age = (date.fromisoformat(today) - date.fromisoformat(batch["first_seen"])).days
        if age < UNCLAIMED_AGE_DAYS:
            continue

        added = set(store.batch_addresses(batch))
        claimed = len(added & {a.lower() for a in gate.claimed_addresses})
        rate = claimed / len(added) * 100 if added else 0.0
        if rate > UNCLAIMED_RATE_PCT:
            continue

        listing = _link(IPFS_VIEW.format(batch["tree_cid"]), "tree")
        out.append(Alert(
            key=f"unclaimed:{gate.label}:{batch['tree_root'][:18]}",
            text=(
                f"🕳 <b>{gate.label} batch unclaimed</b>\n"
                f"{claimed}/{len(added)} claimed ({rate:.0f}%) · added "
                f"{batch['first_seen']} · {age}d ago · {listing}"
            ),
            payload={"gate": gate.label, "claimed": claimed, "added": len(added), "age": age},
        ))
    return out


def round_cutoff(config, today: str) -> list[Alert]:
    """Notice before an assessment round closes."""
    out = []
    for label, cutoff in getattr(config, "assessment_rounds", ()):
        days = (date.fromisoformat(cutoff) - date.fromisoformat(today)).days
        for notice in ROUND_NOTICE_DAYS:
            if days == notice:
                out.append(Alert(
                    key=f"round:{label}:{notice}",
                    text=f"📅 <b>{label} closes in {days} days</b>\n{cutoff}",
                    payload={"round": label, "days": days},
                ))
    return out


# -- runner ----------------------------------------------------------------


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


def evaluate(report: Report, store, config) -> list[Alert]:
    """Collect every alert that should be sent now, after dedupe.

    Ordering matters only for readability: the most urgent classes first, so a burst of alerts arrives
    worst-first.
    """
    candidates: list[Alert] = []
    candidates += frame_late(report)
    candidates += capacity_runway(report)
    candidates += ejection_risk(report)
    candidates += constraint_flip(report, store)
    candidates += exit_anomaly(report, store)
    candidates += unclaimed_batch(report, store, report.day)
    candidates += frame_published(report, store)
    candidates += round_cutoff(config, report.day)

    fresh = []
    for alert in candidates:
        # frame_published is a once-ever announcement rather than a condition that clears, so it uses
        # the delivery ledger instead of the alert-state transition.
        if alert.key.startswith("frame:published:"):
            fresh.append(alert)
            continue
        if store.raise_alert(alert.key, alert.payload):
            fresh.append(alert)
    return fresh


def clear_resolved(report: Report, store) -> None:
    """Let conditions that no longer hold fire again next time they do."""
    runway = report.capacity.runway_days
    for step in RUNWAY_STEPS:
        if runway is None or runway >= step:
            store.clear_alert(f"runway:{step}")

    if report.frame is not None and not report.frame.is_late:
        store.clear_alert("frame:late")

    strikes = report.strikes
    if strikes is not None and strikes.available:
        if not strikes.ejectable:
            for key in _keys_with_prefix(store, "strikes:ejectable:"):
                store.clear_alert(key)
        if not strikes.at_risk:
            for key in _keys_with_prefix(store, "strikes:at_risk:"):
                store.clear_alert(key)


def _keys_with_prefix(store, prefix: str) -> list[str]:
    rows = store.db.execute(
        "SELECT key FROM alert_state WHERE active=1 AND key LIKE ?", (prefix + "%",)
    ).fetchall()
    return [row["key"] for row in rows]
