"""Offline durability tests for the shared atomic JSON state writer."""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from alpha_research.state.store import atomic_write_json
from alpha_research.state.sealed import is_sealed_state, write_sealed_json


class AtomicStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file = self.root / "ledger" / "attempts.json"

    def leftovers(self):
        return list(self.file.parent.glob("." + self.file.name + ".tmp-*"))

    def test_nested_json_round_trip_and_no_temporary_files(self):
        payload = {"description": "中文审计", "calls": [{"id": "C1"}]}
        atomic_write_json(self.file, payload)
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), payload)
        self.assertEqual(self.leftovers(), [])

    def test_replace_failure_preserves_previous_state_and_cleans_temporary(self):
        atomic_write_json(self.file, {"generation": 1})
        before = self.file.read_bytes()
        with patch("alpha_research.state.store.os.replace",
                   side_effect=OSError("synthetic rename failure")):
            with self.assertRaisesRegex(OSError, "synthetic rename"):
                atomic_write_json(self.file, {"generation": 2})
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_hard_interrupt_before_replace_preserves_previous_state(self):
        atomic_write_json(self.file, {"generation": 1})
        before = self.file.read_bytes()
        with patch("alpha_research.state.store.os.replace",
                   side_effect=KeyboardInterrupt("synthetic crash")):
            with self.assertRaisesRegex(KeyboardInterrupt, "synthetic crash"):
                atomic_write_json(self.file, {"generation": 2})
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_serialization_error_never_replaces_existing_state(self):
        atomic_write_json(self.file, {"generation": 1})
        before = self.file.read_bytes()
        with self.assertRaises(TypeError):
            atomic_write_json(self.file, {"bad": object()})
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_file_content_fsync_is_part_of_atomic_publication(self):
        actual = os.fsync
        calls = []
        def observe(descriptor):
            calls.append(descriptor)
            return actual(descriptor)
        with patch("alpha_research.state.store.os.fsync", side_effect=observe):
            atomic_write_json(self.file, {"ok": True})
        self.assertGreaterEqual(len(calls), 2 if os.name == "posix" else 1)
        self.assertEqual(self.leftovers(), [])

    def test_parallel_independent_writers_publish_whole_json(self):
        # The atomic writer does not serialize concurrent updates of the
        # *same* logical record: the caller must hold its transaction lock.
        # Parallel independent destinations must not collide or leak temps.
        begin = Barrier(4, timeout=8)
        payloads = [{"author": i, "body": "x" * 10000} for i in range(3)]
        destinations = [
            self.file.with_name(f"attempts-{i}.json") for i in range(3)
        ]

        def write(path, value):
            begin.wait()
            atomic_write_json(path, value)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(write, path, payload)
                       for path, payload in zip(destinations, payloads)]
            begin.wait()
            for future in futures:
                future.result(timeout=15)
        for path, value in zip(destinations, payloads):
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), value)
        self.assertEqual(list(self.file.parent.glob(".*.tmp-*")), [])

    def test_sealed_ledger_uses_same_durable_atomic_writer(self):
        state = {"schema_version": 1, "research_id": "R1", "calls": []}
        write_sealed_json(self.file, state)
        loaded = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertTrue(is_sealed_state(loaded))
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
