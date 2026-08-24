"""Throttle module - Gaussian delay + burst penalty for anti-detection."""

import random
import time
from collections import deque
from datetime import datetime
from threading import Event, Lock


class GlobalRequestGate:
    """进程级共享的请求节奏闸门。

    并行任务（发送/采集/监测）各自有自己的节流器基准节奏，但如果它们各自
    记录自己的请求历史，从平台服务器（如 BOSS）的视角看请求会叠加翻倍，
    爆发检测（burst detection）完全失效，触发风控的风险大幅上升。

    这个闸门让所有任务共享同一个「近期请求时间戳」历史：
    - 任何真实请求发生后调用 GlobalRequestGate.mark()；
    - 任何任务发起下一次请求前，wait() 都叠加 burst_penalty()
      （基于全局历史，而非只看本任务的局部节奏）。

    效果：即使并行，整体请求节奏仍被约束在「一个人慢慢操作」的水平。
    各任务仍保留自己的延时基准（发送 60-180s / 采集 2-5s），不会互相拖慢。
    """

    _lock = Lock()
    _recent_times: deque = deque(maxlen=24)
    _last_request_time = 0.0

    @classmethod
    def mark(cls) -> None:
        """记录一次已发生的真实请求（发送成功/失败、打开搜索页、打开详情页）。"""
        with cls._lock:
            now = time.time()
            cls._last_request_time = now
            cls._recent_times.append(now)

    @classmethod
    def burst_penalty(cls) -> float:
        """基于全局历史的爆发惩罚，供各任务 wait() 叠加。"""
        with cls._lock:
            if not cls._recent_times:
                return 0.0
            now = time.time()
            recent_15s = sum(1 for ts in cls._recent_times if now - ts <= 15)
            recent_45s = sum(1 for ts in cls._recent_times if now - ts <= 45)
        # 阈值比局部节流略宽（全局里采集翻页也会计入），惩罚也相应温和，
        # 目的是「整体节奏不翻倍」，而不是把每个任务都卡死。
        if recent_45s >= 10:
            return random.uniform(4.0, 7.0)
        if recent_45s >= 7:
            return random.uniform(2.0, 4.0)
        if recent_15s >= 3:
            return random.uniform(0.8, 2.0)
        return 0.0


class RequestThrottle:
    """Rate limiter with Gaussian-distributed delays and burst detection."""

    def __init__(self, delay_min: float = 60.0, delay_max: float = 180.0) -> None:
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._last_request_time = 0.0
        self._recent_times: deque = deque(maxlen=12)

    def wait(self, stop_event: Event | None = None) -> bool:
        """Block until it's safe to send the next request."""
        elapsed = time.time() - self._last_request_time
        mean = (self._delay_min + self._delay_max) / 2
        std = (self._delay_max - self._delay_min) / 4
        base_sleep = max(0, random.gauss(mean, std) - elapsed)

        # 5% chance of a longer pause — mimics human hesitation
        if random.random() < 0.05:
            base_sleep += random.uniform(2.0, 5.0)

        burst = self._burst_penalty()
        global_penalty = GlobalRequestGate.burst_penalty()
        total = max(0, base_sleep + burst + global_penalty)
        if total > 0:
            if stop_event and stop_event.wait(total):
                return True
            if not stop_event:
                time.sleep(total)
        return bool(stop_event and stop_event.is_set())

    def mark(self) -> None:
        """Record that a request was just sent (also mirrored to the global gate)."""
        now = time.time()
        self._last_request_time = now
        self._recent_times.append(now)
        GlobalRequestGate.mark()

    def _burst_penalty(self) -> float:
        """Extra delay when requests arrive in bursts (local view)."""
        if not self._recent_times:
            return 0.0
        now = time.time()
        recent_15s = sum(1 for ts in self._recent_times if now - ts <= 15)
        recent_45s = sum(1 for ts in self._recent_times if now - ts <= 45)
        if recent_45s >= 6:
            return random.uniform(4.0, 7.0)
        if recent_15s >= 3:
            return random.uniform(1.2, 2.8)
        return 0.0


