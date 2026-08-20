"""Reading the state of CSM: one function per source, all pinned to a single block.

Every getter and struct layout here was verified against mainnet on 2026-08-20 before being written.
Where a return shape is asserted, the assertion is the point — a contract upgrade that changes a
signature must break loudly here rather than shift a field silently and poison a brief.

The one thing this module does *not* do is decide what anything means. It reads and decodes; `metrics.py`
interprets. That split is what makes a stored snapshot re-interpretable later.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .chain import Chain, decode_string, to_address, to_uint, words
from .config import Config
from .ipfs import Ipfs, merkle_addresses, merkle_root

# NodeOperator, as returned by CSModule.getNodeOperator(uint256). Sixteen fields; the first ten are
# counters, then four addresses, then two flags. Verified by decoding operators 0, 100 and 614.
OPERATOR_FIELDS = (
    "totalAddedKeys",
    "totalWithdrawnKeys",
    "totalDepositedKeys",
    "totalVettedKeys",
    "stuckValidatorsCount",
    "depositableValidatorsCount",
    "targetLimit",
    "targetLimitMode",
    "totalExitedKeys",
    "enqueuedCount",
)
OPERATOR_WORDS = 16

# 32 ETH — the Staking Router's maxEBType1, the unit share limits are denominated in.
MAX_EFFECTIVE_BALANCE_WEI = 32 * 10**18

# StakingModule as returned by getStakingModules(). Fifteen fields: the last two are
# withdrawalCredentialsType and validatorsBalanceGwei, which an earlier version of this decoder missed.
MODULE_FIELDS = 15

# Bond curve ids, read from the gates themselves rather than hardcoded blindly: the ICS gate reports
# curveId() == 2 and the IDVTC gate reports 3. Curve 0 is the permissionless default. `read_gates`
# re-reads these every run, so a governance change to a gate's curve is picked up rather than assumed.
DEFAULT_CURVE = 0

GATE_LABELS = {"gate_ics": "ICS", "gate_idvtc": "IDVTC"}


class SourceError(RuntimeError):
    pass


# -- shapes ----------------------------------------------------------------


@dataclass
class ModuleState:
    id: int
    name: str
    address: str
    share_limit_bp: int          # basis points, e.g. 900 == 9.00%
    priority_exit_bp: int
    module_fee_bp: int
    treasury_fee_bp: int
    status: int
    exited: int
    deposited: int
    depositable: int
    wc_type: int = 1             # 1 = 0x01 credentials, 2 = 0x02
    total_stake_wei: int = 0     # from getTotalModuleStake(); only 0x02 modules implement it
    reported_balance_gwei: int = 0   # validatorsBalanceGwei from the Staking Router's own struct

    @property
    def active(self) -> int:
        return self.deposited - self.exited

    @property
    def share_units(self) -> int:
        """The module's size in the unit the share limit is actually enforced on.

        Verified against `SRLib._getModulesAllocationAndCapacity` (lidofinance/core,
        contracts/0.8.25/sr/SRLib.sol): the target is
        `targetValidators = shareLimit * totalValidators / TOTAL_BASIS_POINTS`, where a module's
        contribution to `totalValidators` is its active validator count for 0x01 modules but
        `ceilDiv(getTotalModuleStake(), 32 ETH)` for 0x02 modules.

        For 0x01 that equals the validator count, since each holds exactly 32 ETH. For 0x02 it does
        not, because a validator can hold up to 2,048. Measured 2026-08-20 the two agree — curated-v2's
        0x02 validators are all still seeded at 32 ETH and none topped up — but they will diverge as
        top-ups proceed, and at that point counting raw validators would understate the denominator and
        overstate CSM's share.

        Note: the `validatorsBalanceGwei` field on the Staking Router's own struct reads 0 for the 0x02
        module and is not what the protocol uses. Do not substitute it.
        """
        if self.wc_type != 2:
            return self.active
        return -(-self.total_stake_wei // MAX_EFFECTIVE_BALANCE_WEI)  # ceiling division

    @property
    def nominal_stake_eth(self) -> int:
        """Staked ETH at face value: deposited validators times 32.

        This is what `getTotalModuleStake()` returns for 0x02 modules, and it is the basis the share
        limit is enforced on. Measured 2026-08-20 on CSM: 791,360 ETH, exactly 24,730 x 32.
        """
        if self.total_stake_wei:
            return self.total_stake_wei // 10**18
        return self.active * 32

    @property
    def observed_stake_eth(self) -> int | None:
        """Staked ETH as actually observed on the consensus layer, if the Router reports it."""
        if not self.reported_balance_gwei:
            return None
        return self.reported_balance_gwei * 10**9 // 10**18

    @property
    def balance_gap_validators(self) -> float | None:
        """Nominal minus observed stake, expressed in 32-ETH validator equivalents.

        Measured on CSM 2026-08-20: 45,090.82 ETH, or 1,409.09 validators — very nearly a whole
        number, so most of it is validators that are deposited but not yet contributing balance. The
        fractional 2.8 ETH remainder means a small part is balances sitting under 32 ETH.

        **Verified against a public beacon API on 2026-08-20** (Lighthouse via
        ethereum-beacon-api.publicnode.com, `/eth/v1/beacon/states/head/pending_deposits`): of 34,749
        pending deposits network-wide, 13,862 carried Lido's 0x01 withdrawal credentials against a
        combined 0x01 gap of ~14,739 validators read here. The two agree within ~6%, so this gap is
        predominantly **Ethereum's activation queue**, with the small remainder being oracle lag or
        balances under 32 ETH.

        The bot deliberately does not query a beacon node to compute this — the endpoint returns ~15MB
        and the on-chain figures answer the question. The check was done once to earn the label.
        """
        observed = self.observed_stake_eth
        if observed is None:
            return None
        return (self.nominal_stake_eth - observed) / 32


@dataclass
class OperatorState:
    id: int
    curve_id: int
    manager: str
    reward_address: str
    counters: dict[str, int]

    @property
    def active_keys(self) -> int:
        return self.counters["totalDepositedKeys"] - self.counters["totalExitedKeys"]

    @property
    def has_departed(self) -> bool:
        """Every key this operator ever funded has been withdrawn.

        Withdrawal is the terminal state: a key that has been withdrawn cannot come back. An operator
        with deposited keys, all of them withdrawn, and nothing waiting to deposit has finished. This
        is deliberately stricter than "exited" — a validator can be exited but not yet withdrawn, and
        counting those as departures would report the same operator leaving twice.
        """
        deposited = self.counters["totalDepositedKeys"]
        return (
            deposited > 0
            and self.counters["totalWithdrawnKeys"] >= deposited
            and self.counters["depositableValidatorsCount"] == 0
        )


@dataclass
class GateState:
    label: str
    address: str
    curve_id: int
    tree_root: str
    tree_cid: str
    eligible: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    tree_source: str = ""
    root_verified: bool = False

    @property
    def unclaimed(self) -> list[str]:
        claimed = set(self.claimed)
        return [a for a in self.eligible if a not in claimed]


@dataclass
class FrameState:
    slots_per_epoch: int
    seconds_per_slot: int
    genesis_time: int
    initial_epoch: int
    epochs_per_frame: int
    ref_slot: int
    deadline_slot: int

    def slot_time(self, slot: int) -> datetime:
        return datetime.fromtimestamp(
            self.genesis_time + slot * self.seconds_per_slot, timezone.utc
        )

    @property
    def frame_days(self) -> float:
        return self.epochs_per_frame * self.slots_per_epoch * self.seconds_per_slot / 86400

    @property
    def ref_time(self) -> datetime:
        return self.slot_time(self.ref_slot)

    @property
    def deadline(self) -> datetime:
        return self.slot_time(self.deadline_slot)

    def is_late(self, now: datetime) -> bool:
        return now > self.deadline


@dataclass
class Snapshot:
    block: int
    timestamp: int
    day: str
    modules: list[ModuleState] = field(default_factory=list)
    operators: list[OperatorState] = field(default_factory=list)
    gates: list[GateState] = field(default_factory=list)
    frame: FrameState | None = None
    strikes: "StrikesState | None" = None
    pools: list = field(default_factory=list)   # other permissionless pools, for context
    rewards_tree_etag: str = ""
    warnings: list[str] = field(default_factory=list)

    def module(self, module_id: int) -> ModuleState:
        for module in self.modules:
            if module.id == module_id:
                return module
        raise SourceError(f"module {module_id} not present in snapshot")

    @property
    def total_active(self) -> int:
        return sum(m.active for m in self.modules)

    @property
    def total_share_units(self) -> int:
        """The denominator the share limit is enforced against. See `ModuleState.share_units`."""
        return sum(m.share_units for m in self.modules)


# -- preflight -------------------------------------------------------------


def preflight(chain: Chain, config: Config, block: int) -> list[str]:
    """Confirm the pinned addresses still are what we think they are.

    Specifically: that `config.module_id` really resolves to the configured CSM address through the
    Staking Router. Lido's contracts are proxies, so an address is stable while its behaviour is not,
    and module ids have been misreported in documentation before. Cheap to check, expensive to get
    wrong.
    """
    warnings: list[str] = []
    modules = read_modules(chain, config, block, with_summaries=False)

    for module in modules:
        if module.id != config.module_id:
            continue
        if module.address.lower() != config.address("csm").lower():
            raise SourceError(
                f"module {config.module_id} is {module.address}, expected "
                f"{config.address('csm')} — refusing to report on the wrong contract"
            )
        return warnings

    raise SourceError(f"staking module {config.module_id} not found in the Staking Router")


# -- modules ---------------------------------------------------------------


def read_modules(
    chain: Chain, config: Config, block: int, with_summaries: bool = True
) -> list[ModuleState]:
    """Decode StakingRouter.getStakingModules() and, optionally, each module's validator summary."""
    call = chain.call(config.address("staking_router"), "getStakingModules()", block=block)
    if not call.ok:
        raise SourceError(f"getStakingModules failed: {call.error}")

    parts = words(call.response)

    def word(index: int) -> str:
        return parts[index]

    array_offset = to_uint(word(0)) // 32
    count = to_uint(word(array_offset))
    base = array_offset + 1

    modules: list[ModuleState] = []
    for index in range(count):
        head = base + to_uint(word(base + index)) // 32
        fields = [to_uint(word(head + i)) for i in range(MODULE_FIELDS)]

        name_at = head + fields[6] // 32
        name_len = to_uint(word(name_at))
        raw_name = "".join(parts[name_at + 1:])
        name = bytes.fromhex(raw_name[:name_len * 2]).decode("utf-8", errors="replace")

        modules.append(ModuleState(
            id=fields[0],
            name=name,
            address=to_address(word(head + 1)),
            module_fee_bp=fields[2],
            treasury_fee_bp=fields[3],
            share_limit_bp=fields[4],
            status=fields[5],
            priority_exit_bp=fields[10],
            wc_type=fields[13],
            reported_balance_gwei=fields[14],
            exited=0, deposited=0, depositable=0,
        ))

    if with_summaries:
        requests = [
            (config.address("staking_router"), "getStakingModuleSummary(uint256)", f"{m.id:064x}")
            for m in modules
        ]
        for module, result in zip(modules, chain.batch_call(requests, block)):
            if not result.ok:
                raise SourceError(f"module {module.id} summary failed: {result.error}")
            summary = words(result.response)
            if len(summary) < 3:
                raise SourceError(
                    f"module {module.id} summary returned {len(summary)} words, expected 3"
                )
            module.exited = to_uint(summary[0])
            module.deposited = to_uint(summary[1])
            module.depositable = to_uint(summary[2])

        # Only 0x02 modules implement IStakingModuleV2; 0x01 modules revert on this, which is
        # expected and not an error.
        for module in modules:
            if module.wc_type != 2:
                continue
            call = chain.call(module.address, "getTotalModuleStake()", block=block)
            if call.ok and call.response and len(call.response) > 2:
                module.total_stake_wei = to_uint(words(call.response)[0])

    return modules


