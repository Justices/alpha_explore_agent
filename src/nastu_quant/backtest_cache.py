"""File-backed scalable Bloom filter for completed backtests."""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import struct

from .state_store import verify_prereg


class BacktestCacheError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class _BloomLayer:
    HEADER = struct.Struct(">QQdQQ")

    def __init__(self, capacity, probability, *, count=0, bit_count=None,
                 hash_count=None, bits=None):
        self.capacity = int(capacity)
        self.count = int(count)
        self.probability = float(probability)
        if bit_count is None:
            bit_count = math.ceil(
                -self.capacity * math.log(self.probability) / math.log(2) ** 2)
        self.bit_count = int(bit_count)
        if hash_count is None:
            hash_count = round(self.bit_count / self.capacity * math.log(2))
        self.hash_count = max(1, int(hash_count))
        byte_count = (self.bit_count + 7) // 8
        self.bits = bytearray(byte_count) if bits is None else bytearray(bits)
        if (self.capacity < 1 or self.count < 0 or self.bit_count < 1 or
                self.hash_count < 1 or not 0.0 < self.probability < 1.0 or
                len(self.bits) != byte_count):
            raise BacktestCacheError("Bloom layer metadata is invalid")

    def copy(self):
        return _BloomLayer(
            self.capacity, self.probability, count=self.count,
            bit_count=self.bit_count, hash_count=self.hash_count,
            bits=self.bits)

    def _indexes(self, fingerprint):
        raw = int(fingerprint).to_bytes(8, "big", signed=False)
        digest = hashlib.blake2b(
            raw, digest_size=16, person=b"nastu-bloom-v1").digest()
        first = int.from_bytes(digest[:8], "big")
        step = int.from_bytes(digest[8:], "big") | 1
        for index in range(self.hash_count):
            yield (first + index * step) % self.bit_count

    def contains(self, fingerprint):
        for index in self._indexes(fingerprint):
            if not self.bits[index >> 3] & (1 << (index & 7)):
                return False
        return True

    def add(self, fingerprint):
        for index in self._indexes(fingerprint):
            self.bits[index >> 3] |= 1 << (index & 7)
        self.count += 1

    def to_bytes(self):
        return (self.HEADER.pack(
            self.capacity, self.count, self.probability,
            self.bit_count, self.hash_count) + bytes(self.bits))