class PageThrottle:
    """Lighter throttle for page navigation (scraping)."""

    def __init__(self, delay_min: float = 2.0, delay_max: float = 5.0) -> None:
        self._delay_min = delay_min
        self._delay_max = delay_max

    def wait(self, stop_event: Event | None = None) -> bool:
        """Wait before opening the next search/detail page.

        Opening a page is itself a real request to the platform, so it is also
        counted in the global gate (mark) and slowed by any global burst penalty.
        """
        # 翻页/开页也算一次真实请求，先计入全局历史
        GlobalRequestGate.mark()
        delay = random.uniform(self._delay_min, self._delay_max)
        delay += GlobalRequestGate.burst_penalty()
        if stop_event:
            return stop_event.wait(delay)
        time.sleep(delay)
        return False


class SendWindowChecker:
    """Checks if current time falls within configured send windows."""

    def __init__(self, windows: list[str]) -> None:
        """Parse windows like ["09:00-12:00", "14:00-16:00"]."""
        self._windows: list[tuple[int, int, int, int]] = []
        for w in windows:
            parts = w.strip().split("-")
            if len(parts) != 2:
                continue
            start_h, start_m = self._parse_time(parts[0])
            end_h, end_m = self._parse_time(parts[1])
            if start_h >= 0 and end_h >= 0:
                self._windows.append((start_h, start_m, end_h, end_m))

    def is_active(self) -> bool:
        """Check if current local time is within any send window."""
        if not self._windows:
            return True  # No windows configured = always active
        cur_minutes = self._current_minutes()
        for start_h, start_m, end_h, end_m in self._windows:
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            if start_minutes <= cur_minutes < end_minutes:
                return True
        return False

    def has_valid_windows(self) -> bool:
        """Return whether at least one configured window parsed successfully."""
        return bool(self._windows)

    def latest_end_time_reached(self) -> bool:
        """Return whether local time is past the latest configured window end."""
        if not self._windows:
            return False
        latest_end = max(end_h * 60 + end_m for _, _, end_h, end_m in self._windows)
        return self._current_minutes() >= latest_end

    def latest_end_datetime(self, now: datetime | None = None) -> datetime | None:
        """Return today's latest configured window end in local time."""
        if not self._windows:
            return None
        current = now or datetime.now()
        latest_end = max(end_h * 60 + end_m for _, _, end_h, end_m in self._windows)
        end_hour, end_minute = divmod(latest_end, 60)
        return current.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

    def next_window_info(self) -> str:
        """Describe when next window opens."""
        if not self._windows:
            return "无窗口限制"
        cur_minutes = self._current_minutes()
        for start_h, start_m, end_h, end_m in sorted(self._windows):
            start_minutes = start_h * 60 + start_m
            if cur_minutes < start_minutes:
                return f"下个窗口: {start_h:02d}:{start_m:02d}"
        # All windows have passed today
        if self._windows:
            first = self._windows[0]
            return f"今日窗口已过，明日 {first[0]:02d}:{first[1]:02d} 开始"
        return ""

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int]:
        """Return (hour, minute) for a 'HH:MM' string, or (-1,-1) when invalid."""
        parts = str(value).strip().split(":")
        if len(parts) == 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
                if hour == 24 and minute == 0:
                    # "24:00" means end of day; treat as 23:59 so window stays active
                    return 23, 59
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour, minute
            except ValueError:
                pass
        return -1, -1

    @staticmethod
    def _current_minutes() -> int:
        now = datetime.now()
        return now.hour * 60 + now.minute


class ProgressiveBackoff:
    """Progressive error-based backoff for send failures."""

    def __init__(self) -> None:
        self._consecutive_errors = 0

    def record_error(self) -> float:
        """Record an error and return recommended pause duration in seconds."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= 3:
            return 1800.0  # 30 minutes
        elif self._consecutive_errors == 2:
            return 120.0
        else:
            return 60.0

    def record_success(self) -> None:
        """Reset error counter on success."""
        self._consecutive_errors = 0

    @property
    def should_pause_long(self) -> bool:
        """Whether we've hit too many errors and should stop."""
        return self._consecutive_errors >= 3


def should_take_day_off(probability: float = 0.05) -> bool:
    """Random chance to skip a day entirely (anti-pattern detection)."""
    return random.random() < probability
