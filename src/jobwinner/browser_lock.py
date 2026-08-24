"""Per-platform serialization locks with priority for browser/CDP access.

Background
----------
Collect, deliver and monitor all drive the same Chrome instance through the
9222 debug port. When tasks run in parallel (deliver while collecting), a
lock that is shared *per platform* prevents concurrent tab/page mutations on
the SAME platform (which could confuse page state or trip anti-bot signals),
while DIFFERENT platforms (BOSS vs 猎聘 vs 官网) stay fully parallel because
each uses its own Chrome tab/session namespace.

Priorities
----------
Not all browser work is equally sensitive. Sending a greeting / delivering a
resume is the highest-risk action (writes to the site, most likely to trip
filters), so it should never wait long behind a low-risk scrape or monitor
poll. Waiters queue by priority: a higher-priority waiter (smaller number)
jumps ahead of lower-priority waiters that are already waiting.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Deque, Optional


class BrowserPriority:
    """Priority tiers for browser work. Lower number = higher priority."""

    # 发送招呼语 / 发送简历：写操作，最敏感，优先进 Chrome
    DELIVER = 0
    # 采集岗位：读+少量滚动，翻页频率高
    COLLECT = 1
    # 监测 HR 回复：读为主，偶尔发简历（发简历时按发送级处理）
    MONITOR = 2
    # 官网进度巡检：低频后台任务
    PORTAL = 2


class _Waiter:
    __slots__ = ("priority", "cond", "acquired", "order")

    def __init__(self, priority: int, cond: threading.Condition, order: int) -> None:
        self.priority = priority
        self.cond = cond
        self.acquired = False
        self.order = order


class PlatformBrowserLock:
    """Serializes browser access on ONE platform, with priority-based queuing.

    - Same platform: mutually exclusive, waiters queue by priority
      (same priority keeps FIFO order).
    - Different platforms: independent locks, fully parallel.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self._cond = threading.Condition(threading.Lock())
        self._owner: Optional[str] = None
        self._waiters: Deque[_Waiter] = deque()
        self._next_order = 0

    # ── 基本操作 ──────────────────────────────────────────
    def acquire(self, priority: int, timeout: Optional[float] = None) -> bool:
        """Acquire the lock, waiting up to timeout seconds. Returns True if acquired."""
        waiter = _Waiter(priority, self._cond, self._next_order)
        with self._cond:
            self._next_order += 1
            if self._owner is None:
                self._owner = threading.current_thread().name
                return True
            self._insert_waiter(waiter)
            deadline = None if timeout is None else time.monotonic() + timeout
            while not waiter.acquired:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._remove_waiter(waiter)
                        return False
                self._cond.wait(remaining)
            return True

    def release(self) -> None:
        with self._cond:
            self._owner = None
            self._wake_next()

    # ── 上下文管理 ────────────────────────────────────────
    @contextmanager
    def context(self, priority: int, timeout: Optional[float] = None):
        acquired = self.acquire(priority, timeout)
        if not acquired:
            raise TimeoutError(f"等待浏览器锁超时（platform={self.platform}, priority={priority}）")
        try:
            yield
        finally:
            self.release()

    # ── 内部 ──────────────────────────────────────────────
    def _insert_waiter(self, waiter: _Waiter) -> None:
        """Insert waiter so higher priority goes first; same priority keeps FIFO."""
        index = 0
        for existing in self._waiters:
            if existing.priority <= waiter.priority and existing.order < waiter.order:
                index += 1
            elif existing.priority > waiter.priority:
                break
            else:
                index += 1
        self._waiters.insert(index, waiter)

    def _wake_next(self) -> None:
        if not self._waiters:
            return
        next_waiter = self._waiters.popleft()
        next_waiter.acquired = True
        self._owner = threading.current_thread().name
        next_waiter.cond.notify()

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PlatformBrowserLock {self.platform}: owner={self._owner} waiters={len(self._waiters)}>"


# ── 平台锁池 ──────────────────────────────────────────────
_registry_lock = threading.Lock()
_platform_locks: dict[str, PlatformBrowserLock] = {}


def platform_browser_lock(platform: str = "boss") -> PlatformBrowserLock:
    """Return the shared lock for a platform (BOSS / 智联 / 官网 ...).

    Same platform serializes; different platforms run fully in parallel.
    """
    if not platform:
        platform = "boss"
    with _registry_lock:
        lock = _platform_locks.get(platform)
        if lock is None:
            lock = PlatformBrowserLock(platform)
            _platform_locks[platform] = lock
        return lock


# 向后兼容：旧的全局 BROWSER_LOCK 保留为别名，指向 BOSS 平台锁
BROWSER_LOCK = PlatformBrowserLock("boss")
