"""Bounded policy for a new Album send after definitive delivery failure."""

import math
from dataclasses import dataclass


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
