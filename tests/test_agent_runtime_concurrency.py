"""Offline concurrency contract for shared Agent runtimes and audit records."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alpha_research.agents.router import RoleRoutedModelClient
from alpha_research.agents.antigravity import (
    AntigravityAdapterError, AntigravityModelClient,
)
from alpha_research.integrations.model import ConfiguredModelClient, DecisionRecorder


def role_config(*, shared=False):
    return {
        "provider": "role_router", "default_role": "lead",
        "task_roles": {"first": "lead", "second": "executor"},
        "roles": {
            "lead": {"backend": "lead"},
            "executor": {"backend": "lead" if shared else "executor"},
        },
        "backends": {
            "lead": {"provider": "mock", "responses": {"first": {"ok": True}}},
            "executor": {"provider": "mock", "responses": {"second": {"ok": True}}},
        },
    }


class MetadataBackend:
    def __init__(self, config):
        self.config = config
        self.last_usage = None
        self.last_model = None
        self.last_response_text = None
        self.last_thread_id = None

    def complete_json(self, task, context, *, thread_path=None):
        marker = context["marker"]
        self.last_usage = {"input_tokens": marker, "output_tokens": marker + 1}
        self.last_model = "model-" + str(marker)
        self.last_response_text = "response-" + str(marker)
        self.last_thread_id = "thread-" + str(marker)
        if context.get("fail"):
            raise RuntimeError("synthetic provider error")
        return {"marker": marker}


def reported(router):
    return (router.last_usage, router.last_model,
            router.last_response_text, router.last_thread_id)


class AgentRuntimeConcurrencyTests(unittest.TestCase):
    def test_interleaved_different_backend_calls_preserve_thread_local_receipts(self):
        router = RoleRoutedModelClient(
            role_config(), backend_factory=MetadataBackend)
        first_returned = threading.Event()
        second_returned = threading.Event()

        def first():
            result = router.complete_json("first", {"marker": 1})
            first_returned.set()
            self.assertTrue(second_returned.wait(5))
            return result, reported(router)

        def second():
            self.assertTrue(first_returned.wait(5))
            result = router.complete_json("second", {"marker": 2})
            second_returned.set()
            return result, reported(router)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(first)
            f2 = pool.submit(second)
            a, b = f1.result(timeout=10), f2.result(timeout=10)
        self.assertEqual(a[0], {"marker": 1})
        self.assertEqual(b[0], {"marker": 2})
        self.assertEqual(a[1], (
            {"input_tokens": 1, "output_tokens": 2},
            "model-1", "response-1", "thread-1"))
        self.assertEqual(b[1], (
            {"input_tokens": 2, "output_tokens": 3},
            "model-2", "response-2", "thread-2"))
        self.assertIsNone(router.last_model)  # main thread never called it

    def test_same_backend_is_serialized_across_threads(self):
        entered = threading.Event()
        second_started = threading.Event()
        release = threading.Event()
        active = [0, 0]
        activity_lock = threading.Lock()

        class BlockingBackend(MetadataBackend):
            def complete_json(self, task, context, *, thread_path=None):
                with activity_lock:
                    active[0] += 1
                    active[1] = max(active)
                    if context["marker"] == 1:
                        entered.set()
                try:
                    if context["marker"] == 1:
                        if not release.wait(5):
                            raise RuntimeError("fixture timed out")
                    return super().complete_json(task, context, thread_path=thread_path)
                finally:
                    with activity_lock:
                        active[0] -= 1

        router = RoleRoutedModelClient(
            role_config(shared=True), backend_factory=BlockingBackend)

        def second():
            second_started.set()
            return router.complete_json("second", {"marker": 2})

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(router.complete_json, "first", {"marker": 1})
            try:
                self.assertTrue(entered.wait(5))
                b = pool.submit(second)
                self.assertTrue(second_started.wait(5))
                time.sleep(0.075)  # allow an unsafe second invocation to enter
                self.assertEqual(active, [1, 1])
            finally:
                release.set()
            self.assertEqual(a.result(timeout=10), {"marker": 1})
            self.assertEqual(b.result(timeout=10), {"marker": 2})
        self.assertEqual(active, [0, 1])
        self.assertEqual(len(router.instances), 1)

    def test_unrelated_backends_execute_in_parallel(self):
        inside = threading.Barrier(2, timeout=8)

        class ParallelBackend(MetadataBackend):
            def complete_json(self, task, context, *, thread_path=None):
                inside.wait()  # globally serializing all backends would time out
                return super().complete_json(task, context,
                                             thread_path=thread_path)

        router = RoleRoutedModelClient(
            role_config(), backend_factory=ParallelBackend)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(router.complete_json, "first", {"marker": 1})
            second = pool.submit(router.complete_json, "second", {"marker": 2})
            self.assertEqual(first.result(timeout=12), {"marker": 1})
            self.assertEqual(second.result(timeout=12), {"marker": 2})

    def test_backend_factory_runs_once_under_contention(self):
        construct_count = []
        count_lock = threading.Lock()
        simultaneous = threading.Barrier(5, timeout=8)

        def make_backend(cfg):
            with count_lock:
                construct_count.append(1)
            time.sleep(0.025)
            return MetadataBackend(cfg)

        router = RoleRoutedModelClient(
            role_config(shared=True), backend_factory=make_backend)

        def call(marker):
            simultaneous.wait()
            result = router.complete_json("first", {"marker": marker})
            return result, router.last_model

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(call, i) for i in range(1, 5)]
            simultaneous.wait()
            replies = [f.result(timeout=12) for f in futures]
        self.assertEqual(construct_count, [1])
        self.assertEqual(len(router.instances), 1)
        self.assertEqual(
            sorted(reply[1] for reply in replies),
            ["model-1", "model-2", "model-3", "model-4"])

    def test_failure_metadata_does_not_inherit_other_call_response(self):
        router = RoleRoutedModelClient(
            role_config(shared=True), backend_factory=MetadataBackend)
        failure_finished = threading.Event()
        success_finished = threading.Event()

        def failing():
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                router.complete_json("first", {"marker": 4, "fail": True})
            failure_finished.set()
            self.assertTrue(success_finished.wait(5))
            return reported(router)

        def succeeding():
            self.assertTrue(failure_finished.wait(5))
            result = router.complete_json("second", {"marker": 8})
            success_finished.set()
            return result, reported(router)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fail = pool.submit(failing)
            ok = pool.submit(succeeding)
            previous = fail.result(timeout=10)
            result, current = ok.result(timeout=10)
        self.assertEqual(previous[0]["input_tokens"], 4)
        self.assertEqual(previous[2], "response-4")
        self.assertEqual(result, {"marker": 8})
        self.assertEqual(current[0]["input_tokens"], 8)
        self.assertEqual(current[2], "response-8")

    def test_snapshot_does_not_expose_mutable_usage_to_callers(self):
        router = RoleRoutedModelClient(role_config(), backend_factory=MetadataBackend)
        router.complete_json("first", {"marker": 3})
        usage = router.last_usage
        usage["input_tokens"] = 999
        self.assertEqual(router.last_usage["input_tokens"], 3)

    def test_direct_http_client_metadata_is_invocation_local(self):
        def transport(url, headers, payload):
            marker = payload["context"]["marker"]
            return {
                "result": {"marker": marker},
                "usage": {"input_tokens": marker, "output_tokens": marker + 1},
            }

        client = ConfiguredModelClient(
            {"provider": "json_http", "url": "https://api.example.invalid"},
            transport=transport, environ={})
        first_finished = threading.Event()
        second_finished = threading.Event()

        def first():
            response = client.complete_json("task", {"marker": 9})
            first_finished.set()
            self.assertTrue(second_finished.wait(5))
            return response, client.last_usage

        def second():
            self.assertTrue(first_finished.wait(5))
            response = client.complete_json("task", {"marker": 5})
            second_finished.set()
            return response, client.last_usage

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            b = pool.submit(second)
            old, new = a.result(timeout=10), b.result(timeout=10)
        self.assertEqual(old, (
            {"marker": 9}, {"input_tokens": 9, "output_tokens": 10}))
        self.assertEqual(new, (
            {"marker": 5}, {"input_tokens": 5, "output_tokens": 6}))
        self.assertIsNone(client.last_usage)  # owner thread did not invoke

    def test_direct_antigravity_client_isolates_metadata_and_invalid_call(self):
        class OfflineAntigravity(AntigravityModelClient):
            async def _run(self, prompt, timeout, secret):
                marker = json.loads(prompt)["context"]["marker"]
                return json.dumps({"marker": marker})

        client = OfflineAntigravity(
            {"provider": "antigravity_sdk", "api_key_env": "MOCK_SDK_KEY"},
            environ={"MOCK_SDK_KEY": "fixture-secret"})
        first_finished = threading.Event()
        second_finished = threading.Event()

        def first():
            response = client.complete_json("task", {"marker": 17})
            first_finished.set()
            self.assertTrue(second_finished.wait(5))
            return response, client.last_response_text

        def second():
            self.assertTrue(first_finished.wait(5))
            response = client.complete_json("task", {"marker": 23})
            second_finished.set()
            return response, client.last_response_text

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            b = pool.submit(second)
            old, new = a.result(timeout=10), b.result(timeout=10)
        self.assertEqual(old, ({"marker": 17}, '{"marker": 17}'))
        self.assertEqual(new, ({"marker": 23}, '{"marker": 23}'))

        # The same context must clear successful raw text when a subsequent
        # call is rejected before contacting the provider.
        client.complete_json("task", {"marker": 1})
        self.assertEqual(client.last_response_text, '{"marker": 1}')
        client.environ.clear()
        with self.assertRaises(AntigravityAdapterError):
            client.complete_json("task", {"marker": 2})
        self.assertIsNone(client.last_response_text)
        self.assertIsNone(client.last_model)

    def test_one_recorder_serializes_its_own_session_and_manifest(self):
        entered = threading.Event()
        release = threading.Event()
        next_started = threading.Event()

        class BlockingBackend(MetadataBackend):
            def complete_json(self, task, context, *, thread_path=None):
                if context["marker"] == 1:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("fixture timed out")
                return super().complete_json(task, context,
                                             thread_path=thread_path)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            router = RoleRoutedModelClient(
                role_config(), backend_factory=BlockingBackend)
            recorder = DecisionRecorder(router, root)
            recorder.begin_session(research_id="R1")
            recorder.bind_batch(root / "batch")

            def subsequent():
                next_started.set()
                return recorder.complete_json("second", {"marker": 2})

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(recorder.complete_json, "first", {"marker": 1})
                try:
                    self.assertTrue(entered.wait(5))
                    second = pool.submit(subsequent)
                    self.assertTrue(next_started.wait(5))
                    time.sleep(0.075)
                    self.assertEqual(len(recorder.journal.session_calls), 0)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10), {"marker": 1})
                self.assertEqual(second.result(timeout=10), {"marker": 2})
            self.assertEqual(len(recorder.journal.session_calls), 2)
            audits = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in (root / "logs" / "model_calls").glob("*.record.json")
            ]
            self.assertEqual(len(audits), 2)
            self.assertEqual(
                {r["task"]: (r["token_in"], r["token_out"], r["model"])
                 for r in audits},
                {"first": (1, 2, "model-1"),
                 "second": (2, 3, "model-2")})
            manifest = json.loads(
                (root / "batch" / "model_call_manifest.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["calls"]), 2)


if __name__ == "__main__":
    unittest.main()
