"""Assembling the weekly brief.

**No AI here, by requirement.** The headline is picked by walking an ordered list of conditions and
taking the first that holds, then filling a template with real numbers. Same report in, same sentence
out, forever. The ordering *is* the editorial judgement, made once and written down, rather than
re-decided by a model every Monday.

Written to be scanned, not read. Every section is one emoji and one word, every line leads with a
symbol or a number, and prose is confined to the expandable blocks where it does not cost anything.
The test for a line is whether it survives being glanced at — a sentence that needs reading twice gets
cut or moved into a blockquote.

Data only, no advice: what the numbers are, never what to do about them.

Links point at the cheapest next step. An operator id links to its reward address on Etherscan,
because that is what you need to look someone up or contact them; a gate links to its eligibility tree.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from .metrics import Performance, Report, curve_name

RUNWAY_ALERT_DAYS = 30
ETHERSCAN_ADDRESS = "https://etherscan.io/address/{}"
ETHERSCAN_BLOCK = "https://etherscan.io/block/{}"
IPFS_VIEW = "https://ipfs.io/ipfs/{}"

# Short cohort labels. The full names are too wide for a glanceable line.
SHORT_CURVE = {0: "Default", 1: "Legacy", 2: "ICS", 3: "IDVTC"}


def _short(curve_id: int) -> str:
    return SHORT_CURVE.get(curve_id, f"c{curve_id}")


def _pct(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """`3 operators` / `1 operator`. Small, but these strings get quoted at people."""
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


def _waiting(count: int) -> str:
    """Reads correctly at zero and one. This phrase sits on the binding-constraint line, which is the
    one most likely to be quoted at someone."""
    if count == 0:
        return "no keys waiting"
    if count == 1:
        return "1 key waiting"
    return f"{count:,} keys waiting"


def _link(url: str, label: str) -> str:
    return f'<a href="{url}">{html.escape(label)}</a>'


def _operator_link(operator_id: int, reward_address: str) -> str:
    return _link(ETHERSCAN_ADDRESS.format(reward_address), f"#{operator_id}")


# -- headline --------------------------------------------------------------
#
# Ordered most to least urgent; first match wins. Kept to one short sentence — it is the line that has
# to work when the notification is all that gets seen.


def headline(report: Report) -> str:
    capacity = report.capacity
    operators = report.operators
    frame = report.frame
    strikes = report.strikes

    broken = [g for g in report.funnel.gates if not g.integrity_ok]
    if broken:
        gate = broken[0]
        return (
            f"⚠️ {gate.label} funnel integrity failed — {gate.claimed} claims vs "
            f"{gate.operators_on_curve} operators. Funnel numbers unverified."
        )

    unavailable = [g for g in report.funnel.gates if not g.available]
    if unavailable:
        return f"⚠️ {unavailable[0].label} eligibility tree unreadable — funnel unknown."

    if frame is not None and frame.is_late:
        return f"⚠️ Rewards frame {frame.hours_late:.0f}h past deadline."

    if capacity.runway_days is not None and capacity.runway_days < RUNWAY_ALERT_DAYS:
        return f"⚠️ {capacity.runway_days:.0f} days of capacity left at {capacity.rate_7d:+.1f}/day."

    if capacity.constraint == "capacity":
        return f"⚠️ At the share limit — {capacity.depositable:,} keys waiting."

    if strikes is not None and strikes.ejectable:
        return (
            f"⚠️ {_plural(len(strikes.ejectable), 'operator')} at the ejection threshold."
        )

    zero_claim = [g for g in report.funnel.gates if g.available and g.unclaimed and not g.claimed]
    if zero_claim:
        gate = zero_claim[0]
        return f"None of the {gate.eligible} addresses on the {gate.label} gate have claimed."

    if operators.departed_ids is not None and operators.new_ids is not None:
        joined, left = len(operators.new_ids), len(operators.departed_ids)
        net = joined - left
        if net < 0:
            ics = operators.departed_by_curve.get(2, 0)
            return f"Operators down {abs(net)} — {left} left, {joined} joined" + (
                f", {ics} ICS." if ics else "."
            )
        if joined or left:
            return f"{joined} joined · {left} left · net {_signed(net)}"

    largest = max(
        (g for g in report.funnel.gates if g.available and g.unclaimed > 0),
        key=lambda g: g.unclaimed, default=None,
    )
    if largest is not None:
        return f"{largest.unclaimed} of {largest.eligible} {largest.label} addresses unclaimed."

    return "No material change."


# -- blocks ---------------------------------------------------------------


def _operators_block(report: Report) -> list[str]:
    operators = report.operators
    lines = ["👥 <b>Operators</b>"]

    if operators.new_ids is not None and operators.departed_ids is not None:
        if operators.new_ids:
            detail = " · ".join(
                f"{_short(c)} +{n}" for c, n in sorted(operators.new_by_curve.items())
            )
            lines.append(f"↗ {len(operators.new_ids)} joined — {detail}")
        if operators.departed_ids:
            detail = " · ".join(
                f"{_short(c)} −{n}" for c, n in sorted(operators.departed_by_curve.items())
            )
            lines.append(f"↘ {len(operators.departed_ids)} withdrew all keys — {detail}")
        if operators.curve_changes:
            moves: dict[tuple[int, int], int] = {}
            for change in operators.curve_changes:
                moves[(change.from_curve, change.to_curve)] = (
                    moves.get((change.from_curve, change.to_curve), 0) + 1
                )
            detail = " · ".join(
                f"{_short(a)}→{_short(b)} {n}" for (a, b), n in sorted(moves.items())
            )
            lines.append(f"⇄ {len(operators.curve_changes)} changed type — {detail}")
        if not (operators.new_ids or operators.departed_ids or operators.curve_changes):
            lines.append(f"→ no movement since {report.comparison_day}")
    else:
        lines.append("→ first run, movement starts next week")

    lines.append(
        f"{operators.active} active · {operators.active_keys:,} keys · "
        f"{operators.total} registered · {operators.never_funded} never funded"
    )

    # No mutability footnote here: the caveat matters for how the numbers are *computed* — which is
    # why no ratio over a mutable cohort exists anywhere — and it is documented in
    # `metrics.operators` and the handoff. Repeating it weekly to a reader who wrote the rules is
    # just a line to skip past.
    # First line is a summary, not the column header. Telegram shows the head of a collapsed
    # blockquote, so that line is the only one most readings will see — spending it on the words
    # "type NOs active gone keys" wastes it.
    summary = " · ".join(
        f"{_short(c.curve_id)} {c.active}" for c in operators.cohorts if c.active
    )
    rows = [f"active by type — {summary}", ""]
    rows.append(f"{'type':<9}{'NOs':>5}{'active':>8}{'gone':>6}{'keys':>8}")
    for cohort in operators.cohorts:
        rows.append(
            f"{_short(cohort.curve_id):<9}{cohort.total:>5}{cohort.active:>8}"
            f"{cohort.departed:>6}{cohort.active_keys:>8,}"
        )
    lines.append(f"<blockquote expandable><pre>{chr(10).join(rows)}</pre></blockquote>")
    return lines


def _funnel_block(report: Report) -> list[str]:
    lines = ["🎯 <b>Claim funnel</b>"]
    for gate in report.funnel.gates:
        if not gate.available:
            lines.append(f"{gate.label} — tree unreadable")
            continue
        tree = _link(IPFS_VIEW.format(gate.tree_cid), "list")
        lines.append(
            f"{gate.label} {gate.claimed}/{gate.eligible} · {_pct(gate.claim_rate)} · "
            f"{gate.unclaimed} open · {tree}"
        )
    return lines


def _capacity_block(report: Report) -> list[str]:
    capacity = report.capacity
    lines = ["📦 <b>Capacity</b>"]
    # Reported as a percentage against the cap, with the ETH figure alongside. The cap is enforced on
    # validator counts while other modules run 0x02 validators holding more than 32 ETH, so this ratio
    # is not a share of stake — hence the ETH number rather than a qualifier nobody wants to read.
    lines.append(
        f"{_pct(capacity.validator_share_pct, 2)} of {_pct(capacity.limit_pct, 2)} cap · "
        f"{capacity.csm_stake_eth:,} ETH"
    )
    lines.append(f"Headroom {capacity.headroom:,} val · {capacity.headroom_eth:,} ETH")
    if capacity.not_yet_contributing is not None and capacity.not_yet_contributing >= 1:
        # Verified against the beacon chain's pending-deposit queue: this gap is predominantly
        # validators awaiting activation, not balance shortfall. See ModuleState.balance_gap_validators.
        lines.append(
            f"~{capacity.not_yet_contributing:,.0f} val awaiting activation"
        )
    if capacity.rate_7d is None:
        lines.append("→ rate needs a week of history")
    else:
        tail = (
            f" → {capacity.runway_days:.0f}d to cap"
            if capacity.runway_days is not None else " → flat"
        )
        lines.append(f"{capacity.rate_7d:+.1f}/day over {capacity.window_days}d{tail}")

    label = {
        "supply": "key supply", "capacity": "the share limit", "unknown": "not determinable",
    }[capacity.constraint]
    lines.append(f"Limited by <b>{label}</b> · {_waiting(capacity.depositable)}")
    return lines


def _strikes_block(report: Report) -> list[str]:
    strikes = report.strikes
    if strikes is None:
        return []
    if not strikes.available:
        return ["⚡ <b>Strikes</b>", "→ tree unreadable"]
    if not strikes.struck_keys:
        return ["⚡ <b>Strikes</b>", "→ none recorded"]

    lines = ["⚡ <b>Strikes</b>"]
    if strikes.ejectable:
        lines.append(
            f"🔴 {len(strikes.ejectable)} at the ejection threshold · "
            + " ".join(_operator_link(r.operator_id, r.reward_address)
                       for r in strikes.ejectable[:8])
        )
    if strikes.at_risk:
        lines.append(
            f"🟡 {len(strikes.at_risk)} within one strike · "
            + " ".join(_operator_link(r.operator_id, r.reward_address)
                       for r in strikes.at_risk[:8])
        )
    tail = (
        f" · {strikes.departed_with_strikes} already left"
        if strikes.departed_with_strikes else ""
    )
    lines.append(f"{strikes.struck_keys:,} keys · {strikes.struck_operators} operators{tail}")

    # "worst key" is the highest strike count on any single key, which is what decides ejectability.
    # The ejection limit is not a column: it is implied by the type, and the reader sets those limits.
    ranked = (strikes.ejectable + strikes.at_risk)[:20]
    summary = " · ".join(
        f"#{r.operator_id} {r.worst_key}/{r.threshold}" for r in ranked[:5]
    )
    rows = [f"closest to ejection — {summary}", ""]
    rows.append(f"{'NO':>6}{'type':>9}{'keys':>6}{'worst key':>11}")
    for risk in ranked:
        rows.append(
            f"{risk.operator_id:>6}{_short(risk.curve_id):>9}{risk.struck_keys:>6}"
            f"{risk.worst_key:>11}"
        )
    lines.append(f"<blockquote expandable><pre>{chr(10).join(rows)}</pre></blockquote>")
    return lines


def _pools_block(report: Report) -> list[str]:
    """Other permissionless pools. Counts and movement, nothing else — these are reference points,
    not a scoreboard."""
    if not report.pools:
        return []
    # Collapsed by default. These are reference points rather than something to act on, so they
    # should cost no vertical space until asked for.
    # The visible line is the week's movement. The levels barely change week to week and the
    # concentration ratio is structural, so both belong below the fold.
    headline_bits, detail = [], []
    for pool in report.pools:
        if pool.validators is None:
            headline_bits.append(f"{pool.name} unreadable")
            continue
        moves = []
        if pool.node_change is not None:
            moves.append(f"NOs {_signed(pool.node_change)}")
        if pool.validator_change is not None:
            moves.append(f"val {_signed(pool.validator_change)}")
        headline_bits.append(
            f"{pool.name} {' · '.join(moves)}" if moves
            else f"{pool.name} {pool.validators:,} val"
        )
        per = pool.validators_per_operator
        density = f" · {per:.1f} val/NO" if per is not None else ""
        detail.append(
            f"{pool.name}: {pool.nodes:,} NOs · {pool.validators:,} val · "
            f"{pool.staked_eth:,} ETH{density}"
        )
        if pool.note:
            detail.append(f"  ({pool.note})")

    rows = ["this week — " + " · ".join(headline_bits), ""] + detail
    return [
        "🌐 <b>Other permissionless pools</b>",
        f"<blockquote expandable><pre>{chr(10).join(rows)}</pre></blockquote>",
    ]


def _frame_interval_label(interval_days: int) -> str:
    if interval_days == 28:
        return "28 days"
    return f"{interval_days} days; expected 28"


def _performance_lines(performance: Performance) -> list[str]:
    """Describe reward transitions without making the reader decode status labels.

    The final two lines deliberately describe current validator state, not when an operator exited:
    the cumulative rewards tree proves that they earned nothing, while the live snapshot only proves
    whether they have active validators now.
    """
    lines = [
        f"{_plural(performance.earned, 'returning operator')} earned rewards",
        f"{_plural(performance.first_time, 'operator')} earned rewards for the first time",
    ]
    if performance.stopped is not None:
        lines += [
            f"{_plural(performance.stopped, 'operator')} earned rewards last frame, not this one",
            f"{_plural(performance.resumed, 'operator')} earned rewards this frame after missing "
            "the last",
        ]
    lines += [
        f"{_plural(performance.idle_retired, 'operator')} earned no rewards · "
        "no active validators now",
        f"{_plural(performance.idle_running, 'operator')} earned no rewards · "
        "active validators now",
    ]
    return lines


def _performance_block(report: Report) -> list[str]:
    performance = report.performance
    if performance is None:
        return []

    lines = [
        f"💰 <b>Frame {performance.frame_date}</b> "
        f"({_frame_interval_label(performance.interval_days)})",
        *_performance_lines(performance),
    ]

    frame = report.frame
    if frame is not None:
        lines.append(
            f"⚠️ Frame overdue by {frame.hours_late:.0f} hours" if frame.is_late
            else f"⏳ Next frame due {frame.deadline:%d %b} · "
            f"{frame.hours_remaining / 24:.0f} days remaining"
        )
    return lines


def _frame_block(report: Report) -> list[str]:
    """Only used before there is frame history to fold the deadline into."""
    frame = report.frame
    if frame is None or report.performance is not None:
        return []
    lines = ["💰 <b>Rewards frame</b>"]
    lines.append(
        f"⚠️ Frame overdue by {frame.hours_late:.0f} hours" if frame.is_late
        else f"⏳ Next frame due {frame.deadline:%d %b %H:%M} UTC · "
        f"{frame.hours_remaining / 24:.0f} days remaining"
    )
    return lines


def _footer(report: Report) -> list[str]:
    lines = [f"<i>{_link(ETHERSCAN_BLOCK.format(report.block), f'block {report.block:,}')}</i>"]
    if report.missing_days:
        shown = ", ".join(report.missing_days[:3])
        more = f" +{len(report.missing_days) - 3}" if len(report.missing_days) > 3 else ""
        lines.append(f"<i>⚠️ no data {shown}{more}</i>")
    for warning in report.warnings:
        lines.append(f"<i>⚠️ {html.escape(warning)}</i>")
    return lines


def render(report: Report) -> str:
    blocks: list[list[str]] = [
        [f"📊 <b>CSM · {report.day}</b>", headline(report)],
        _operators_block(report),
        _funnel_block(report),
        _capacity_block(report),
        _strikes_block(report),
        _performance_block(report),
        _pools_block(report),
        _frame_block(report),
        _footer(report),
    ]
    return "\n\n".join("\n".join(b) for b in blocks if b)


def week_key(when: datetime | None = None) -> str:
    """ISO year and week, used to make brief delivery idempotent across restarts."""
    when = when or datetime.now(timezone.utc)
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"
