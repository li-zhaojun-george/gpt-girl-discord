"""Validated operator configuration; channel scope is always explicit."""
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

@dataclass
class Settings:
    allowed: set[int]
    observed: set[int] = field(default_factory=set)
    mode: str = "mention"
    backend: str = "codex"
    timezone: str = "America/Toronto"
    daily_calls: int = 100
    social_calls: int = 12
    social_gap: int = 600
    quiet_start: int = 23
    quiet_end: int = 8

    def __post_init__(self):
        if not self.allowed or not self.observed <= self.allowed:
            raise ValueError("频道白名单不能为空；观察频道必须属于白名单")
        if self.mode not in {"mention", "observe", "social"}:
            raise ValueError("BOT_MODE 必须是 mention / observe / social")
        if self.backend not in {"codex", "openai"}:
            raise ValueError("MODEL_BACKEND 必须是 codex / openai")
        if not (1 <= self.daily_calls <= 10000 and 0 <= self.social_calls <= self.daily_calls):
            raise ValueError("每日调用次数必须在 1–10000，社交次数不得超过总数")
        if self.social_gap < 60 or not all(0 <= h <= 23 for h in (self.quiet_start, self.quiet_end)):
            raise ValueError("社交冷却至少 60 秒；安静时段使用 0–23 小时")
        ZoneInfo(self.timezone)

    def quiet(self, hour):
        start, end = self.quiet_start, self.quiet_end
        return (start <= hour < end) if start < end else (hour >= start or hour < end) if start != end else False

    @classmethod
    def from_env(cls):
        def ids(name):
            return {int(x.strip()) for x in os.getenv(name, "").split(",") if x.strip()}
        return cls(allowed=ids("ALLOWED_CHANNEL_IDS"), observed=ids("OBSERVED_CHANNEL_IDS"),
                   mode=os.getenv("BOT_MODE", "mention"), backend=os.getenv("MODEL_BACKEND", "codex"),
                   timezone=os.getenv("BOT_TIMEZONE", "America/Toronto"),
                   daily_calls=int(os.getenv("DAILY_MODEL_CALLS", "100")),
                   social_calls=int(os.getenv("DAILY_SOCIAL_CALLS", "12")),
                   social_gap=int(os.getenv("SOCIAL_COOLDOWN_SECONDS", "600")),
                   quiet_start=int(os.getenv("QUIET_START_HOUR", "23")),
                   quiet_end=int(os.getenv("QUIET_END_HOUR", "8")))

