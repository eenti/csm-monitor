"""Tests for the pieces every number in a brief passes through.

Selectors, ABI decoding and the store are load-bearing: a fault in any of them corrupts every figure
downstream, silently and plausibly. They are tested against values that can be checked independently
rather than against the implementation's own output.

Run with:  python -m unittest discover -s tests -v
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from csmbot import chain
from csmbot.chain import RawCall
from csmbot.keccak import keccak256, selector, topic
from csmbot.store import Store


class TestKeccak(unittest.TestCase):
    def test_empty_string(self):
        # The canonical Keccak-256 of the empty input. Distinct from NIST SHA3-256, which is the
        # mistake this test exists to catch: hashlib.sha3_256 would give a different digest.
        self.assertEqual(
            keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )

    def test_known_selectors(self):
        # Selectors that can be checked against any block explorer.
        cases = {
            "transfer(address,uint256)": "0xa9059cbb",
            "balanceOf(address)": "0x70a08231",
            "totalSupply()": "0x18160ddd",
            "approve(address,uint256)": "0x095ea7b3",
            # Lido-specific, cross-checked against the deployed CSModule.
            "getNodeOperatorsCount()": "0xa70c70e4",
        }
        for signature, expected in cases.items():
            with self.subTest(signature=signature):
                self.assertEqual(selector(signature), expected)

    def test_known_event_topic(self):
        self.assertEqual(
            topic("Transfer(address,address,uint256)"),
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        )

    def test_multiblock_input(self):
        # Longer than the 136-byte rate, so the sponge absorbs more than once.
        digest = keccak256(b"a" * 500).hex()
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, keccak256(b"a" * 499).hex())


class TestDecoding(unittest.TestCase):
    def test_words_splits_on_32_byte_boundaries(self):
        raw = "0x" + "11" * 32 + "22" * 32
        self.assertEqual(chain.words(raw), ["11" * 32, "22" * 32])

    def test_words_rejects_partial_word(self):
        # A truncated return must raise rather than decode to a plausible-looking number.
        with self.assertRaises(ValueError):
            chain.words("0x" + "00" * 31)

    def test_words_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            chain.words("not hex")

    def test_uint_and_address(self):
        word = f"{12345:064x}"
        self.assertEqual(chain.to_uint(word), 12345)

        address_word = "0" * 24 + "da7de2ecddfccc6c3af10108db212acbbf9ea83f"
        self.assertEqual(
            chain.to_address(address_word), "0xda7de2ecddfccc6c3af10108db212acbbf9ea83f"
        )

    def test_bool_rejects_garbage(self):
        self.assertTrue(chain.to_bool(f"{1:064x}"))
        self.assertFalse(chain.to_bool(f"{0:064x}"))
        # A word that is neither 0 nor 1 means the return was misaligned; do not coerce it.
        with self.assertRaises(ValueError):
            chain.to_bool(f"{42:064x}")

    def test_decode_uints_enforces_width(self):
        raw = "0x" + f"{1:064x}" + f"{2:064x}" + f"{3:064x}"
        self.assertEqual(chain.decode_uints(raw, 3), [1, 2, 3])
        # This is the guard against a contract upgrade changing a return signature. Asking for more
        # words than were returned must fail loudly, not pad with zeros.
        with self.assertRaises(ValueError):
            chain.decode_uints(raw, 4)

    def test_encoders(self):
        self.assertEqual(chain.encode_uint(3), f"{3:064x}")
        with self.assertRaises(ValueError):
            chain.encode_uint(-1)

        encoded = chain.encode_address("0xdA7dE2ECdDfccC6c3AF10108Db212ACBBf9EA83F")
        self.assertEqual(len(encoded), 64)
        self.assertTrue(encoded.endswith("da7de2ecddfccc6c3af10108db212acbbf9ea83f"))
        with self.assertRaises(ValueError):
            chain.encode_address("0x1234")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_run_lifecycle(self):
        run_id = self.store.start_run("2026-08-19", 25787734, 1755600000)
        self.assertIsNone(self.store.last_successful_run())

        self.store.finish_run(run_id, "ok")
        last = self.store.last_successful_run()
        self.assertIsNotNone(last)
        self.assertEqual(last["block_number"], 25787734)

    def test_raw_calls_are_persisted_verbatim(self):
        run_id = self.store.start_run("2026-08-19", 100, 1755600000)
        calls = [
            RawCall("eth_call", "0xabc", "0xa70c70e4", "0xa70c70e4", 100,
                    "0x" + f"{615:064x}", True, endpoint="https://rpc"),
            RawCall("eth_call", "0xdef", "0xdeadbeef", "0xdeadbeef", 100,
                    None, False, error="reverted"),
        ]
        self.store.record_calls(run_id, calls)

        rows = self.store.raw_for_run(run_id)
        self.assertEqual(len(rows), 2)
        # The successful response round-trips untouched, so it can be re-decoded later.
        self.assertEqual(chain.to_uint(chain.words(rows[0]["response"])[0]), 615)
        # The failure is kept too — a missing row and a failed row mean different things.
        self.assertEqual(rows[1]["ok"], 0)
        self.assertEqual(rows[1]["error"], "reverted")

    def test_metric_upsert_keeps_one_row_per_day(self):
        run_id = self.store.start_run("2026-08-19", 100, 1755600000)
        self.store.put_metric("2026-08-19", "csm_share", 8.30, run_id, 100)
        self.store.put_metric("2026-08-19", "csm_share", 8.31, run_id, 101)

        row = self.store.get_metric("2026-08-19", "csm_share")
        self.assertAlmostEqual(row["value"], 8.31)
        self.assertEqual(row["block_number"], 101)

    def test_metric_detail_round_trips_as_json(self):
        run_id = self.store.start_run("2026-08-19", 100, 1755600000)
        self.store.put_metric(
            "2026-08-19", "cohorts", None, run_id, 100, detail={"ics": 220, "default": 380}
        )
        import json
        detail = json.loads(self.store.get_metric("2026-08-19", "cohorts")["detail"])
        self.assertEqual(detail["ics"], 220)

    def test_alert_fires_once_per_episode(self):
        # The whole point of the dedupe: a condition true for a fortnight is one message.
        self.assertTrue(self.store.raise_alert("runway:30", {"days": 28}))
        self.assertFalse(self.store.raise_alert("runway:30", {"days": 26}))
        self.assertFalse(self.store.raise_alert("runway:30", {"days": 24}))

        # Once it goes false and true again, it is a new episode and should be reported.
        self.store.clear_alert("runway:30")
        self.assertFalse(self.store.alert_is_active("runway:30"))
        self.assertTrue(self.store.raise_alert("runway:30", {"days": 29}))

    def test_gaps_are_recorded_and_cleared(self):
        self.store.note_gap("2026-08-18", "rpc unreachable")
        self.assertEqual(len(self.store.gaps_between("2026-08-01", "2026-08-31")), 1)
        self.store.clear_gap("2026-08-18")
        self.assertEqual(len(self.store.gaps_between("2026-08-01", "2026-08-31")), 0)

    def test_delivery_is_idempotent(self):
        # A redeploy mid-week must not resend Monday's brief.
        self.assertFalse(self.store.already_delivered("brief", "2026-W34"))
        self.store.record_delivery("brief", "2026-W34", "body")
        self.assertTrue(self.store.already_delivered("brief", "2026-W34"))
        self.store.record_delivery("brief", "2026-W34", "different body")
        rows = self.store.db.execute("SELECT COUNT(*) c FROM deliveries").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_metric_series_is_ordered_and_bounded(self):
        run_id = self.store.start_run("2026-08-19", 100, 1755600000)
        for day, value in [("2026-08-17", 1.0), ("2026-08-19", 3.0), ("2026-08-18", 2.0)]:
            self.store.put_metric(day, "x", value, run_id, 100)

        series = self.store.metric_series("x", "2026-08-17", "2026-08-18")
        self.assertEqual([r["day"] for r in series], ["2026-08-17", "2026-08-18"])

    def test_foreign_key_is_enforced(self):
        # A metric with no run behind it has no provenance, which defeats the point of the schema.
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.put_metric("2026-08-19", "orphan", 1.0, 99999, 100)


if __name__ == "__main__":
    unittest.main()


class TestMessageSplitting(unittest.TestCase):
    """A brief must never lose a section to Telegram's length cap."""

    def test_short_message_is_untouched(self):
        from csmbot.telegram import split_message
        self.assertEqual(split_message("hello"), ["hello"])

    def test_splits_on_block_boundaries(self):
        from csmbot.telegram import split_message
        blocks = ["BLOCK-%02d\n%s" % (i, "x" * 400) for i in range(20)]
        parts = split_message("\n\n".join(blocks), limit=1000)

        self.assertGreater(len(parts), 1)
        # Every block survives somewhere; nothing is dropped.
        joined = "".join(parts)
        for i in range(20):
            self.assertIn("BLOCK-%02d" % i, joined)
        # Parts are labelled so a reader knows more is coming.
        self.assertIn("(1/", parts[0])

    def test_oversized_single_line_still_splits(self):
        from csmbot.telegram import split_message
        parts = split_message("y" * 5000, limit=1000)
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum(p.count("y") for p in parts), 5000)

    def test_every_part_respects_the_limit(self):
        from csmbot.telegram import split_message
        text = "\n\n".join("section %d\n%s" % (i, "z" * 300) for i in range(30))
        for part in split_message(text, limit=1000):
            self.assertLessEqual(len(part), 1000 + 12)  # + continuation marker


