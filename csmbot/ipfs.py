"""Fetching the merkle trees that CSM publishes to IPFS, and keeping them retrievable.

Three of the bot's inputs live on IPFS, addressed by a CID read from a contract: the ICS eligibility
tree, the IDVTC eligibility tree, and the validator strikes tree.

**IPFS availability is the least reliable thing this bot touches.** Measured on 2026-08-20 against a
two-month-old ICS tree CID: `ipfs.io` returned 504, `dweb.link` returned 504, `cloudflare-ipfs.com`
refused the connection, and `gateway.pinata.cloud` served it. The current CID was fine everywhere.
The pattern is that content stays available while something keeps it alive and quietly stops being
retrievable later.

So this module does three things, in order of how much they matter:

1. **Caches every tree on disk the first time it is seen.** This is what makes the bot reproducible.
   Once a tree is cached it never needs IPFS again, and a CID going dark cannot retroactively break a
   past brief. Non-negotiable.
2. **Tries gateways in order, and records which one answered.** So a degrading gateway shows up as a
   fact rather than a mystery.
3. **Optionally re-pins what it fetches**, if a pinning service is configured. This does nothing for
   the bot — the local cache already covers it — but it keeps the tree retrievable for the operators
   who need it to claim. Off unless a token is set.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Tried in order. Pinata is last-resort in normal operation but is the one that answered for an old
# CID, so it stays in the list rather than being dropped for being slower.
GATEWAYS = (
    "https://ipfs.io/ipfs",
    "https://dweb.link/ipfs",
    "https://gateway.pinata.cloud/ipfs",
    "https://w3s.link/ipfs",
    "https://4everland.io/ipfs",
)

PIN_ENDPOINT = "https://api.pinata.cloud/pinning/pinByHash"


class IpfsError(RuntimeError):
    pass


@dataclass
class Fetched:
    cid: str
    body: bytes
    source: str          # gateway URL, or "cache"
    from_cache: bool

    def json(self):
        return json.loads(self.body)


@dataclass
class Ipfs:
    cache_dir: Path
    gateways: tuple[str, ...] = GATEWAYS
    timeout: int = 40
    pin_token: str | None = None
    pin_log: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, cid: str) -> Path:
        # CIDs are filesystem-safe, but hashing keeps the name bounded regardless of CID version.
        return self.cache_dir / f"{hashlib.sha256(cid.encode()).hexdigest()[:32]}.json"

    def fetch(self, cid: str, min_bytes: int = 64) -> Fetched:
        """Return the content for a CID, from cache if present, otherwise from the first gateway
        that serves it. Caches on success. Raises if no gateway answers.

        `min_bytes` guards against a gateway returning an error page with a 200 status, which does
        happen — a few hundred bytes of HTML where a merkle tree was expected.
        """
        cached = self._cache_path(cid)
        if cached.exists():
            return Fetched(cid, cached.read_bytes(), "cache", True)

        failures = []
        for gateway in self.gateways:
            url = f"{gateway}/{cid}"
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "csm-bot/1.0"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures.append(f"{gateway}: {exc}")
                continue

            if len(body) < min_bytes:
                failures.append(f"{gateway}: {len(body)} bytes, too short to be real")
                continue
            try:
                json.loads(body)
            except json.JSONDecodeError:
                failures.append(f"{gateway}: served non-JSON")
                continue

            cached.write_bytes(body)
            if self.pin_token:
                self._pin(cid)
            return Fetched(cid, body, gateway, False)

        raise IpfsError(f"no gateway served {cid}: {'; '.join(failures)}")

    def _pin(self, cid: str) -> None:
        """Ask the pinning service to keep this CID alive.

        Best-effort by design: a failure here is logged and ignored. The local cache is what the bot
        depends on, and a brief must not fail because a pinning API was down.
        """
        payload = json.dumps({"hashToPin": cid}).encode()
        request = urllib.request.Request(
            PIN_ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.pin_token}",
                "User-Agent": "csm-bot/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.pin_log.append(f"pinned {cid} ({response.status})")
        except urllib.error.HTTPError as exc:
            # Already-pinned is a success as far as we care.
            detail = exc.read()[:200].decode(errors="replace")
            if exc.code == 400 and "already" in detail.lower():
                self.pin_log.append(f"already pinned {cid}")
            else:
                self.pin_log.append(f"pin failed {cid}: HTTP {exc.code} {detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            self.pin_log.append(f"pin failed {cid}: {exc}")

    def cached_cids(self) -> int:
        return len(list(self.cache_dir.glob("*.json")))


def merkle_addresses(tree: dict) -> list[str]:
    """Extract the address list from an OpenZeppelin `standard-v1` merkle tree.

    CSM publishes eligibility trees in this format with `leafEncoding: ["address"]`, addresses at
    `values[].value[0]`. The format is asserted rather than assumed: if Lido ever changes the
    encoding, this raises instead of silently returning an empty or wrong list — which would read as
    "nobody is eligible" and quietly break the funnel.
    """
    if tree.get("format") != "standard-v1":
        raise IpfsError(f"unexpected merkle format: {tree.get('format')!r}")
    if tree.get("leafEncoding") != ["address"]:
        raise IpfsError(f"unexpected leaf encoding: {tree.get('leafEncoding')!r}")

    addresses = []
    for entry in tree.get("values", []):
        value = entry.get("value")
        if not isinstance(value, list) or len(value) != 1:
            raise IpfsError(f"unexpected leaf value: {value!r}")
        addresses.append(value[0].lower())

    if not addresses:
        raise IpfsError("merkle tree contained no addresses")
    return addresses


def merkle_root(tree: dict) -> str:
    """The tree's own root, for cross-checking against the root the contract reports.

    A mismatch means the CID and the on-chain root disagree, which should never happen and must not
    be papered over — it would mean the eligibility list the bot is reasoning about is not the one
    the protocol is enforcing.
    """
    nodes = tree.get("tree")
    if not isinstance(nodes, list) or not nodes:
        raise IpfsError("merkle tree has no nodes")
    return nodes[0].lower()