# -- operators -------------------------------------------------------------


def read_operators(chain: Chain, config: Config, block: int) -> list[OperatorState]:
    """Read every node operator and its bond curve.

    Two batched calls per operator. At ~615 operators that is ~1,230 eth_calls, which JSON-RPC
    batching turns into around twenty requests — a few seconds. Cheap enough to do daily, which is
    why nothing here depends on an indexer or a helper contract being deployed.
    """
    count_call = chain.call(config.address("csm"), "getNodeOperatorsCount()", block=block)
    if not count_call.ok:
        raise SourceError(f"getNodeOperatorsCount failed: {count_call.error}")
    count = to_uint(words(count_call.response)[0])

    csm = config.address("csm")
    accounting = config.address("cs_accounting")

    operator_results = chain.batch_call(
        [(csm, "getNodeOperator(uint256)", f"{i:064x}") for i in range(count)], block
    )
    curve_results = chain.batch_call(
        [(accounting, "getBondCurveId(uint256)", f"{i:064x}") for i in range(count)], block
    )

    operators: list[OperatorState] = []
    for index, (op_call, curve_call) in enumerate(zip(operator_results, curve_results)):
        if not op_call.ok:
            raise SourceError(f"getNodeOperator({index}) failed: {op_call.error}")
        fields = words(op_call.response)
        if len(fields) != OPERATOR_WORDS:
            raise SourceError(
                f"getNodeOperator({index}) returned {len(fields)} words, expected {OPERATOR_WORDS} "
                f"— the NodeOperator struct may have changed"
            )
        if not curve_call.ok:
            raise SourceError(f"getBondCurveId({index}) failed: {curve_call.error}")

        operators.append(OperatorState(
            id=index,
            curve_id=to_uint(words(curve_call.response)[0]),
            manager=to_address(fields[10]),
            reward_address=to_address(fields[12]),
            counters={name: to_uint(fields[i]) for i, name in enumerate(OPERATOR_FIELDS)},
        ))

    return operators


