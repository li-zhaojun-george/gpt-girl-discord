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
    daily_calls: int = -1
    social_calls: int = -1
    social_gap: int = 30
    conversation_window: int = 180
    debounce_seconds: float = 1.5
    aliases: tuple[str, ...] = ("GPT娘", "大白龙", "龙娘", "小龙")
    quiet_start: int = 23
    quiet_end: int = 8
    interest_channels: set[int] = field(default_factory=set)
    interest_interval: int = 90
    interest_idle: int = 120

    def __post_init__(self):
        if not self.allowed or not self.observed <= self.allowed:
            raise ValueError("频道白名单不能为空；观察频道必须属于白名单")
        if self.mode not in {"mention", "observe", "social"}:
            raise ValueError("BOT_MODE 必须是 mention / observe / social")
        if self.backend not in {"codex", "openai"}:
            raise ValueError("MODEL_BACKEND 必须是 codex / openai")
        if (self.daily_calls < -1 or self.social_calls < -1 or
                (self.daily_calls >= 0 and (self.social_calls == -1 or self.social_calls > self.daily_calls))):
            raise ValueError("调用次数用-1表示不设本地日上限，0表示禁用；有限总额下社交次数不得超过总额")
        if self.social_gap < 5 or not all(0 <= h <= 23 for h in (self.quiet_start, self.quiet_end)):
            raise ValueError("无关插话冷却至少 5 秒；安静时段使用 0–23 小时")
        if not 15 <= self.conversation_window <= 900 or not 0 <= self.debounce_seconds <= 5:
            raise ValueError("接续窗口15–900秒，消息合并等待0–5秒")
        if not self.aliases or any(not a.strip() or len(a) > 32 for a in self.aliases):
            raise ValueError("昵称不能为空且长度不得超过32字符")
        ZoneInfo(self.timezone)
        if self.interest_channels and (self.mode != "social" or self.backend != "codex" or
                                       not self.interest_channels <= self.observed):
            raise ValueError("兴趣分享需要Codex/social，且仅限观察频道")
        if not 15 <= self.interest_interval <= 1440 or not 30 <= self.interest_idle <= 3600:
            raise ValueError("兴趣巡游间隔15–1440分钟，聊天让路30–3600秒")

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
                   daily_calls=int(os.getenv("DAILY_MODEL_CALLS", "-1")),
                   social_calls=int(os.getenv("DAILY_SOCIAL_CALLS", "-1")),
                   social_gap=int(os.getenv("SOCIAL_COOLDOWN_SECONDS", "30")),
                   conversation_window=int(os.getenv("CONVERSATION_WINDOW_SECONDS", "180")),
                   debounce_seconds=float(os.getenv("MESSAGE_DEBOUNCE_SECONDS", "1.5")),
                   aliases=tuple(a.strip() for a in os.getenv("BOT_ALIASES", "GPT娘,大白龙,龙娘,小龙").split(",") if a.strip()),
                   quiet_start=int(os.getenv("QUIET_START_HOUR", "23")),
                   quiet_end=int(os.getenv("QUIET_END_HOUR", "8")),
                   interest_channels=ids("INTEREST_CHANNEL_IDS"),
                   interest_interval=int(os.getenv("INTEREST_INTERVAL_MINUTES", "90")),
                   interest_idle=int(os.getenv("INTEREST_IDLE_SECONDS", "120")))

