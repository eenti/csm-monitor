"""JSON-RPC client and ABI decoding.

Two things here exist for the sake of reproducibility rather than convenience:

**Block pinning.** A collection run resolves one block number up front and reads everything at that
block. Without this, a run that takes ninety seconds reads a moving chain, and the numbers in a brief
would not be consistent with each other — validator counts from one block, share limits from another.
Pinning also means a stored run can be replayed against an archive node and produce identical output.

**Raw responses.** Every call returns the undecoded hex alongside the decoded value, so the caller can
persist what the chain actually said. Decoding logic can be fixed later and old runs recomputed; a
discarded response is gone for good.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Sequence

from .keccak import selector as _selector


class ChainError(RuntimeError):
    """A call failed against every configured endpoint."""


@dataclass
class RawCall:
    """One RPC read, kept in full so the value can be re-derived later."""

    method: str
    target: str
    selector: str | None
    calldata: str
    block_number: int | None
    response: str | None
    ok: bool
    error: str | None = None
    endpoint: str | None = None


@dataclass
class Chain:
    endpoints: Sequence[str]
    timeout: int = 20
    retries: int = 3
    # Batches get more attempts than single calls: a failed chunk of sixty costs sixty fallback
    # round-trips, so it is worth trying harder to avoid that.
    batch_retries: int = 4
    backoff: float = 1.5
    calls: list[RawCall] = field(default_factory=list)

    # -- transport ---------------------------------------------------------

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "csm-bot/1.0"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _rpc(self, method: str, params: list) -> Any:
        """Try each endpoint in turn, with backoff. Raises if all of them fail.

        Endpoints are tried in order rather than at random so that a run's behaviour is the same on
        every replay, and so the first-listed endpoint can be the one we trust most.
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        failures = []

        for attempt in range(self.retries):
            for endpoint in self.endpoints:
                try:
                    result = self._post(endpoint, payload)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    failures.append(f"{endpoint}: {exc}")
                    continue

                if "error" in result:
                    # A revert is a real answer from a healthy node; retrying elsewhere will not
                    # help and would only obscure the cause.
                    raise ChainError(f"{method} reverted: {result['error']}")
                if "result" in result:
                    return result["result"], endpoint
                failures.append(f"{endpoint}: malformed response {result}")

            if attempt < self.retries - 1:
                time.sleep(self.backoff ** attempt)

        raise ChainError(f"{method} failed on all endpoints: {'; '.join(failures[-4:])}")

    # -- reads -------------------------------------------------------------

    def block_number(self) -> int:
        result, _ = self._rpc("eth_blockNumber", [])
        return int(result, 16)

    def block_timestamp(self, block: int) -> int:
        result, _ = self._rpc("eth_getBlockByNumber", [hex(block), False])
        return int(result["timestamp"], 16)

    def call(
        self,
        to: str,
        signature: str,
        args_hex: str = "",
        block: int | None = None,
    ) -> RawCall:
        """eth_call a function by signature, recording the raw result.

        `args_hex` is pre-encoded argument data without the 0x prefix. Encoding is left to the caller
        because every call this bot makes takes either no arguments or a single uint256, and a general
        ABI encoder would be more surface area than that justifies.
        """
        sel = _selector(signature)
        calldata = sel + args_hex
        block_tag = hex(block) if block is not None else "latest"

        try:
            result, endpoint = self._rpc("eth_call", [{"to": to, "data": calldata}, block_tag])
            record = RawCall("eth_call", to, sel, calldata, block, result, True, endpoint=endpoint)
        except ChainError as exc:
            record = RawCall("eth_call", to, sel, calldata, block, None, False, error=str(exc))

        self.calls.append(record)
        return record

    def batch_call(
        self,
        requests: Sequence[tuple[str, str, str]],
        block: int,
        chunk: int = 60,
    ) -> list[RawCall]:
        """eth_call many functions in JSON-RPC batches. `requests` is (to, signature, args_hex).

        This is what makes a daily snapshot of every operator affordable: measured against a public
        endpoint, 514 calls complete in 9 requests and under ten seconds. One call per operator over
        HTTP would take minutes and invite throttling.

        Results come back in request order. A failed item is recorded as a failed RawCall rather than
        dropped, so the caller can tell "this operator returned nothing" from "this operator was
        never asked".
        """
        out: list[RawCall] = []

        for start in range(0, len(requests), chunk):
            window = requests[start:start + chunk]
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "eth_call",
                    "params": [
                        {"to": to, "data": _selector(signature) + args},
                        hex(block),
                    ],
                }
                for index, (to, signature, args) in enumerate(window)
            ]

            responses: dict[int, dict] = {}
            error: str | None = None
            endpoint: str | None = None

            for attempt in range(self.batch_retries):
                for candidate in self.endpoints:
                    try:
                        result = self._post(candidate, payload)
                    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                        error = f"{candidate}: {exc}"
                        continue
                    if isinstance(result, list):
                        responses = {item.get("id"): item for item in result}
                        endpoint = candidate
                        break
                    # Not every provider supports JSON-RPC batching; cloudflare-eth.com returns a
                    # single error object instead of an array. Treat that as this endpoint being
                    # unsuitable and move on rather than as the data being unavailable.
                    error = f"{candidate}: batch returned {type(result).__name__}"
                if responses:
                    break
                if attempt < self.batch_retries - 1:
                    time.sleep(self.backoff ** attempt)

            for index, (to, signature, args) in enumerate(window):
                sel = _selector(signature)
                calldata = sel + args
                item = responses.get(index)
                if item and "result" in item:
                    out.append(RawCall("eth_call", to, sel, calldata, block,
                                       item["result"], True, endpoint=endpoint))
                    continue

                # Fall back to a single call for this item. A whole chunk failing used to abort the
                # collection, which meant one transient blip in any of ~30 batches cost the entire
                # day — and a lost day of history is not recoverable. One call at a time is slow, so
                # it only happens for the items that actually failed.
                detail = (item or {}).get("error") if item else error
                single = self.call(to, signature, args, block=block)
                # `call` already appended it to self.calls; keep the list authoritative by removing
                # the duplicate, since `out` is extended into it below.
                if self.calls and self.calls[-1] is single:
                    self.calls.pop()
                if single.ok:
                    out.append(single)
                else:
                    out.append(RawCall("eth_call", to, sel, calldata, block, None, False,
                                       error=f"batch: {detail}; single: {single.error}"))

        self.calls.extend(out)
        return out

    def logs(
        self,
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> RawCall:
        """eth_getLogs over an explicit block range.

        Measured 2026-08-20: public endpoints cap the span at **800 blocks**. Scanning a module's
        history that way costs thousands of requests, which is why nothing in this bot depends on
        event history — claim status is read from contract state via `isConsumed` instead. This method
        exists for narrow, recent lookups only.

        Callers must chunk; this method does not, so an over-wide range fails visibly rather than
        returning a quietly truncated set.
        """
        params = [{
            "address": address,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]
        descriptor = f"{from_block}-{to_block}"

        try:
            result, endpoint = self._rpc("eth_getLogs", params)
            record = RawCall(
                "eth_getLogs", address, topics[0] if topics else None, descriptor,
                to_block, json.dumps(result), True, endpoint=endpoint,
            )
        except ChainError as exc:
            record = RawCall(
                "eth_getLogs", address, topics[0] if topics else None, descriptor,
                to_block, None, False, error=str(exc),
            )

        self.calls.append(record)
        return record


# -- decoding --------------------------------------------------------------
#
# Returns are decoded positionally. Every decoder below takes the hex string a call returned and is
# total: it either produces a value or raises. Silent coercion of a malformed return into a plausible
# number is the specific failure this bot exists to avoid.


def words(hexstr: str) -> list[str]:
    """Split an ABI return into 32-byte words."""
    if not hexstr or not hexstr.startswith("0x"):
        raise ValueError(f"not a hex return: {hexstr!r}")
    body = hexstr[2:]
    if len(body) % 64 != 0:
        raise ValueError(f"return is not a whole number of words: {len(body)} hex chars")
    return [body[i:i + 64] for i in range(0, len(body), 64)]


def to_uint(word: str) -> int:
    return int(word, 16)


def to_address(word: str) -> str:
    return "0x" + word[-40:]


def to_bool(word: str) -> bool:
    value = int(word, 16)
    if value not in (0, 1):
        raise ValueError(f"not a boolean word: {word}")
    return bool(value)


def to_bytes32(word: str) -> str:
    return "0x" + word


def decode_uints(hexstr: str, count: int) -> list[int]:
    """Decode the first `count` words as uints, asserting the return is the expected width.

    The width check is the guard against a contract upgrade changing a return signature: a call that
    starts returning four values where it returned three will fail here rather than silently shifting
    every field by one position.
    """
    parts = words(hexstr)
    if len(parts) < count:
        raise ValueError(f"expected at least {count} words, got {len(parts)}")
    return [to_uint(w) for w in parts[:count]]


def decode_string(hexstr: str, word_index: int = 0) -> str:
    """Decode a dynamically-sized string from an ABI return.

    Used for `treeCid()`, which is how the bot learns where a merkle tree lives. The offset is read
    from the head rather than assumed, so this stays correct if a getter is ever changed to return the
    string alongside other values.
    """
    parts = words(hexstr)
    offset = to_uint(parts[word_index]) // 32
    if offset >= len(parts):
        raise ValueError(f"string offset {offset} past end of {len(parts)}-word return")
    length = to_uint(parts[offset])
    body = hexstr[2:][(offset + 1) * 64:]
    if len(body) < length * 2:
        raise ValueError(f"string claims {length} bytes but only {len(body) // 2} remain")
    return bytes.fromhex(body[:length * 2]).decode("utf-8")


def encode_uint(value: int) -> str:
    """Encode a single uint256 as calldata arguments (no 0x prefix)."""
    if value < 0:
        raise ValueError("uint256 cannot be negative")
    return f"{value:064x}"


def encode_address(value: str) -> str:
    """Encode an address as calldata arguments (no 0x prefix)."""
    clean = value.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError(f"not an address: {value!r}")
    return clean.rjust(64, "0")