# -- gates and the claim funnel --------------------------------------------


def read_gates(
    chain: Chain, config: Config, block: int, ipfs: Ipfs, warnings: list[str]
) -> list[GateState]:
    """Read each vetted gate, its eligibility tree, and who has claimed.

    Claim status comes from `isConsumed(address)` — contract state, not event history. Public RPCs cap
    `eth_getLogs` at 800 blocks, so scanning `Consumed` events back to deployment would cost thousands
    of requests. State reads are cheaper *and* more trustworthy: one batched pass gives the exact
    current answer with no range arithmetic to get wrong.
    """
    gates: list[GateState] = []

    for label_key, label in GATE_LABELS.items():
        address = config.address(label_key)

        root_call = chain.call(address, "treeRoot()", block=block)
        cid_call = chain.call(address, "treeCid()", block=block)
        curve_call = chain.call(address, "curveId()", block=block)

        for name, call in (("treeRoot", root_call), ("treeCid", cid_call), ("curveId", curve_call)):
            if not call.ok:
                raise SourceError(f"{label} gate {name}() failed: {call.error}")

        gate = GateState(
            label=label,
            address=address,
            curve_id=to_uint(words(curve_call.response)[0]),
            tree_root="0x" + words(root_call.response)[0],
            tree_cid=decode_string(cid_call.response),
        )

        try:
            fetched = ipfs.fetch(gate.tree_cid)
        except Exception as exc:
            # A gate whose tree cannot be fetched leaves the funnel unknown for that cohort. Record
            # it and carry on; the brief will say so rather than imply a zero.
            warnings.append(f"{label} eligibility tree unavailable ({gate.tree_cid}): {exc}")
            gates.append(gate)
            continue

        tree = fetched.json()
        gate.tree_source = fetched.source
        gate.eligible = merkle_addresses(tree)

        published_root = merkle_root(tree)
        gate.root_verified = published_root == gate.tree_root.lower()
        if not gate.root_verified:
            # The tree behind the CID does not hash to the root the contract enforces. Do not use it:
            # the eligibility list would not be the one the protocol actually honours.
            warnings.append(
                f"{label} tree root mismatch — contract says {gate.tree_root}, "
                f"tree hashes to {published_root}; eligibility not counted"
            )
            gate.eligible = []
            gates.append(gate)
            continue

        results = chain.batch_call(
            [(address, "isConsumed(address)", a[2:].rjust(64, "0")) for a in gate.eligible], block
        )
        for candidate, result in zip(gate.eligible, results):
            if not result.ok:
                raise SourceError(f"{label} isConsumed({candidate}) failed: {result.error}")
            if to_uint(words(result.response)[0]) == 1:
                gate.claimed.append(candidate)

        gates.append(gate)

    return gates


