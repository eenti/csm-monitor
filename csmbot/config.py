"""Configuration, validated once at startup.

Everything is read from the environment and checked before the bot does any work. A container that
starts with a malformed address or a missing token should die immediately with a clear message,
rather than run for six days and produce a brief with a hole in it.

Contract addresses are pinned here rather than discovered, with one exception: the CSM module address
is cross-checked against the Staking Router at startup. Lido's contracts sit behind proxies, and a
proxy whose implementation changes keeps its address — so pinning the address is safe, while assuming
the ABI behind it never changes is not. That check is in `sources.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

MAINNET = "mainnet"

# Verified against mainnet on 2026-08-19. CSM is staking module id 3; some documentation says 2,
# which is wrong — `sources.preflight` re-checks this on every start.
ADDRESSES = {
    "staking_router":     "0xFdDf38947aFB03C621C71b06C9C70bce73f12999",
    "csm":                "0xdA7dE2ECdDfccC6c3AF10108Db212ACBBf9EA83F",
    "cs_accounting":      "0x4d72BFF1BeaC69925F8Bd12526a39BAAb069e5Da",
    "cs_fee_distributor": "0xD99CC66fEC647E68294C6477B40fC7E0F6F618D0",
    "cs_fee_oracle":      "0x4D4074628678Bd302921c20573EEa1ed38DdF7FB",
    "hash_consensus":     "0x71093efF8D8599b5fA340D665Ad60fA7C80688e4",
    "parameters_registry": "0x9D28ad303C90DF524BA960d7a2DAC56DcC31e428",
    "validator_strikes":  "0xaa328816027F2D32B9F56d190BC9Fa4A5C07637f",
    "exit_penalties":     "0x06cd61045f958A209a0f8D746e103eCc625f4193",
    "ejector":            "0x610B517D380f287c239C93F8eF6FfBd567AA4bA5",
    "gate_permissionless": "0xb8cd8F059Ad7a5dB8CAfDe34aAb007317F7156C8",
    "gate_ics":           "0xB314D4A76C457c93150d308787939063F4Cc67E0",
    "gate_idvtc":         "0xa12760721A72A7199aB38059DA6690b9Cd4ed7B8",
}

CSM_MODULE_ID = 3

# Public fallbacks. A datacenter IP gets throttled harder than a residential one, so a private
# endpoint in CSM_RPC_URLS is strongly preferred once this is running on a server.
DEFAULT_RPC = (
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
)

REWARDS_TREE_URL = "https://raw.githubusercontent.com/lidofinance/csm-rewards/mainnet/tree.json"

# Assessment round cutoffs, from the 2026 calendar published on research.lido.fi (topic 5917).
# Dates only, no logic — override with CSM_ROUNDS="label=YYYY-MM-DD,label=YYYY-MM-DD".
DEFAULT_ROUNDS = (
    ("ICS Round 6", "2026-09-07"),
    ("IDVTC Round 2", "2026-09-21"),
    ("ICS Round 7", "2026-12-07"),
    ("IDVTC Round 3", "2026-12-21"),
)


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _rounds() -> tuple[tuple[str, str], ...]:
    raw = os.environ.get("CSM_ROUNDS", "").strip()
    if not raw:
        return DEFAULT_ROUNDS
    out = []
    for item in raw.split(","):
        if "=" not in item:
            raise ConfigError(f"CSM_ROUNDS entry must be label=YYYY-MM-DD, got {item!r}")
        label, cutoff = item.split("=", 1)
        try:
            date.fromisoformat(cutoff.strip())
        except ValueError as exc:
            raise ConfigError(f"CSM_ROUNDS date not ISO: {cutoff!r}") from exc
        out.append((label.strip(), cutoff.strip()))
    return tuple(out)


def _check_address(label: str, value: str) -> str:
    clean = value.strip()
    if not clean.startswith("0x") or len(clean) != 42:
        raise ConfigError(f"{label} is not a valid address: {value!r}")
    try:
        int(clean, 16)
    except ValueError as exc:
        raise ConfigError(f"{label} is not hex: {value!r}") from exc
    return clean


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str
    rpc_urls: tuple[str, ...]
    db_path: str
    brief_weekday: int          # 0 = Monday
    brief_hour_utc: int
    collect_hour_utc: int = 6   # before the brief, so Monday's brief includes that morning's read
    addresses: dict[str, str] = field(default_factory=lambda: dict(ADDRESSES))
    module_id: int = CSM_MODULE_ID
    rewards_tree_url: str = REWARDS_TREE_URL
    dry_run: bool = False
    ipfs_cache_dir: str = "/data/ipfs"
    # Optional. When set, trees fetched from IPFS are re-pinned so they stay retrievable for the
    # operators who need them to claim. The bot itself does not need this — its local cache is what
    # makes it reproducible — so a missing token is not an error.
    pin_token: str | None = None
    assessment_rounds: tuple[tuple[str, str], ...] = DEFAULT_ROUNDS

    @classmethod
    def from_env(cls) -> "Config":
        rpc_raw = os.environ.get("CSM_RPC_URLS", "").strip()
        rpc_urls = tuple(u.strip() for u in rpc_raw.split(",") if u.strip()) or DEFAULT_RPC

        brief_weekday = _int("CSM_BRIEF_WEEKDAY", 0)
        if not 0 <= brief_weekday <= 6:
            raise ConfigError("CSM_BRIEF_WEEKDAY must be 0-6 (0 = Monday)")

        # 07:00 UTC is 14:00 in Ho Chi Minh City, which is UTC+7 all year — Vietnam has not
        # observed daylight saving since 1975, so this needs no seasonal adjustment.
        brief_hour = _int("CSM_BRIEF_HOUR_UTC", 7)
        if not 0 <= brief_hour <= 23:
            raise ConfigError("CSM_BRIEF_HOUR_UTC must be 0-23")

        collect_hour = _int("CSM_COLLECT_HOUR_UTC", 6)
        if not 0 <= collect_hour <= 23:
            raise ConfigError("CSM_COLLECT_HOUR_UTC must be 0-23")

        dry_run = os.environ.get("CSM_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

        config = cls(
            telegram_token="dry-run" if dry_run else _require("CSM_TELEGRAM_TOKEN"),
            telegram_chat_id="dry-run" if dry_run else _require("CSM_TELEGRAM_CHAT_ID"),
            rpc_urls=rpc_urls,
            db_path=os.environ.get("CSM_DB_PATH", "/data/csm.db"),
            brief_weekday=brief_weekday,
            brief_hour_utc=brief_hour,
            collect_hour_utc=collect_hour,
            dry_run=dry_run,
            ipfs_cache_dir=os.environ.get("CSM_IPFS_CACHE_DIR", "/data/ipfs"),
            pin_token=os.environ.get("CSM_PINATA_JWT", "").strip() or None,
            assessment_rounds=_rounds(),
        )

        for label, address in config.addresses.items():
            config.addresses[label] = _check_address(label, address)

        return config

    def address(self, label: str) -> str:
        try:
            return self.addresses[label]
        except KeyError as exc:
            raise ConfigError(f"unknown contract label: {label}") from exc
