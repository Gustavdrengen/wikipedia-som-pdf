import threading
import time


class RequestLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self, interval: float | None = None) -> None:
        with self._lock:
            interval = self.interval if interval is None else max(0.0, interval)
            now = time.monotonic()
            remaining = interval - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