# -- frames and strikes ----------------------------------------------------


def read_frame(chain: Chain, config: Config, block: int) -> FrameState:
    """Read the oracle's frame schedule, which makes the report deadline exactly computable.

    Verified 2026-08-20: epochsPerFrame 6300 == 28.0 days exactly. That precision is what lets the
    bot say "this frame is late" instead of guessing from the age of the last rewards commit.
    """
    consensus = config.address("hash_consensus")
    calls = chain.batch_call(
        [
            (consensus, "getChainConfig()", ""),
            (consensus, "getFrameConfig()", ""),
            (consensus, "getCurrentFrame()", ""),
        ],
        block,
    )
    for name, call in zip(("getChainConfig", "getFrameConfig", "getCurrentFrame"), calls):
        if not call.ok:
            raise SourceError(f"{name} failed: {call.error}")

    chain_config = [to_uint(w) for w in words(calls[0].response)[:3]]
    frame_config = [to_uint(w) for w in words(calls[1].response)[:3]]
    current = [to_uint(w) for w in words(calls[2].response)[:2]]

    if chain_config[1] == 0 or chain_config[0] == 0:
        raise SourceError(f"implausible chain config: {chain_config}")

    return FrameState(
        slots_per_epoch=chain_config[0],
        seconds_per_slot=chain_config[1],
        genesis_time=chain_config[2],
        initial_epoch=frame_config[0],
        epochs_per_frame=frame_config[1],
        ref_slot=current[0],
        deadline_slot=current[1],
    )


