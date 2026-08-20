"""Recovering CSM's past from the git history of the published rewards trees.

Almost nothing this bot measures is recoverable after the fact. A validator count at a past block
needs an archive node; the depth of the deposit queue last Tuesday is gone for good. There is one
exception, and it is a good one.

Every performance frame, the CSM oracle publishes a merkle tree of cumulative fee shares per operator,
and `lidofinance/csm-rewards` commits it to a public branch. That history goes back to 2024-11-27 —
about twenty-two months of frames. Each tree gives the operator set and each operator's cumulative
earnings at that moment, so **differencing consecutive frames yields what every operator earned in
that frame**, and an operator whose cumulative total did not move earned nothing.

Verified 2026-08-20 that the format is unchanged across CSM v1, v2 and v3: `standard-v1`, leaf
encoding `["uint256","uint256"]` of `[nodeOperatorId, cumulativeFeeShares]`, with leaf counts growing
178 → 347 → 567. The format is nonetheless asserted on every frame — CSM has been through three major
versions and will go through more, and a silently misparsed tree would corrupt the whole series.

Fetches are deliberately paced. Twenty-three sequential requests is not much, but walking rather than
sprinting keeps a one-off backfill from looking like abuse to a CDN.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

COMMITS_API = (
    "https://api.github.com/repos/lidofinance/csm-rewards/commits"
    "?sha=mainnet&per_page=100"
)
TREE_RAW = "https://raw.githubusercontent.com/lidofinance/csm-rewards/{sha}/tree.json"

EXPECTED_FORMAT = "standard-v1"
EXPECTED_LEAF_ENCODING = ["uint256", "uint256"]


class BackfillError(RuntimeError):
    pass


def _get(url: str, pause: float = 1.2, timeout: int = 45, tries: int = 3) -> bytes:
    """Fetch, then pause. The pause is the point — see the module docstring."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "csm-bot/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            time.sleep(pause)
            return body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise BackfillError(f"{url}: {last}")


@dataclass
class FrameCommit:
    sha: str
    date: str
    message: str


def list_frame_commits() -> list[FrameCommit]:
    """Commits on the `mainnet` branch that published a tree, oldest first.

    Filtered by message: the branch also carries a README and an initial commit that contain no tree.
    """
    payload = json.loads(_get(COMMITS_API, pause=0.5))
    commits = []
    for entry in payload:
        message = entry["commit"]["message"].split("\n")[0]
        if "tree" not in message.lower():
            continue
        commits.append(FrameCommit(
            sha=entry["sha"],
            date=entry["commit"]["author"]["date"][:10],
            message=message,
        ))
    commits.reverse()
    return commits


def parse_tree(body: bytes) -> tuple[str, dict[int, int]]:
    """Return (root, {operator_id: cumulative_shares}), asserting the format.

    A tree whose shape we do not recognise raises. The alternative — parsing it optimistically — would
    put wrong numbers into a series that is meant to be the reliable part of the history.
    """
    tree = json.loads(body)

    if tree.get("format") != EXPECTED_FORMAT:
        raise BackfillError(f"unexpected tree format {tree.get('format')!r}")
    if tree.get("leafEncoding") != EXPECTED_LEAF_ENCODING:
        raise BackfillError(f"unexpected leaf encoding {tree.get('leafEncoding')!r}")

    nodes = tree.get("tree")
    if not isinstance(nodes, list) or not nodes:
        raise BackfillError("tree has no nodes")

    shares: dict[int, int] = {}
    for entry in tree.get("values", []):
        value = entry.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise BackfillError(f"unexpected leaf value {value!r}")
        shares[int(value[0])] = int(value[1])

    if not shares:
        raise BackfillError("tree contained no operators")
    return nodes[0].lower(), shares


def run(store, on_progress=None) -> dict:
    """Fetch and store every frame not already held. Idempotent — safe to run on every start.

    Returns a summary; individual frame failures are collected rather than raised, so one unreachable
    commit does not abandon the other twenty-two.
    """
    commits = list_frame_commits()
    added, skipped, failed = 0, 0, []

    for commit in commits:
        if store.has_frame(commit.sha):
            skipped += 1
            continue
        try:
            root, shares = parse_tree(_get(TREE_RAW.format(sha=commit.sha)))
        except BackfillError as exc:
            failed.append(f"{commit.date} {commit.sha[:8]}: {exc}")
            continue

        store.put_frame(
            sha=commit.sha,
            committed_at=commit.date,
            operator_count=len(shares),
            total_shares=sum(shares.values()),
            tree_root=root,
            payload=shares,
        )
        added += 1
        if on_progress:
            on_progress(commit, len(shares))

    return {
        "commits_seen": len(commits),
        "added": added,
        "already_held": skipped,
        "failed": failed,
    }


