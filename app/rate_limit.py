import threading
import time
import uuid
from dataclasses import dataclass


class RateLimitExceeded(Exception):
    def __init__(self, remaining_minutes: int) -> None:
        self.remaining_minutes = remaining_minutes
        super().__init__(
            "You have reached the submission limit. "
            f"Please wait {remaining_minutes} minute"
            f"{'' if remaining_minutes == 1 else 's'} before submitting again."
        )


@dataclass(frozen=True)
class Reservation:
    client_key: str
    token: str


class InquiryRateLimiter:
    def __init__(self, max_submissions: int, window_seconds: int) -> None:
        self._max_submissions = max_submissions
        self._window_seconds = window_seconds
        self._events: dict[str, list[tuple[str, float]]] = {}
        self._lock = threading.Lock()

    def reserve(self, client_key: str) -> Reservation:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            for key, events in list(self._events.items()):
                active_events = [event for event in events if event[1] > cutoff]
                if active_events:
                    self._events[key] = active_events
                else:
                    del self._events[key]
            recent = self._events.get(client_key, [])
            if len(recent) >= self._max_submissions:
                remaining = max(
                    1,
                    int((recent[0][1] + self._window_seconds - now + 59) // 60),
                )
                self._events[client_key] = recent
                raise RateLimitExceeded(remaining)
            token = uuid.uuid4().hex
            recent.append((token, now))
            self._events[client_key] = recent
        return Reservation(client_key=client_key, token=token)

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self._events[reservation.client_key] = [
                event
                for event in self._events.get(reservation.client_key, [])
                if event[0] != reservation.token
            ]