class TestBatchFallback(unittest.TestCase):
    """A failed batch must not lose the run.

    One transient blip in any of the ~30 batches a collection makes used to abort the whole thing, and
    a lost day of history cannot be recovered. Failed items now fall back to single calls.
    """

    def setUp(self):
        from csmbot.chain import Chain
        self.Chain = Chain

    def test_non_list_batch_response_falls_back_to_single_calls(self):
        chain = self.Chain(endpoints=["http://a"], batch_retries=1, backoff=1.0)
        posted = []

        def fake_post(endpoint, payload):
            posted.append(payload)
            if isinstance(payload, list):
                # What cloudflare-eth.com does: an error object instead of an array.
                return {"error": {"code": -32600, "message": "batch not supported"}}
            return {"jsonrpc": "2.0", "id": 1, "result": "0x" + f"{42:064x}"}

        chain._post = fake_post
        results = chain.batch_call([("0xabc", "f()", ""), ("0xabc", "g()", "")], block=100)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        from csmbot import chain as chain_mod
        self.assertEqual(chain_mod.to_uint(chain_mod.words(results[0].response)[0]), 42)

    def test_partial_batch_failure_only_retries_the_failed_items(self):
        chain = self.Chain(endpoints=["http://a"], batch_retries=1, backoff=1.0)
        singles = []

        def fake_post(endpoint, payload):
            if isinstance(payload, list):
                # First item errors, second succeeds.
                return [
                    {"jsonrpc": "2.0", "id": 0, "error": {"message": "boom"}},
                    {"jsonrpc": "2.0", "id": 1, "result": "0x" + f"{7:064x}"},
                ]
            singles.append(payload)
            return {"jsonrpc": "2.0", "id": 1, "result": "0x" + f"{99:064x}"}

        chain._post = fake_post
        results = chain.batch_call([("0xabc", "f()", ""), ("0xabc", "g()", "")], block=100)

        from csmbot import chain as chain_mod
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(chain_mod.to_uint(chain_mod.words(results[0].response)[0]), 99)  # refetched
        self.assertEqual(chain_mod.to_uint(chain_mod.words(results[1].response)[0]), 7)   # from batch
        self.assertEqual(len(singles), 1, "only the failed item should be refetched")

    def test_recorded_calls_are_not_duplicated_by_the_fallback(self):
        chain = self.Chain(endpoints=["http://a"], batch_retries=1, backoff=1.0)

        def fake_post(endpoint, payload):
            if isinstance(payload, list):
                return {"error": {"message": "no batching"}}
            return {"jsonrpc": "2.0", "id": 1, "result": "0x" + f"{1:064x}"}

        chain._post = fake_post
        results = chain.batch_call([("0xabc", "f()", "")], block=100)
        # batch_call records into chain.calls itself; the fallback single must not leave a second
        # copy behind, or the stored provenance would double-count every refetched read.
        self.assertEqual(len(results), 1)
        self.assertEqual(len(chain.calls), 1)

    def test_total_failure_is_reported_not_silently_zero(self):
        chain = self.Chain(endpoints=["http://a"], batch_retries=1, retries=1, backoff=1.0)

        def fake_post(endpoint, payload):
            raise TimeoutError("down")

        chain._post = fake_post
        results = chain.batch_call([("0xabc", "f()", "")], block=100)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIsNone(results[0].response)
        self.assertIn("single:", results[0].error)
