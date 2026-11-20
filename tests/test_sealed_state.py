"""Shared persisted state seal retained across independent owners."""
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.state.sealed import (
    is_sealed_state, seal_state, state_payload, write_sealed_json,
)


class TestSealedState(unittest.TestCase):
    def test_seal_verify_round_trip_and_nonfinite_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            state = {"schema_version": 1, "status": "RUNNING",
                     "request": {"region": "CHN", "delay": 1}}
            write_sealed_json(path, state)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["state_sha256"], state["state_sha256"])
            self.assertTrue(is_sealed_state(loaded))
            self.assertNotIn("state_sha256", state_payload(loaded))
            loaded["request"]["region"] = "USA"
            self.assertFalse(is_sealed_state(loaded))
            self.assertFalse(is_sealed_state({"status": "RUNNING"}))
            with self.assertRaises(ValueError):
                seal_state({"error": float("nan")})

    def test_seal_is_canonical_and_preserves_schema(self):
        first = seal_state({"a": 1, "b": {"region": "CHN", "delay": 1}})
        second = seal_state({"b": {"delay": 1, "region": "CHN"}, "a": 1})
        self.assertEqual(first["state_sha256"], second["state_sha256"])
        self.assertTrue(is_sealed_state(first))
        self.assertTrue(is_sealed_state(second))


if __name__ == "__main__":
    unittest.main()
