"""Canonical finite-JSON hashing shared by research, budgets, and audit stores.

This module does not sign or authenticate data. A digest detects accidental
mutation; it is not a MAC against a malicious writer with filesystem access.
"""
import hashlib
import json


def canonical_json_bytes(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def fingerprint_json(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