class BacktestCache:
    """Persist only Bloom filter dimensions and bit arrays, never exact keys."""

    FILE_MAGIC = b"NASTUBF2"
    FILE_HEADER = struct.Struct(">8sQ32s")
    LAYER_COUNT = struct.Struct(">I")
    LEGACY_MAGICS = (b"NASTUBF1", b"NASTU_BACKTEST_CACHE\t")
    KEY_VERSION = 2
    TARGET_FALSE_POSITIVE_RATE = 1e-6
    INITIAL_CAPACITY = 100_000
    GROWTH_FACTOR = 2

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.needs_rebuild = False
        self._layers = self._load()

    @staticmethod
    def _scope_key(scope):
        if not isinstance(scope, dict):
            raise BacktestCacheError("backtest scope must be a JSON object")
        return {key: scope.get(key) for key in ("region", "universe", "delay")}

    @classmethod
    def fingerprint(cls, expression, dataset_id, scope, settings):
        if not isinstance(expression, str) or not expression.strip():
            raise BacktestCacheError("expression is required for backtest fingerprint")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise BacktestCacheError("Dataset ID is required for backtest fingerprint")
        if not isinstance(settings, dict):
            raise BacktestCacheError("simulation settings must be a JSON object")
        payload = {"key_version": cls.KEY_VERSION,
                   "expression": expression.strip(),
                   "dataset_id": dataset_id,
                   "scope": cls._scope_key(scope),
                   "settings": settings}
        try:
            encoded = _canonical(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BacktestCacheError(
                "simulation fingerprint contains non-JSON values") from exc
        digest = hashlib.blake2b(encoded, digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=False)

    @classmethod
    def _new_layer(cls, index):
        capacity = cls.INITIAL_CAPACITY * cls.GROWTH_FACTOR ** index
        probability = cls.TARGET_FALSE_POSITIVE_RATE / (2 ** (index + 1))
        return _BloomLayer(capacity, probability)

    def _load(self):
        if not self.path.exists():
            return [self._new_layer(0)]
        try:
            content = self.path.read_bytes()
            if any(content.startswith(magic) for magic in self.LEGACY_MAGICS):
                # Re-index completed run artifacts when upgrading a Bloom cache.
                self.needs_rebuild = True
                return [self._new_layer(0)]
            if len(content) < self.FILE_HEADER.size:
                raise BacktestCacheError("Bloom cache header is incomplete")
            magic, payload_size, checksum = self.FILE_HEADER.unpack_from(content)
            payload = content[self.FILE_HEADER.size:]
            if magic != self.FILE_MAGIC:
                raise BacktestCacheError("Bloom cache file has an unsupported format")
            if payload_size != len(payload):
                raise BacktestCacheError("Bloom cache payload length is invalid")
            if hashlib.sha256(payload).digest() != checksum:
                raise BacktestCacheError("Bloom cache checksum is invalid")
            if len(payload) < self.LAYER_COUNT.size:
                raise BacktestCacheError("Bloom cache layer list is incomplete")
            layer_count, = self.LAYER_COUNT.unpack_from(payload)
            if not 1 <= layer_count <= 64:
                raise BacktestCacheError("Bloom cache layer count is invalid")
            offset = self.LAYER_COUNT.size
            layers = []
            for _ in range(layer_count):
                if offset + _BloomLayer.HEADER.size > len(payload):
                    raise BacktestCacheError("Bloom cache layer header is incomplete")
                capacity, count, probability, bit_count, hash_count = (
                    _BloomLayer.HEADER.unpack_from(payload, offset))
                offset += _BloomLayer.HEADER.size
                byte_count = (bit_count + 7) // 8
                if byte_count > len(payload) - offset:
                    raise BacktestCacheError("Bloom cache layer bits are incomplete")
                bits = payload[offset:offset + byte_count]
                offset += byte_count
                layers.append(_BloomLayer(
                    capacity, probability, count=count, bit_count=bit_count,
                    hash_count=hash_count, bits=bits))
            if offset != len(payload):
                raise BacktestCacheError("Bloom cache contains trailing data")
            return layers
        except BacktestCacheError:
            raise
        except (OSError, ValueError, OverflowError, struct.error) as exc:
            raise BacktestCacheError(
                f"Bloom cache file could not be read: {self.path}") from exc

    def _save(self, layers):
        payload = bytearray(self.LAYER_COUNT.pack(len(layers)))
        for layer in layers:
            payload.extend(layer.to_bytes())
        payload = bytes(payload)
        header = self.FILE_HEADER.pack(
            self.FILE_MAGIC, len(payload), hashlib.sha256(payload).digest())
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp_path.write_bytes(header + payload)
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise BacktestCacheError(f"Bloom cache file could not be saved: {exc}") from exc

    def _might_contain(self, fingerprint):
        return any(layer.contains(fingerprint) for layer in self._layers)

    def find(self, expression, dataset_id, scope, settings):
        fingerprint = self.fingerprint(expression, dataset_id, scope, settings)
        return True if self._might_contain(fingerprint) else None

    def record(self, expression, dataset_id, scope, settings):
        return bool(self.record_many([{
            "expression": expression, "dataset_id": dataset_id,
            "scope": scope, "settings": settings,
        }])[0])

    def record_many(self, items):
        fingerprints = [self.fingerprint(
            item.get("expression"), item.get("dataset_id"),
            item.get("scope"), item.get("settings")) for item in items]
        if not fingerprints:
            return []
        layers = [layer.copy() for layer in self._layers]
        found_before = []
        for fingerprint in fingerprints:
            found = any(layer.contains(fingerprint) for layer in layers)
            found_before.append(found)
            if found:
                continue
            if layers[-1].count >= layers[-1].capacity:
                layers.append(self._new_layer(len(layers)))
            layers[-1].add(fingerprint)
        self._save(layers)
        self._layers = layers
        return [not found for found in found_before]

    @staticmethod
    def _alpha_ids(raw, expected_count):
        rows = raw.get("alpha_results") if isinstance(raw, dict) else None
        if isinstance(raw, dict) and raw.get("alpha_id"):
            rows = [raw]
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise BacktestCacheError("closed simulation has an incomplete Alpha ID list")
        alpha_ids = [row.get("alpha_id") for row in rows
                     if isinstance(row, dict) and row.get("alpha_id")]
        if len(alpha_ids) != expected_count or len(set(alpha_ids)) != expected_count:
            raise BacktestCacheError("closed simulation has duplicate or missing Alpha IDs")
        return alpha_ids

    def sync_closed_runs(self, runs_root: Path, settings_resolver):
        """Seed a missing or legacy cache from completed local artifacts."""
        runs_root = Path(runs_root).resolve()
        if not runs_root.exists():
            self._save(self._layers)
            self.needs_rebuild = False
            return 0
        records = []
        try:
            for intent_path in sorted(runs_root.rglob("simulation_intent.json")):
                batch_dir = intent_path.parent
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                    if not isinstance(intent, dict) or intent.get("status") != "READ_BACK":
                        continue
                    prereg_path = batch_dir / "prereg.json"
                    state = json.loads((batch_dir / "state.json").read_text(encoding="utf-8"))
                    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                    if not (batch_dir / "results.json").is_file() or not verify_prereg(
                            prereg_path, state):
                        raise BacktestCacheError(
                            f"closed batch has no valid pre-registration/results: {batch_dir}")
                    variants = prereg.get("variants")
                    alpha_ids = intent.get("alpha_ids")
                    if (not isinstance(variants, list) or not isinstance(alpha_ids, list) or
                            len(variants) != len(alpha_ids)):
                        raise BacktestCacheError(
                            f"closed batch expression/Alpha counts differ: {batch_dir}")
                    scope = self._scope_key(prereg["scope"])
                    settings = (intent.get("settings") or
                                prereg.get("simulation_settings") or
                                settings_resolver(prereg["scope"]))
                    for variant in variants:
                        if not isinstance(variant, dict) or not variant.get("expression"):
                            raise BacktestCacheError(
                                f"closed batch has an unreadable expression: {batch_dir}")
                        records.append({"expression": variant["expression"],
                                        "dataset_id": prereg["dataset_id"],
                                        "scope": scope, "settings": settings})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise BacktestCacheError(
                        f"closed batch could not be indexed: {batch_dir}: {exc}") from exc

            for intent_path in sorted(runs_root.rglob("probe_*_intent.json")):
                batch_dir = intent_path.parent
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                    if not isinstance(intent, dict) or intent.get("status") != "READ_BACK":
                        continue
                    expressions = intent.get("expressions")
                    raw_path = batch_dir / intent_path.name.replace("_intent.json", "_raw.json")
                    raw = json.loads(raw_path.read_text(encoding="utf-8"))
                    if not isinstance(expressions, list) or not expressions:
                        raise BacktestCacheError("closed probe has no expressions")
                    self._alpha_ids(raw, len(expressions))
                    context = json.loads((batch_dir / "batch_context.json").read_text(
                        encoding="utf-8"))
                    for expression in expressions:
                        records.append({"expression": expression,
                                        "dataset_id": context["dataset_id"],
                                        "scope": self._scope_key(context["scope"]),
                                        "settings": intent["settings"]})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise BacktestCacheError(
                        f"closed probe could not be indexed: {batch_dir}: {exc}") from exc
        except BacktestCacheError:
            raise
        added = sum(self.record_many(records)) if records else 0
        if not self.path.exists() or self.needs_rebuild:
            self._save(self._layers)
            self.needs_rebuild = False
        return added
