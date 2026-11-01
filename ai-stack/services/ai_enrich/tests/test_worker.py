import threading
import time
import unittest

from worker import AIEnrichWorker


class AIEnrichWorkerTests(unittest.TestCase):
    def test_batch_calls_are_serialized_for_same_avito_id(self):
        active = 0
        max_active = 0
        calls = []
        state_lock = threading.Lock()
        start_barrier = threading.Barrier(3)

        def handler(avito_id):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                calls.append(avito_id)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return {"processed": 1, "errors": 0}

        worker = AIEnrichWorker(handler, 123)
        threads = [
            threading.Thread(target=lambda: (start_barrier.wait(), worker.run_batch(123)))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        start_barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active, 1)
        self.assertEqual(calls, [123, 123])

    def test_empty_queue_is_successful_and_uses_idle_delay(self):
        worker = AIEnrichWorker(
            lambda _: {"processed": 0, "errors": 0},
            object(),
            idle_sleep=10,
        )
        delay = worker.run_once()
        status = worker.status()
        self.assertEqual(delay, 10)
        self.assertIsNotNone(status["last_successful_batch_at"])
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIsNone(status["last_error"])

    def test_openrouter_failure_does_not_stop_worker(self):
        calls = 0

        def handler(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("OpenRouter unavailable")
            return {"processed": 1, "errors": 0}

        worker = AIEnrichWorker(
            handler, object(), idle_sleep=10, random_func=lambda low, high: high
        )
        retry_delay = worker.run_once()
        self.assertEqual(retry_delay, 2)
        self.assertIsNone(worker.status()["last_successful_batch_at"])
        self.assertEqual(worker.run_once(), 10)
        self.assertEqual(calls, 2)
        self.assertIsNotNone(worker.status()["last_successful_batch_at"])
        self.assertIsNone(worker.status()["last_error"])

    def test_item_errors_use_bounded_exponential_backoff(self):
        worker = AIEnrichWorker(
            lambda _: {"processed": 0, "errors": 1},
            object(),
            backoff_base=2,
            backoff_max=6,
            random_func=lambda low, high: high,
        )
        delays = [worker.run_once() for _ in range(5)]
        self.assertEqual(delays, [2, 4, 6, 6, 6])
        self.assertEqual(worker.status()["consecutive_failures"], 5)

    def test_restart_resumes_without_persistent_offset(self):
        calls = 0

        def handler(_):
            nonlocal calls
            calls += 1
            return {"processed": 0, "errors": 0}

        worker = AIEnrichWorker(handler, object(), enabled=True, idle_sleep=60)
        for expected_calls in (1, 2):
            self.assertTrue(worker.start())
            deadline = time.monotonic() + 1
            while calls < expected_calls and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()
            self.assertEqual(calls, expected_calls)
            self.assertFalse(worker.status()["running"])

    def test_heartbeat_is_not_successful_batch_evidence(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(_):
            entered.set()
            release.wait(1)
            return {"processed": 1, "errors": 0}

        worker = AIEnrichWorker(handler, object(), enabled=True, idle_sleep=60)
        worker.start()
        self.assertTrue(entered.wait(1))
        status = worker.status()
        self.assertIsNotNone(status["heartbeat_at"])
        self.assertIsNone(status["last_successful_batch_at"])
        release.set()
        worker.stop()

    def test_start_is_idempotent(self):
        worker = AIEnrichWorker(
            lambda _: {"processed": 0, "errors": 0},
            object(),
            enabled=True,
            idle_sleep=60,
        )
        self.assertTrue(worker.start())
        first_thread = worker._thread
        self.assertFalse(worker.start())
        self.assertIs(worker._thread, first_thread)
        worker.stop()


if __name__ == "__main__":
    unittest.main()
