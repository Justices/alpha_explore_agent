"""Canonical hash helper is deterministic and excludes nonfinite JSON."""
import unittest

from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json


class FingerprintTests(unittest.TestCase):
    def test_object_key_order_does_not_affect_hash(self):
        a = {"settings": {"delay": 1, "region": "CHN"}, "alpha": "A1"}
        b = {"alpha": "A1", "settings": {"region": "CHN", "delay": 1}}
        self.assertEqual(canonical_json_bytes(a), canonical_json_bytes(b))
        self.assertEqual(fingerprint_json(a), fingerprint_json(b))
        self.assertEqual(len(fingerprint_json(a)), 64)

    def test_invalid_nonfinite_evidence_rejected(self):
        with self.assertRaises(ValueError):
            fingerprint_json({"sharpe": float("nan")})
        with self.assertRaises(ValueError):
            fingerprint_json({"fitness": float("inf")})


if __name__ == "__main__":
    unittest.main()