# -- deriving the series ---------------------------------------------------


@dataclass
class FrameDelta:
    """What changed between two consecutive published frames.

    Note what is deliberately *not* here: a ratio of earners to non-earners. The rewards tree is
    cumulative and never removes an operator, so once someone withdraws their last validator they stay
    in the tree forever with a frozen total, counted as "earned nothing" in every subsequent frame.
    That makes any earners/total ratio decline mechanically as CSM ages, with no relationship to how
    validators are performing. Measured 2026-08-20: 159 operators earned nothing in the latest frame
    while 162 operators had withdrawn all their keys — the two populations are very nearly the same
    set, so almost none of that number is underperformance.

    `stopped_earning` is the honest version: operators who earned in the previous frame and earned
    nothing in this one. It excludes the accumulated retirees by construction, because they had
    already stopped.

    `interval_days` is the gap between *publications*, which is not the same as the gap between frame
    boundaries. A frame delivered late by an oracle problem stretches the interval without changing
    the period it covers. Measured across the 22 recovered frames: 6 intervals deviate from the
    28-day cadence (23, 30, 26, 30, 26 and 31 days). It is recorded so an odd interval is visible
    rather than quietly averaged into a per-day rate.
    """

    date: str
    previous_date: str
    interval_days: int             # between publications, not between frame boundaries
    operators_in_tree: int
    first_appearance: int          # present now, absent in the previous frame
    earned: int                    # cumulative shares increased
    earned_nothing: int            # present in both frames, cumulative total unchanged
    stopped_earning: int | None    # earned last frame, nothing this frame. None for the first pair
    resumed_earning: int | None    # earned nothing last frame, earning again now
    total_shares_distributed: int
    # Operator ids that earned nothing. Kept so the *cause* can be resolved against live key counts:
    # no active validators means retired, active validators means it earned nothing while running.
    earned_nothing_ids: list[int] = field(default_factory=list)


def deltas(store, limit: int | None = None) -> list[FrameDelta]:
    """Difference consecutive frames.

    `earned_nothing` is a fact about reward accrual, not a performance verdict: an operator earns
    nothing in a frame either by falling below the performance threshold or by having no active
    validators, and those are indistinguishable from this data alone. `stopped_earning` narrows it to
    the transition, which is the part worth looking at.
    """
    # One extra row on each side: computing `stopped_earning` for the newest frame needs the two
    # frames before it.
    rows = store.frames(limit=(limit + 2) if limit else None)
    out: list[FrameDelta] = []

    earners_by_index: list[set[int]] = []

    for index, (previous, current) in enumerate(zip(rows, rows[1:])):
        before = store.frame_payload(previous)
        after = store.frame_payload(current)

        first_appearance = earned = earned_nothing = 0
        distributed = 0
        earners: set[int] = set()
        idle: set[int] = set()

        for operator_id, total in after.items():
            if operator_id not in before:
                first_appearance += 1
                distributed += total
                earners.add(operator_id)
                continue
            change = total - before[operator_id]
            if change > 0:
                earned += 1
                distributed += change
                earners.add(operator_id)
            else:
                earned_nothing += 1
                idle.add(operator_id)

        stopped = resumed = None
        if earners_by_index:
            was_earning = earners_by_index[-1]
            stopped = len(idle & was_earning)
            resumed = len(earners - was_earning - {
                oid for oid in after if oid not in before
            })
        earners_by_index.append(earners)

        out.append(FrameDelta(
            date=current["committed_at"],
            previous_date=previous["committed_at"],
            interval_days=(
                date.fromisoformat(current["committed_at"])
                - date.fromisoformat(previous["committed_at"])
            ).days,
            operators_in_tree=len(after),
            first_appearance=first_appearance,
            earned=earned,
            earned_nothing=earned_nothing,
            stopped_earning=stopped,
            resumed_earning=resumed,
            total_shares_distributed=distributed,
            earned_nothing_ids=sorted(idle),
        ))

    if limit:
        out = out[-limit:]
    return out
