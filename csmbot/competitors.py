"""Other permissionless staking pools, for context.

Scope is deliberately narrow: counts and stake, week over week. No commentary, no ranking, no framing
of anyone as winning or losing. The point is to notice when something moves, not to keep score.

Only Rocket Pool for now. It is the closest comparable — permissionless entry, operator-provided
collateral, ETH-denominated — and adding pools without verifying each one's getters against mainnet is
exactly the fragility this bot is built to avoid. A second pool is a small addition once someone has
done that work.

**Contracts resolve through RocketStorage rather than being pinned.** Rocket Pool upgrades its
contracts and re-points the registry, so a hardcoded address goes stale silently. The registry address
is the only stable thing, and resolving through it is what keeps this working across their upgrades.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chain import Chain, to_address, to_uint, words
from .keccak import keccak256
from .keccak import selector as _selector

ROCKET_STORAGE = "0x1d8f8f00cfa6758d7bE78336684788Fb0ee0Fa46"

# Every Rocket Pool minipool is a 32 ETH validator; the node operator supplies 8 or 16 and the pool
# supplies the rest. So staked ETH is minipools x 32, the same arithmetic as CSM's 0x01 validators.
ETH_PER_MINIPOOL = 32


@dataclass
class PoolState:
    name: str
    available: bool = False
    nodes: int | None = None
    active_validators: int | None = None
    lifetime_validators: int | None = None
    note: str = ""

    @property
    def staked_eth(self) -> int | None:
        if self.active_validators is None:
            return None
        return self.active_validators * ETH_PER_MINIPOOL


def _registry_key(name: str) -> str:
    """RocketStorage keys contracts by keccak256("contract.address" + name)."""
    return keccak256(b"contract.address" + name.encode()).hex()


def read_rocket_pool(chain: Chain, block: int, warnings: list[str]) -> PoolState:
    state = PoolState(name="Rocket Pool")

    resolved: dict[str, str] = {}
    for contract in ("rocketNodeManager", "rocketMinipoolManager"):
        call = chain.call(
            ROCKET_STORAGE, "getAddress(bytes32)", _registry_key(contract), block=block
        )
        if not call.ok:
            warnings.append(f"Rocket Pool: could not resolve {contract}: {call.error}")
            return state
        address = to_address(words(call.response)[0])
        if int(address, 16) == 0:
            warnings.append(f"Rocket Pool: {contract} resolved to the zero address")
            return state
        resolved[contract] = address

    reads = chain.batch_call(
        [
            (resolved["rocketNodeManager"], "getNodeCount()", ""),
            (resolved["rocketMinipoolManager"], "getStakingMinipoolCount()", ""),
            (resolved["rocketMinipoolManager"], "getMinipoolCount()", ""),
        ],
        block,
    )
    labels = ("getNodeCount", "getStakingMinipoolCount", "getMinipoolCount")
    values: list[int | None] = []
    for label, call in zip(labels, reads):
        if not call.ok:
            warnings.append(f"Rocket Pool: {label} failed: {call.error}")
            values.append(None)
            continue
        values.append(to_uint(words(call.response)[0]))

    if any(v is None for v in values):
        return state

    state.nodes, state.active_validators, state.lifetime_validators = values
    state.available = True
    # `getNodeCount` counts registered nodes, including those with no active minipools. CSM's headline
    # operator figure is *active* operators, so the two are not directly comparable and the note has
    # to travel with the number.
    state.note = "registered nodes, not necessarily active"
    return state


def read_all(chain: Chain, block: int, warnings: list[str]) -> list[PoolState]:
    return [read_rocket_pool(chain, block, warnings)]


# -- comparison ------------------------------------------------------------


@dataclass
class PoolDelta:
    name: str
    nodes: int | None
    node_change: int | None
    validators: int | None
    validator_change: int | None
    staked_eth: int | None
    note: str = ""

    @property
    def validators_per_operator(self) -> float | None:
        """Validators per operator — the number that actually separates the two models.

        Measured 2026-08-20: CSM runs 24,729 keys across 428 active operators (~58 each), Rocket Pool
        14,297 minipools across 4,152 registered nodes (~3.4 each). A seventeen-fold difference in
        concentration says considerably more about who each protocol attracts than total staked ETH
        does, and it speaks directly to whether CSM is reaching individual stakers.
        """
        if not self.nodes or self.validators is None:
            return None
        return self.validators / self.nodes


def deltas(pools: list[PoolState], previous: dict[str, dict] | None) -> list[PoolDelta]:
    """Week-over-week movement. Changes are None until there is a prior reading to compare."""
    out = []
    for pool in pools:
        if not pool.available:
            out.append(PoolDelta(pool.name, None, None, None, None, None, pool.note))
            continue
        before = (previous or {}).get(pool.name)
        node_change = validator_change = None
        if before:
            if before.get("nodes") is not None and pool.nodes is not None:
                node_change = pool.nodes - before["nodes"]
            if (
                before.get("active_validators") is not None
                and pool.active_validators is not None
            ):
                validator_change = pool.active_validators - before["active_validators"]
        out.append(PoolDelta(
            name=pool.name,
            nodes=pool.nodes,
            node_change=node_change,
            validators=pool.active_validators,
            validator_change=validator_change,
            staked_eth=pool.staked_eth,
            note=pool.note,
        ))
    return out


def to_record(pools: list[PoolState]) -> dict[str, dict]:
    """Shape stored in the daily metric, so next week can diff against it."""
    return {
        pool.name: {
            "nodes": pool.nodes,
            "active_validators": pool.active_validators,
            "lifetime_validators": pool.lifetime_validators,
        }
        for pool in pools
        if pool.available
    }
