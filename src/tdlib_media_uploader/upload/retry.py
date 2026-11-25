"""Bounded policy for a new Album send after definitive delivery failure."""

import math
from dataclasses import dataclass
from random import uniform
from threading import Lock
from time import monotonic


@dataclass(frozen=True, slots=True)
class AlbumRetryPolicy:
    enabled: bool = False
    max_retries: int = 2
    delay_seconds: float = 1.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Album 自动重试开关必须为布尔值")
        if type(self.max_retries) is not int or not 1 <= self.max_retries <= 10:
            raise ValueError("Album 额外重试次数必须在 1–10 之间")
        if not math.isfinite(self.delay_seconds) or not 0 <= self.delay_seconds <= 3600:
            raise ValueError("Album 重试间隔必须在 0–3600 秒之间")

    @classmethod
    def from_config(cls, config):
        return cls(
            getattr(config, "UPLOAD_RETRY_ENABLED", False),
            getattr(config, "UPLOAD_RETRY_MAX_RETRIES", 2),
            getattr(config, "UPLOAD_RETRY_DELAY_SECONDS", 1.0),
        )

    @property
    def max_attempts(self):
        return 1 + (self.max_retries if self.enabled else 0)


@dataclass(frozen=True, slots=True)
class QueueSendPolicy:
    error_policy: str
    pause_on_unknown: bool
    min_interval: float
    flood_jitter: float
    flood_max_retries: int = 1

    @classmethod
    def from_config(cls, config):
        return cls(
            getattr(config, "UPLOAD_QUEUE_ERROR_POLICY", "pause_on_error"),
            getattr(config, "UPLOAD_PAUSE_ON_UNKNOWN", True),
            getattr(config, "UPLOAD_MIN_SEND_INTERVAL", 0.0),
            getattr(config, "UPLOAD_FLOOD_JITTER", 0.25),
            getattr(config, "UPLOAD_FLOOD_MAX_RETRIES", 1),
        )

    def pauses_for(self, *, unknown=False):
        return self.error_policy == "pause_on_error" or (unknown and self.pause_on_unknown)


class TargetSendGate:
    """Share rate limits across consecutive jobs using the same target."""

    def __init__(self):
        self._lock = Lock()
        self._cooldowns = {}
        self._last_send = -math.inf

    def remaining(self, target, interval):
        with self._lock:
            now = monotonic()
            for expired in tuple(key for key, value in self._cooldowns.items() if value <= now):
                del self._cooldowns[expired]
            return max(0.0, self._last_send + interval - now, self._cooldowns.get(target, now) - now)

    def mark_send(self):
        with self._lock:
            self._last_send = monotonic()

    def cool_down(self, target, seconds, jitter):
        with self._lock:
            self._cooldowns[target] = max(
                self._cooldowns.get(target, 0.0), monotonic() + seconds + uniform(0, jitter),
            )


target_send_gate = TargetSendGate()
