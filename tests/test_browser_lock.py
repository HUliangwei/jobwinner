"""Tests for per-platform browser locks with priority-based queuing."""

import threading
import time
import unittest

from jobwinner.browser_lock import (
    BROWSER_LOCK,
    BrowserPriority,
    PlatformBrowserLock,
    platform_browser_lock,
)


class PlatformBrowserLockTests(unittest.TestCase):
    def test_same_platform_is_mutually_exclusive(self):
        """Two waiters on the same platform must run serially."""
        lock = platform_browser_lock("boss")
        elapsed = self._run_two(lock, lock, 0.15)
        self.assertGreaterEqual(elapsed, 0.25)  # serial ≈ 2 * hold

    def test_cross_platform_is_parallel(self):
        """Different platforms use independent locks and run concurrently."""
        boss = platform_browser_lock("boss")
        liepin = platform_browser_lock("liepin")
        elapsed = self._run_two(boss, liepin, 0.3)
        self.assertLess(elapsed, 0.5)  # parallel ≈ single hold

    def test_higher_priority_jumps_queue(self):
        """A DELIVER waiter arriving later outranks an already-waiting COLLECT."""
        lock = platform_browser_lock("boss")
        order: list[str] = []

        def low():
            with lock.context(BrowserPriority.COLLECT, timeout=3):
                order.append("collect")
                time.sleep(0.25)

        def high():
            time.sleep(0.05)  # let low acquire first
            with lock.context(BrowserPriority.DELIVER, timeout=3):
                order.append("deliver")

        t1 = threading.Thread(target=low)
        t2 = threading.Thread(target=high)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(order, ["collect", "deliver"])

    def test_priority_fifo_within_same_tier(self):
        """Waiters of the same priority keep FIFO order."""
        lock = platform_browser_lock("boss")
        order: list[str] = []

        def w1():
            with lock.context(BrowserPriority.COLLECT, timeout=3):
                order.append("w1")
                time.sleep(0.1)

        def w2():
            time.sleep(0.05)
            with lock.context(BrowserPriority.COLLECT, timeout=3):
                order.append("w2")

        t1 = threading.Thread(target=w1)
        t2 = threading.Thread(target=w2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(order, ["w1", "w2"])

    def test_acquire_timeout_returns_false(self):
        """Acquiring while another holds and timing out must return False."""
        lock = platform_browser_lock("boss")
        hold = threading.Event()
        release = threading.Event()

        def holder():
            with lock.context(BrowserPriority.MONITOR):
                hold.set()
                release.wait(2)

        t = threading.Thread(target=holder)
        t.start()
        hold.wait(1)
        ok = lock.acquire(BrowserPriority.COLLECT, timeout=0.1)
        self.assertFalse(ok)
        release.set()
        t.join()

    def test_context_manager_acquires_and_releases(self):
        lock = platform_browser_lock("boss")
        with lock.context(BrowserPriority.DELIVER):
            pass
        # After release, another acquire must succeed immediately.
        self.assertTrue(lock.acquire(BrowserPriority.COLLECT, timeout=0.1))
        lock.release()

    def test_legacy_browser_lock_still_works(self):
        """BROWSER_LOCK remains a usable per-platform lock (BOSS)."""
        with BROWSER_LOCK.context(BrowserPriority.DELIVER):
            pass
        self.assertEqual(BROWSER_LOCK.platform, "boss")

    def _run_two(self, lock_a, lock_b, hold: float) -> float:
        start = time.time()

        def worker(lock):
            with lock.context(BrowserPriority.COLLECT):
                time.sleep(hold)

        t1 = threading.Thread(target=worker, args=(lock_a,))
        t2 = threading.Thread(target=worker, args=(lock_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        return time.time() - start


if __name__ == "__main__":
    unittest.main()
