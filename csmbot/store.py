"""SQLite persistence.

The schema is built around one idea: **raw responses are the record, computed metrics are a cache.**

Most of what this bot measures is only observable at the head of the chain. A validator count at
block N is not recoverable next month without an archive node, and the depth of a deposit queue on a
given Tuesday is not recoverable at all. So a collection run persists what the chain and the network
actually returned, and metrics are derived from that. When a metric turns out to be computed wrong,
the fix is to recompute from `raw` — not to shrug and start the history over.

`raw` grows by a few hundred rows a day. At that rate the database is measured in megabytes per year,
which is not worth optimising away.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .chain import RawCall

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    day            TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    block_number   INTEGER,
    block_time     INTEGER,
    status         TEXT    NOT NULL DEFAULT 'running',
    note           TEXT
);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day);

-- Every read, kept undecoded. This table is the reason a brief can be re-derived later.
CREATE TABLE IF NOT EXISTS raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    method        TEXT    NOT NULL,
    target        TEXT    NOT NULL,
    selector      TEXT,
    calldata      TEXT,
    block_number  INTEGER,
    response      TEXT,
    ok            INTEGER NOT NULL,
    error         TEXT,
    endpoint      TEXT,
    fetched_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_run    ON raw(run_id);
CREATE INDEX IF NOT EXISTS raw_lookup ON raw(target, selector, block_number);

-- Derived values. Always reconstructible from `raw`; stored so a brief does not recompute a month
-- of history on every run.
CREATE TABLE IF NOT EXISTS metrics (
    day          TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    value        REAL,
    detail       TEXT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    block_number INTEGER,
    PRIMARY KEY (day, name)
);

-- Alert episodes. An alert fires when it becomes true and stays quiet until it has gone false again,
-- so a condition that persists for a fortnight produces one message rather than fourteen.
CREATE TABLE IF NOT EXISTS alert_state (
    key            TEXT PRIMARY KEY,
    active         INTEGER NOT NULL,
    first_fired_at TEXT,
    last_fired_at  TEXT,
    cleared_at     TEXT,
    payload        TEXT
);

-- Days with no successful run. Recorded explicitly so the brief can say "three days missing" instead
-- of quietly averaging over a hole.
CREATE TABLE IF NOT EXISTS gaps (
    day    TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    noted_at TEXT NOT NULL
);

-- Historical reward frames, backfilled from the git history of lidofinance/csm-rewards. This is the
-- one part of CSM's past that is recoverable without an archive node: every frame's merkle tree is a
-- commit on a public branch, and each tree carries cumulative fee shares per operator.
CREATE TABLE IF NOT EXISTS frames (
    sha            TEXT PRIMARY KEY,
    committed_at   TEXT    NOT NULL,
    operator_count INTEGER NOT NULL,
    total_shares   TEXT    NOT NULL,   -- decimal string; exceeds SQLite's integer range
    tree_root      TEXT    NOT NULL,
    payload        TEXT    NOT NULL,   -- {operator_id: cumulative_shares_as_string}
    fetched_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS frames_date ON frames(committed_at);

-- Each distinct eligibility tree a gate has published, with the addresses it added. This is what
-- makes "a batch was made eligible N days ago and nobody has claimed" answerable: the tree root
-- changes when governance adds addresses, and the first day we see a new root dates the batch.
CREATE TABLE IF NOT EXISTS gate_batches (
    gate         TEXT NOT NULL,
    tree_root    TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    tree_cid     TEXT NOT NULL,
    added_count  INTEGER NOT NULL,
    added_json   TEXT NOT NULL,     -- addresses absent from the previous root
    eligible_json TEXT NOT NULL,    -- the full set at this root, so `added` can be recomputed
    PRIMARY KEY (gate, tree_root)
);

-- Messages actually delivered, so a redeploy mid-week cannot double-send a brief.
CREATE TABLE IF NOT EXISTS deliveries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    body       TEXT NOT NULL,
    UNIQUE (kind, key)
);

-- Operator-controlled runtime settings. Environment variables provide defaults; values changed
-- through Telegram live here so they survive container restarts and redeploys with the rest of
-- /data. Secrets never belong in this table.
CREATE TABLE IF NOT EXISTS runtime_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN")
        try:
            yield self.db
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # -- runtime settings ---------------------------------------------------

    def get_setting(self, key: str) -> Any | None:
        row = self.db.execute(
            "SELECT value FROM runtime_settings WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row is not None else None

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            """INSERT INTO runtime_settings (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value, updated_at=excluded.updated_at""",
            (key, json.dumps(value), utcnow()),
        )

    def clear_settings(self, keys: tuple[str, ...] | None = None) -> None:
        if keys is None:
            self.db.execute("DELETE FROM runtime_settings")
            return
        self.db.executemany("DELETE FROM runtime_settings WHERE key=?", ((key,) for key in keys))

    # -- runs --------------------------------------------------------------

    def start_run(self, day: str, block_number: int, block_time: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs (day, started_at, block_number, block_time) VALUES (?,?,?,?)",
            (day, utcnow(), block_number, block_time),
        )
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str, note: str | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, note=? WHERE id=?",
            (utcnow(), status, note, run_id),
        )

    def last_successful_run(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def runs_between(self, start_day: str, end_day: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs WHERE day BETWEEN ? AND ? ORDER BY day", (start_day, end_day)
        ).fetchall()

    # -- raw ---------------------------------------------------------------

    def record_calls(self, run_id: int, calls: list[RawCall]) -> None:
        stamp = utcnow()
        self.db.executemany(
            """INSERT INTO raw
               (run_id, method, target, selector, calldata, block_number, response, ok, error,
                endpoint, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, c.method, c.target, c.selector, c.calldata, c.block_number,
                 c.response, 1 if c.ok else 0, c.error, c.endpoint, stamp)
                for c in calls
            ],
        )

    def raw_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM raw WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

    def find_raw(self, run_id: int, target: str, selector: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM raw WHERE run_id=? AND target=? AND selector=? AND ok=1 "
            "ORDER BY id DESC LIMIT 1",
            (run_id, target.lower(), selector),
        ).fetchone()

    # -- metrics -----------------------------------------------------------

    def put_metric(
        self,
        day: str,
        name: str,
        value: float | None,
        run_id: int,
        block_number: int | None = None,
        detail: Any = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO metrics (day, name, value, detail, run_id, block_number)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(day, name) DO UPDATE SET
                   value=excluded.value, detail=excluded.detail,
                   run_id=excluded.run_id, block_number=excluded.block_number""",
            (day, name, value, json.dumps(detail) if detail is not None else None,
             run_id, block_number),
        )

    def get_metric(self, day: str, name: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM metrics WHERE day=? AND name=?", (day, name)
        ).fetchone()

    def metric_series(self, name: str, start_day: str, end_day: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM metrics WHERE name=? AND day BETWEEN ? AND ? ORDER BY day",
            (name, start_day, end_day),
        ).fetchall()

    # -- gaps --------------------------------------------------------------

    def note_gap(self, day: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO gaps (day, reason, noted_at) VALUES (?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET reason=excluded.reason",
            (day, reason, utcnow()),
        )

    def clear_gap(self, day: str) -> None:
        self.db.execute("DELETE FROM gaps WHERE day=?", (day,))

    def gaps_between(self, start_day: str, end_day: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM gaps WHERE day BETWEEN ? AND ? ORDER BY day", (start_day, end_day)
        ).fetchall()

    # -- alerts ------------------------------------------------------------

    def alert_is_active(self, key: str) -> bool:
        row = self.db.execute("SELECT active FROM alert_state WHERE key=?", (key,)).fetchone()
        return bool(row and row["active"])

    def raise_alert(self, key: str, payload: Any = None) -> bool:
        """Mark an alert active. Returns True only on the transition from inactive to active."""
        if self.alert_is_active(key):
            self.db.execute(
                "UPDATE alert_state SET last_fired_at=?, payload=? WHERE key=?",
                (utcnow(), json.dumps(payload) if payload is not None else None, key),
            )
            return False
        stamp = utcnow()
        self.db.execute(
            """INSERT INTO alert_state (key, active, first_fired_at, last_fired_at, payload)
               VALUES (?,1,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   active=1, first_fired_at=excluded.first_fired_at,
                   last_fired_at=excluded.last_fired_at, payload=excluded.payload""",
            (key, stamp, stamp, json.dumps(payload) if payload is not None else None),
        )
        return True

    def clear_alert(self, key: str) -> None:
        self.db.execute(
            "UPDATE alert_state SET active=0, cleared_at=? WHERE key=? AND active=1",
            (utcnow(), key),
        )

    # -- frames ------------------------------------------------------------

    def put_frame(
        self, sha: str, committed_at: str, operator_count: int,
        total_shares: int, tree_root: str, payload: dict[int, int],
    ) -> None:
        self.db.execute(
            """INSERT INTO frames
               (sha, committed_at, operator_count, total_shares, tree_root, payload, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(sha) DO NOTHING""",
            (sha, committed_at, operator_count, str(total_shares), tree_root,
             json.dumps({str(k): str(v) for k, v in payload.items()}), utcnow()),
        )

    def has_frame(self, sha: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM frames WHERE sha=?", (sha,)
        ).fetchone() is not None

    def frames(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Frames oldest first, so consecutive rows can be differenced."""
        sql = "SELECT * FROM frames ORDER BY committed_at"
        if limit:
            sql = (
                "SELECT * FROM (SELECT * FROM frames ORDER BY committed_at DESC LIMIT ?) "
                "ORDER BY committed_at"
            )
            return self.db.execute(sql, (limit,)).fetchall()
        return self.db.execute(sql).fetchall()

    @staticmethod
    def frame_payload(row: sqlite3.Row) -> dict[int, int]:
        return {int(k): int(v) for k, v in json.loads(row["payload"]).items()}

    # -- gate batches ------------------------------------------------------

    def latest_gate_batch(self, gate: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM gate_batches WHERE gate=? ORDER BY first_seen DESC, rowid DESC LIMIT 1",
            (gate,),
        ).fetchone()

    def has_gate_root(self, gate: str, tree_root: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM gate_batches WHERE gate=? AND tree_root=?", (gate, tree_root)
        ).fetchone() is not None

    def put_gate_batch(
        self, gate: str, tree_root: str, day: str, tree_cid: str,
        added: list[str], eligible: list[str],
    ) -> None:
        self.db.execute(
            """INSERT INTO gate_batches
               (gate, tree_root, first_seen, tree_cid, added_count, added_json, eligible_json)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(gate, tree_root) DO NOTHING""",
            (gate, tree_root, day, tree_cid, len(added),
             json.dumps(added), json.dumps(eligible)),
        )

    def gate_batches(self, gate: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM gate_batches WHERE gate=? ORDER BY first_seen", (gate,)
        ).fetchall()

    @staticmethod
    def batch_addresses(row: sqlite3.Row) -> list[str]:
        """The addresses this tree added relative to the previous one."""
        return json.loads(row["added_json"])

    @staticmethod
    def batch_eligible(row: sqlite3.Row) -> list[str]:
        """The full eligible set at this tree root."""
        return json.loads(row["eligible_json"])

    # -- deliveries --------------------------------------------------------

    def already_delivered(self, kind: str, key: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM deliveries WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return row is not None

    def record_delivery(self, kind: str, key: str, body: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO deliveries (kind, key, sent_at, body) VALUES (?,?,?,?)",
            (kind, key, utcnow(), body),
        )
