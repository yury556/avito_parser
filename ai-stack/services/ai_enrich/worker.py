"""Фоновый последовательный worker для существующего batch_process."""
from __future__ import annotations

import random
import threading
from datetime import datetime, timezone
from typing import Any, Callable


class AIEnrichWorker:
    """Периодически вызывает batch handler и сериализует ручные вызовы /batch."""

    def __init__(
        self,
        batch_handler: Callable[[Any], Any],
        default_request: Any,
        *,
        enabled: bool = False,
        idle_sleep: float = 10.0,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        random_func: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._batch_handler = batch_handler
        self._default_request = default_request
        self.enabled = enabled
        self.idle_sleep = max(1.0, idle_sleep)
        self.backoff_base = max(0.1, backoff_base)
        self.backoff_max = max(self.backoff_base, backoff_max)
        self._random = random_func
        self._batch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_at: str | None = None
        self._last_successful_batch_at: str | None = None
        self._last_batch_finished_at: str | None = None
        self._last_error: str | None = None
        self._last_backoff_seconds: float | None = None
        self._consecutive_failures = 0
        self._last_batch_processed = 0
        self._last_batch_errors = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def run_batch(self, request: Any = None) -> Any:
        """Один batch в процессе: общий замок для worker и HTTP /batch."""
        req = self._default_request if request is None else request
        with self._batch_lock:
            return self._batch_handler(req)

    @staticmethod
    def _result_value(result: Any, name: str) -> int:
        value = result.get(name, 0) if isinstance(result, dict) else getattr(result, name, 0)
        return int(value or 0)

    def run_once(self) -> float:
        """Выполнить один batch и вернуть задержку перед следующим."""
        with self._state_lock:
            self._heartbeat_at = self._now()

        try:
            result = self.run_batch()
            processed = self._result_value(result, "processed")
            errors = self._result_value(result, "errors")
            with self._state_lock:
                self._last_batch_processed = processed
                self._last_batch_errors = errors
            if errors:
                raise RuntimeError(f"batch completed with {errors} item errors")

            with self._state_lock:
                self._last_successful_batch_at = self._now()
                self._last_batch_finished_at = self._last_successful_batch_at
                self._last_error = None
                self._last_backoff_seconds = None
                self._consecutive_failures = 0
            if processed:
                print(f"ai_worker: batch processed={processed}, errors=0", flush=True)
            return self.idle_sleep
        except Exception as exc:
            with self._state_lock:
                self._consecutive_failures += 1
                failures = self._consecutive_failures
                self._last_batch_finished_at = self._now()
                self._last_error = " ".join(str(exc).split())[:500]
                cap = min(self.backoff_max, self.backoff_base * (2 ** min(failures - 1, 10)))
                delay = self._random(cap / 2, cap)
                self._last_backoff_seconds = delay
            print(
                f"ai_worker: batch failed ({self._last_error}); retry in {delay:.1f}s",
                flush=True,
            )
            return delay

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            delay = self.run_once()
            self._stop_event.wait(delay)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="ai-enrich-worker",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "enabled": self.enabled,
                "running": self._thread is not None and self._thread.is_alive(),
                "idle_sleep_seconds": self.idle_sleep,
                "heartbeat_at": self._heartbeat_at,
                "last_successful_batch_at": self._last_successful_batch_at,
                "last_batch_finished_at": self._last_batch_finished_at,
                "last_batch_processed": self._last_batch_processed,
                "last_batch_errors": self._last_batch_errors,
                "consecutive_failures": self._consecutive_failures,
                "last_backoff_seconds": self._last_backoff_seconds,
                "last_error": self._last_error,
            }
