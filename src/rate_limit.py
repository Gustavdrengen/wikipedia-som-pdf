import threading
import time
from collections.abc import Callable


class RequestLimiter:
    def __init__(self, interval: float, minimum_interval: float = 0.1, maximum_interval: float = 10.0) -> None:
        self.interval = max(minimum_interval, interval)
        self.minimum_interval = max(0.01, minimum_interval)
        self.maximum_interval = max(self.interval, maximum_interval)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cooldown_until = 0.0
        self._successes = 0

    def wait(self, interval: float | None = None) -> None:
        with self._lock:
            requested = self.interval if interval is None else max(0.0, interval)
            now = time.monotonic()
            remaining = max(requested - (now - self._last_request), self._cooldown_until - now)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()

    def success(self) -> None:
        with self._lock:
            self._successes += 1
            if self._successes >= 10:
                self.interval = max(self.minimum_interval, self.interval * 0.9)
                self._successes = 0

    def rate_limited(self, retry_after: float | None = None) -> None:
        with self._lock:
            self.interval = min(self.maximum_interval, max(self.interval * 2.0, 1.0))
            self._successes = 0
            cooldown = max(0.0, retry_after or 60.0)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + cooldown)

    def cooldown(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.0, seconds))


GLOBAL_REQUEST_LIMITER = RequestLimiter(1.0, minimum_interval=0.25, maximum_interval=10.0)


def wait_with_progress(seconds: float, message: str, log: Callable[[str], None] = print) -> None:
    """Wait in visible chunks so long server-directed delays do not look hung."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        delay = min(10.0, remaining)
        time.sleep(delay)
        remaining -= delay
        if remaining > 0:
            log(f"  {message}; {remaining:g}s remaining", flush=True)
