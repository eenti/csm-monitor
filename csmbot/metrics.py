"""Turning a snapshot into the numbers a brief reports.

Nothing here touches the network. Metrics are derived from a `Snapshot` plus whatever history the
store holds, which is what makes them recomputable: fix a calculation, replay the stored snapshots,
get corrected history rather than a fresh start.

Where a metric needs history the bot does not have yet, it returns `None` rather than a plausible
default. A runway of "unknown" is honest on day three; a runway of "999 days" because the rate was
silently treated as zero is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .sources import OperatorState, Snapshot

# Bond curve ids, verified on mainnet 2026-08-20 by reading curveId() from each gate.
CURVE_NAMES = {
    0: "Permissionless",
    1: "Legacy early adoption",
    2: "ICS",
    3: "IDVTC",
}


def curve_name(curve_id: int) -> str:
    return CURVE_NAMES.get(curve_id, f"curve {curve_id}")


# -- capacity --------------------------------------------------------------


@dataclass
class Capacity:
    """Capacity measured in **validators**, which is not the same as measured in stake.

    `stakeShareLimit` and every figure the Staking Router exposes are validator counts:
    `StakingModuleSummary` carries `totalExitedValidators`, `totalDepositedValidators` and
    `depositableValidatorsCount`, and nothing denominated in ETH.

    That distinction matters now that Lido runs both credential types. A 0x01 validator holds exactly
    32 ETH, while a 0x02 validator holds up to 2,048. CSM is 0x01-only, so CSM's own stake is exactly
    `active x 32` — but the *denominator* includes modules running 0x02 validators, whose balances
    exceed 32 ETH each. A validator-count ratio therefore **overstates CSM's share of stake**, and the
    gap widens as 0x02 adoption grows.

    So this is reported as what it is: a share of validator count. It is the right number for tracking
    the limit, and the wrong number for describing how much stake CSM operates.
    """

    validator_share_pct: float
    limit_pct: float
    active: int
    total_active: int
    headroom: int                  # validators that fit before the limit
    depositable: int
    rate_7d: float | None          # validators added per day, mean over the window
    runway_days: float | None
    constraint: str                # "supply" | "capacity" | "unknown"
    window_days: int = 0
    stake_eth: int = 0
    not_yet_contributing: float | None = None
    # Movement against the comparison snapshot. None until there is one to compare against.
    share_change: float | None = None
    headroom_change: int | None = None
    active_change: int | None = None

    @property
    def csm_stake_eth(self) -> int:
        """CSM's staked ETH, from the module's own reported total where available."""
        return self.stake_eth or self.active * 32

    @property
    def headroom_eth(self) -> int:
        return self.headroom * 32


def capacity(
    snapshot: Snapshot,
    module_id: int,
    history: list[tuple[str, float]],
    previous: dict | None = None,
) -> Capacity:
    """Share, headroom, growth rate and which side is actually binding.

    `history` is [(day, active_validators)] oldest first, from the store.
    `previous` is the same three figures from the comparison snapshot, so the brief can show where
    each one moved rather than only where it stands.

    The constraint is the judgement call worth being careful about. CSM is **capacity**-constrained
    when keys are waiting but cannot be deposited, and **supply**-constrained when there is room under
    the limit and nothing queued. The distinction decides what to do about it: capacity means a share
    limit motion, supply means operator acquisition. Getting it backwards wastes a quarter.
    """
    module = snapshot.module(module_id)
    # The unit the limit is actually enforced on: validator counts for 0x01 modules, stake/32 for 0x02.
    # See ModuleState.share_units for the source reference.
    total = snapshot.total_share_units
    if total <= 0:
        raise ValueError("no share units across modules — snapshot is unusable")

    share = module.share_units / total * 100
    limit = module.share_limit_bp / 100
    headroom = int(total * module.share_limit_bp / 10_000) - module.share_units

    rate: float | None = None
    window = 0
    if len(history) >= 2:
        first_day, first_value = history[0]
        last_day, last_value = history[-1]
        span = (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days
        if span > 0:
            rate = (last_value - first_value) / span
            window = span

    runway: float | None = None
    if rate is not None and rate > 0:
        runway = max(headroom, 0) / rate

    # A module at or over its limit with keys waiting is unambiguously capacity-bound. Room plus an
    # empty queue is unambiguously supply-bound. Anything else we decline to call.
    if headroom <= 0 and module.depositable > 0:
        constraint = "capacity"
    elif headroom > 0 and module.depositable == 0:
        constraint = "supply"
    elif headroom > 0 and module.depositable > 0:
        # Room and keys both present: the protocol simply has not deposited them yet, which is normal
        # between deposit cycles and not a constraint on CSM at all.
        constraint = "supply" if module.depositable < headroom else "capacity"
    else:
        constraint = "unknown"

    share_change = headroom_change = active_change = None
    if previous:
        if previous.get("share_pct") is not None:
            share_change = share - previous["share_pct"]
        if previous.get("headroom") is not None:
            headroom_change = headroom - int(previous["headroom"])
        if previous.get("active") is not None:
            active_change = module.active - int(previous["active"])

    return Capacity(
        validator_share_pct=share,
        limit_pct=limit,
        active=module.active,
        total_active=total,
        headroom=headroom,
        depositable=module.depositable,
        rate_7d=rate,
        runway_days=runway,
        constraint=constraint,
        window_days=window,
        stake_eth=module.nominal_stake_eth,
        not_yet_contributing=module.balance_gap_validators,
        share_change=share_change,
        headroom_change=headroom_change,
        active_change=active_change,
    )


# -- operators -------------------------------------------------------------


@dataclass
class CohortStats:
    curve_id: int
    name: str
    total: int
    active: int
    departed: int
    never_funded: int
    active_keys: int


@dataclass
class CurveChange:
    operator_id: int
    from_curve: int
    to_curve: int


@dataclass
class Operators:
    total: int
    active: int
    departed: int
    never_funded: int
    active_keys: int
    cohorts: list[CohortStats] = field(default_factory=list)
    # Week-over-week movement. None until there is a prior snapshot to compare against.
    new_ids: list[int] | None = None
    departed_ids: list[int] | None = None
    new_by_curve: dict[int, int] = field(default_factory=dict)
    departed_by_curve: dict[int, int] = field(default_factory=dict)
    # Operators that moved between cohorts without joining or leaving CSM.
    curve_changes: list[CurveChange] = field(default_factory=list)

    @property
    def total_change(self) -> int | None:
        """Change in registered operators over the comparison window.

        Equal to the number of joins: `getNodeOperatorsCount()` only ever increases — an operator
        that withdraws every key stays registered — so there is nothing to subtract.
        """
        return None if self.new_ids is None else len(self.new_ids)


def operators(
    snapshot: Snapshot,
    previous: dict[int, dict] | None = None,
) -> Operators:
    """Cohort counts, and week-over-week joins, departures and cohort moves.

    `previous` maps operator id -> `{"curve_id": int, **counters}` from the comparison snapshot.

    **Cohort membership is mutable, and that changes how these numbers must be read.**
    `VettedGate.claimBondCurve(nodeOperatorId, proof)` lets an operator that already exists claim a
    gate's bond curve, so a legacy-early-adoption operator can become ICS, and an ICS operator can
    become IDVTC, without ever leaving CSM. Consequences:

    - A cohort's current membership is the set that has *not* moved on. Dividing its departures by its
      current size therefore measures a selected population, not a churn rate, so no such ratio is
      computed here. Counts only.
    - Movement between cohorts is reported separately as `curve_changes`, because from a single
      snapshot it is indistinguishable from a departure plus an arrival.

    "Departed" means every key the operator ever funded has been withdrawn. Withdrawal is terminal, so
    an operator exiting validators over several weeks is not counted twice.
    """
    ops = snapshot.operators
    result = Operators(
        total=len(ops),
        active=sum(1 for o in ops if o.active_keys > 0),
        departed=sum(1 for o in ops if o.has_departed),
        never_funded=sum(1 for o in ops if o.counters["totalDepositedKeys"] == 0),
        active_keys=sum(o.active_keys for o in ops),
    )

    by_curve: dict[int, list[OperatorState]] = {}
    for operator in ops:
        by_curve.setdefault(operator.curve_id, []).append(operator)

    for curve_id in sorted(by_curve):
        members = by_curve[curve_id]
        result.cohorts.append(CohortStats(
            curve_id=curve_id,
            name=curve_name(curve_id),
            total=len(members),
            active=sum(1 for o in members if o.active_keys > 0),
            departed=sum(1 for o in members if o.has_departed),
            never_funded=sum(1 for o in members if o.counters["totalDepositedKeys"] == 0),
            active_keys=sum(o.active_keys for o in members),
        ))

    if previous is not None:
        seen = set(previous)
        result.new_ids = [o.id for o in ops if o.id not in seen]

        was_departed = {
            oid
            for oid, record in previous.items()
            if record.get("totalDepositedKeys", 0) > 0
            and record.get("totalWithdrawnKeys", 0) >= record.get("totalDepositedKeys", 0)
        }
        result.departed_ids = [o.id for o in ops if o.has_departed and o.id not in was_departed]

        for operator in ops:
            before = previous.get(operator.id)
            if before is None:
                continue
            old_curve = before.get("curve_id")
            if old_curve is not None and old_curve != operator.curve_id:
                result.curve_changes.append(
                    CurveChange(operator.id, old_curve, operator.curve_id)
                )

        index = {o.id: o for o in ops}
        for oid in result.new_ids:
            curve = index[oid].curve_id
            result.new_by_curve[curve] = result.new_by_curve.get(curve, 0) + 1
        for oid in result.departed_ids:
            curve = index[oid].curve_id
            result.departed_by_curve[curve] = result.departed_by_curve.get(curve, 0) + 1

    return result


# -- claim funnel ----------------------------------------------------------


@dataclass
class GateFunnel:
    label: str
    curve_id: int
    eligible: int
    claimed: int
    unclaimed: int
    claim_rate: float | None
    operators_on_curve: int
    tree_root: str
    tree_cid: str
    available: bool
    integrity_ok: bool
    # The actual addresses, not just counts. The unclaimed-batch alert has to intersect a stored
    # batch against who has claimed, which a count cannot answer.
    claimed_addresses: list[str] = field(default_factory=list)
    unclaimed_addresses: list[str] = field(default_factory=list)
    # Claims on this gate with no operator on its curve to match them. A count, not a cause — see the
    # note in `funnel`. Reported, not alarmed.
    off_curve: int = 0
    # Movement against the comparison snapshot. None until there is one to compare against.
    new_claims: list[str] = field(default_factory=list)
    claimed_change: int | None = None
    eligible_change: int | None = None


@dataclass
class Funnel:
    gates: list[GateFunnel] = field(default_factory=list)


def funnel(snapshot: Snapshot, previous: dict | None = None) -> Funnel:
    """Eligibility versus claims per gate, movement over the window, and an integrity check.

    **The integrity check only fails in the direction the gate's claims cannot explain.** Every way
    of consuming a gate ends in `ACCOUNTING.setBondCurve(nodeOperatorId, curveId)` — checked in
    `VettedGate.sol` (lidofinance/staking-modules): all three `addNodeOperator*` paths and
    `claimBondCurve`. So each claim put one operator on the gate's curve when it happened, and more
    operators on the curve than claims means a misread, or a curve set some other way. Either way the
    funnel cannot be reconciled and must not be trusted.

    More claims than operators is normal. `claimBondCurve` lets an existing operator move to a later
    curve, which leaves its original gate consumed while the operator sits somewhere else. Measured on
    mainnet 2026-08-26: operator 327 consumed the ICS gate months ago and then claimed IDVTC, leaving
    ICS at 291 claims against 290 operators on curve 2. An equality check called that a data-integrity
    failure and put "funnel numbers unverified" at the top of every brief from then on — a warning
    about two numbers that were both correct.

    The excess is reported as `off_curve`, a count with no cause attached. A cohort move is the usual
    cause, not the only possible one: `claimBondCurve` does not check whether the operator is already
    on the curve, so an operator whose owner address changed could consume the same gate twice.

    `previous` maps gate label -> {"claimed": [addresses], "eligible": int} from the comparison
    snapshot, and produces the week's movement. Claim sets are differenced rather than counts
    subtracted, so a gate that ever un-consumed an address would show up as such instead of quietly
    netting against a new claim.
    """
    result = Funnel()
    curve_counts: dict[int, int] = {}
    for operator in snapshot.operators:
        curve_counts[operator.curve_id] = curve_counts.get(operator.curve_id, 0) + 1

    for gate in snapshot.gates:
        available = bool(gate.eligible) and gate.root_verified
        on_curve = curve_counts.get(gate.curve_id, 0)

        before = (previous or {}).get(gate.label)
        new_claims: list[str] = []
        claimed_change = eligible_change = None
        if before is not None:
            was_claimed = {a.lower() for a in before.get("claimed", ())}
            new_claims = [a for a in gate.claimed if a.lower() not in was_claimed]
            claimed_change = len(gate.claimed) - len(was_claimed)
            if before.get("eligible") is not None:
                eligible_change = len(gate.eligible) - int(before["eligible"])

        result.gates.append(GateFunnel(
            label=gate.label,
            curve_id=gate.curve_id,
            eligible=len(gate.eligible),
            claimed=len(gate.claimed),
            unclaimed=len(gate.unclaimed),
            claim_rate=(len(gate.claimed) / len(gate.eligible) * 100) if gate.eligible else None,
            operators_on_curve=on_curve,
            tree_root=gate.tree_root,
            tree_cid=gate.tree_cid,
            available=available,
            integrity_ok=(not available) or on_curve <= len(gate.claimed),
            claimed_addresses=list(gate.claimed),
            unclaimed_addresses=list(gate.unclaimed),
            off_curve=max(len(gate.claimed) - on_curve, 0),
            new_claims=new_claims,
            claimed_change=claimed_change,
            eligible_change=eligible_change,
        ))

    return result


# -- frame -----------------------------------------------------------------


@dataclass
class Frame:
    ref_time: datetime
    deadline: datetime
    frame_days: float
    hours_remaining: float
    is_late: bool
    hours_late: float


def frame(snapshot: Snapshot, now: datetime | None = None) -> Frame | None:
    if snapshot.frame is None:
        return None
    now = now or datetime.now(timezone.utc)
    state = snapshot.frame
    delta = (state.deadline - now).total_seconds() / 3600
    return Frame(
        ref_time=state.ref_time,
        deadline=state.deadline,
        frame_days=state.frame_days,
        hours_remaining=max(delta, 0.0),
        is_late=delta < 0,
        hours_late=max(-delta, 0.0),
    )


# -- strikes ---------------------------------------------------------------


@dataclass
class StrikeRisk:
    operator_id: int
    curve_id: int
    reward_address: str
    struck_keys: int
    worst_key: int        # highest strike count on any one of its keys
    threshold: int        # its curve's ejection threshold
    active_keys: int


@dataclass
class Strikes:
    available: bool
    struck_keys: int
    struck_operators: int
    ejectable: list[StrikeRisk] = field(default_factory=list)   # worst key at or over threshold
    at_risk: list[StrikeRisk] = field(default_factory=list)     # worst key one below threshold
    thresholds: dict[int, int] = field(default_factory=dict)
    # Operators that reached the threshold and have since withdrawn everything. Kept out of the lists
    # above but counted, so the number is not silently lost.
    departed_with_strikes: int = 0
    # Movement against the comparison snapshot. None until there is one to compare against.
    keys_change: int | None = None
    operators_change: int | None = None


def strikes(snapshot, previous: dict | None = None) -> Strikes:
    """Which operators are ejectable, and which are one strike away.

    **The ejection threshold is per bond curve, not global.** Read on-chain 2026-08-20: curves 0, 1
    and 3 eject at 3 strikes within a 6-frame window, while ICS (curve 2) allows 4. Applying one
    number to everyone would put ICS operators on a warning list they are not on.

    Strikes are recorded per validator key, so an operator's exposure is its *worst* key — one key at
    threshold is enough to make that key ejectable.

    **Operators that have already withdrawn every key are excluded.** Strikes persist in the tree after
    an operator leaves, so without this filter the ejection list names people who are already gone —
    nothing to eject, nobody worth contacting. They are counted in `departed_with_strikes` instead.
    """
    state = snapshot.strikes
    if state is None or not state.available:
        return Strikes(available=False, struck_keys=0, struck_operators=0,
                       thresholds=dict(state.params) if state else {})

    by_operator: dict[int, list[int]] = {}
    for key in state.keys:
        by_operator.setdefault(key.operator_id, []).append(key.strikes)

    operators_by_id = {o.id: o for o in snapshot.operators}
    result = Strikes(
        available=True,
        struck_keys=len(state.keys),
        struck_operators=len(by_operator),
        thresholds=dict(state.params),
    )

    for operator_id, counts in by_operator.items():
        operator = operators_by_id.get(operator_id)
        if operator is None:
            continue
        lifetime_threshold = state.params.get(operator.curve_id)
        if lifetime_threshold is None:
            continue
        _, threshold = lifetime_threshold
        worst = max(counts)
        risk = StrikeRisk(
            operator_id=operator_id,
            curve_id=operator.curve_id,
            reward_address=operator.reward_address,
            struck_keys=len(counts),
            worst_key=worst,
            threshold=threshold,
            active_keys=operator.active_keys,
        )
        if operator.active_keys == 0:
            if worst >= threshold - 1:
                result.departed_with_strikes += 1
            continue
        if worst >= threshold:
            result.ejectable.append(risk)
        elif worst == threshold - 1:
            result.at_risk.append(risk)

    if previous:
        if previous.get("keys") is not None:
            result.keys_change = result.struck_keys - int(previous["keys"])
        if previous.get("operators") is not None:
            result.operators_change = result.struck_operators - int(previous["operators"])

    result.ejectable.sort(key=lambda r: (-r.worst_key, -r.struck_keys))
    result.at_risk.sort(key=lambda r: -r.struck_keys)
    return result


# -- performance -----------------------------------------------------------


@dataclass
class Performance:
    """Reward accrual for the most recently published frame.

    Built from the backfilled frame series, not from a live read: the oracle publishes once per 28
    days, so this changes on one day in twenty-eight and is identical in between.

    `earned_nothing` is reported with its explanation attached because the number is large and looks
    alarming on its own. The rewards tree never removes an operator, so everyone who has withdrawn
    their last validator is counted in it every frame thereafter. The figure worth watching is
    `stopped_earning` — operators who earned in the previous frame and not in this one.
    """

    frame_date: str
    previous_frame_date: str
    interval_days: int
    operators_in_tree: int
    earned: int
    earned_nothing: int
    first_time: int
    stopped: int | None
    resumed: int | None
    # The two causes of earning nothing, separated by cross-referencing live key counts.
    idle_retired: int          # earned nothing and has no active validators
    idle_running: int          # earned nothing while still running validators
    idle_running_ids: list[int] = field(default_factory=list)


def performance(frame_deltas, snapshot) -> Performance | None:
    """Take the newest frame delta and resolve *why* each idle operator earned nothing.

    An operator earns nothing in a frame either because it had no active validators or because it ran
    validators and still earned nothing — which for a frame that has been distributed means it fell
    below the performance threshold. Those are completely different facts, and the raw count conflates
    them. Live key counts separate them.

    This works for the latest frame only: the split needs current key counts, and historical ones are
    not recoverable without an archive node.
    """
    if not frame_deltas:
        return None
    latest = frame_deltas[-1]

    active_keys = {o.id: o.active_keys for o in snapshot.operators}
    retired = running = 0
    running_ids: list[int] = []
    for operator_id in latest.earned_nothing_ids:
        if active_keys.get(operator_id, 0) > 0:
            running += 1
            running_ids.append(operator_id)
        else:
            retired += 1

    return Performance(
        frame_date=latest.date,
        previous_frame_date=latest.previous_date,
        interval_days=latest.interval_days,
        operators_in_tree=latest.operators_in_tree,
        earned=latest.earned,
        earned_nothing=latest.earned_nothing,
        first_time=latest.first_appearance,
        stopped=latest.stopped_earning,
        resumed=latest.resumed_earning,
        idle_retired=retired,
        idle_running=running,
        idle_running_ids=running_ids,
    )


# -- other pools -----------------------------------------------------------


def pools(snapshot, previous: dict | None):
    """Week-over-week movement for other permissionless pools. Counts only, no ranking."""
    from . import competitors
    return competitors.deltas(getattr(snapshot, "pools", []), previous)


# -- bundle ----------------------------------------------------------------


@dataclass
class Report:
    day: str
    block: int
    capacity: Capacity
    operators: Operators
    funnel: Funnel
    frame: Frame | None
    performance: Performance | None = None
    strikes: Strikes | None = None
    pools: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_days: list[str] = field(default_factory=list)
    comparison_day: str | None = None
    # How many days the comparison actually spans. Stated rather than assumed: the brief is weekly,
    # but a run missed on the target day makes the real window six or eight, and a delta labelled
    # "this week" that covers three days is worse than no delta at all.
    comparison_days: int | None = None


def build(
    snapshot: Snapshot,
    module_id: int,
    active_history: list[tuple[str, float]],
    previous_operators: dict[int, dict] | None = None,
    comparison_day: str | None = None,
    missing_days: list[str] | None = None,
    now: datetime | None = None,
    frame_deltas=None,
    previous_pools: dict | None = None,
    previous_gates: dict | None = None,
    previous_capacity: dict | None = None,
    previous_strikes: dict | None = None,
) -> Report:
    ops = operators(snapshot, previous_operators)
    span = None
    if comparison_day:
        span = (date.fromisoformat(snapshot.day) - date.fromisoformat(comparison_day)).days
    return Report(
        day=snapshot.day,
        block=snapshot.block,
        capacity=capacity(snapshot, module_id, active_history, previous_capacity),
        operators=ops,
        funnel=funnel(snapshot, previous_gates),
        frame=frame(snapshot, now),
        performance=performance(frame_deltas or [], snapshot),
        strikes=strikes(snapshot, previous_strikes),
        pools=pools(snapshot, previous_pools),
        warnings=list(snapshot.warnings),
        missing_days=list(missing_days or []),
        comparison_day=comparison_day,
        comparison_days=span,
    )