@dataclass
class StruckKey:
    operator_id: int
    pubkey: str
    strikes: int          # total over the lifetime window
    window: list[int]


@dataclass
class StrikesState:
    root: str = ""
    cid: str = ""
    keys: list[StruckKey] = field(default_factory=list)
    # curve id -> (lifetime frames, ejection threshold). Read on-chain, never assumed: the threshold
    # is per curve and they differ — measured 2026-08-20, ICS is 4 while every other curve is 3.
    params: dict[int, tuple[int, int]] = field(default_factory=dict)
    available: bool = False


def read_strikes(
    chain: Chain, config: Config, block: int, ipfs: Ipfs, curve_ids: set[int],
    warnings: list[str],
) -> StrikesState:
    """Strike counts per validator key, plus each curve's ejection threshold.

    Strikes are published off-chain per frame by the oracle as a merkle tree, addressed by a CID on
    `ValidatorStrikes`. Leaves are `[nodeOperatorId, pubkey, uint256[]]`, where the array is a rolling
    window with one slot per frame in the lifetime.

    **Only the sum of the window is used, never the slot order.** Ejection depends on the total within
    the window, and the mapping from slot index to frame is not documented anywhere we could verify —
    inferring it from the data would be a guess dressed up as a fact.
    """
    address = config.address("validator_strikes")
    state = StrikesState()

    root_call = chain.call(address, "treeRoot()", block=block)
    cid_call = chain.call(address, "treeCid()", block=block)
    if not root_call.ok:
        raise SourceError(f"strikes treeRoot() failed: {root_call.error}")
    if not cid_call.ok:
        raise SourceError(f"strikes treeCid() failed: {cid_call.error}")

    state.root = "0x" + words(root_call.response)[0]
    state.cid = decode_string(cid_call.response)

    registry = config.address("parameters_registry")
    param_calls = chain.batch_call(
        [(registry, "getStrikesParams(uint256)", f"{c:064x}") for c in sorted(curve_ids)], block
    )
    for curve_id, call in zip(sorted(curve_ids), param_calls):
        if not call.ok:
            warnings.append(f"strike params for curve {curve_id} unavailable: {call.error}")
            continue
        values = words(call.response)
        if len(values) < 2:
            warnings.append(f"strike params for curve {curve_id} returned {len(values)} words")
            continue
        state.params[curve_id] = (to_uint(values[0]), to_uint(values[1]))

    try:
        tree = ipfs.fetch(state.cid).json()
    except Exception as exc:
        warnings.append(f"strikes tree unavailable ({state.cid}): {exc}")
        return state

    if tree.get("leafEncoding") != ["uint256", "bytes", "uint256[]"]:
        warnings.append(f"unexpected strikes leaf encoding {tree.get('leafEncoding')!r}")
        return state

    for entry in tree.get("values", []):
        value = entry.get("value")
        if not isinstance(value, list) or len(value) != 3:
            warnings.append(f"unexpected strikes leaf {value!r}")
            return state
        window = [int(x) for x in value[2]]
        state.keys.append(StruckKey(
            operator_id=int(value[0]),
            pubkey=value[1],
            strikes=sum(window),
            window=window,
        ))

    state.available = True
    return state


