"""Circuit breaker for the model provider (requirement A5).

closed     -> calls flow; consecutive failures are counted
open       -> after FAILURE_THRESHOLD consecutive failures; calls are refused
              immediately (no waiting on a dead provider) for OPEN_SECONDS
half_open  -> after OPEN_SECONDS one probe call is allowed; success closes
              the breaker, failure re-opens it
"""
from __future__ import annotations

import threading
import time

STATE_CODES = {"closed": 0, "half_open": 1, "open": 2}


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, open_seconds: float = 20.0):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.opens_total = 0
        self._probe_in_flight = False
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.opened_at >= self.open_seconds:
                    self.state = "half_open"
                    self._probe_in_flight = False
                else:
                    return False
            # half open: exactly one probe at a time
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self.state = "closed"
            self.failures = 0
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                if self.state != "open":
                    self.opens_total += 1
                self.state = "open"
                self.opened_at = time.time()
                self._probe_in_flight = False

    def reset(self) -> None:
        with self._lock:
            self.state, self.failures, self._probe_in_flight = "closed", 0, False

    def retry_after(self) -> float:
        if self.state != "open":
            return 0.0
        return max(0.0, self.open_seconds - (time.time() - self.opened_at))

    def snapshot(self) -> dict:
        return {"state": self.state, "consecutive_failures": self.failures, "opens_total": self.opens_total,
                "retry_after_s": round(self.retry_after(), 1)}