# -- rewards tree ----------------------------------------------------------


def rewards_tree_state(config: Config) -> tuple[str, str]:
    """Cheaply detect whether the published rewards tree has changed.

    A HEAD request for the ETag avoids pulling 140KB daily just to notice nothing moved. The tree is
    republished once per frame, so on all but one day in twenty-eight the answer is "unchanged".
    """
    request = urllib.request.Request(
        config.rewards_tree_url, method="HEAD", headers={"User-Agent": "csm-bot/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.headers.get("ETag", ""), response.headers.get("Last-Modified", "")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SourceError(f"rewards tree HEAD failed: {exc}") from exc


def fetch_rewards_tree(config: Config) -> dict:
    request = urllib.request.Request(
        config.rewards_tree_url, headers={"User-Agent": "csm-bot/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(f"rewards tree fetch failed: {exc}") from exc


# -- the whole snapshot ----------------------------------------------------


def collect(chain: Chain, config: Config, ipfs: Ipfs) -> Snapshot:
    """Take one complete, internally consistent reading of CSM.

    Everything is read at a single pinned block. A run that spanned blocks could report a validator
    count from one moment against a share limit from another, and the two would not add up — which is
    exactly the kind of quiet inconsistency that destroys trust in a number.
    """
    block = chain.block_number()
    timestamp = chain.block_timestamp(block)
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()

    warnings: list[str] = []
    warnings.extend(preflight(chain, config, block))

    snapshot = Snapshot(block=block, timestamp=timestamp, day=day, warnings=warnings)
    snapshot.modules = read_modules(chain, config, block)
    snapshot.operators = read_operators(chain, config, block)
    snapshot.gates = read_gates(chain, config, block, ipfs, warnings)
    snapshot.frame = read_frame(chain, config, block)
    snapshot.strikes = read_strikes(
        chain, config, block, ipfs,
        {o.curve_id for o in snapshot.operators}, warnings,
    )

    from . import competitors
    snapshot.pools = competitors.read_all(chain, block, warnings)

    try:
        etag, _ = rewards_tree_state(config)
        snapshot.rewards_tree_etag = etag
    except SourceError as exc:
        warnings.append(str(exc))

    return snapshot
