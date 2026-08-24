"""Tests for the global request gate shared across parallel tasks."""

import time
import unittest
from unittest.mock import patch

from jobwinner.throttle import GlobalRequestGate, PageThrottle, RequestThrottle


class GlobalRequestGateTests(unittest.TestCase):
    def setUp(self):
        # Reset global state so tests are isolated.
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
            GlobalRequestGate._last_request_time = 0.0

    def test_empty_gate_has_no_penalty(self):
        self.assertEqual(GlobalRequestGate.burst_penalty(), 0.0)

    def test_mark_records_into_global_history(self):
        GlobalRequestGate.mark()
        with GlobalRequestGate._lock:
            self.assertEqual(len(GlobalRequestGate._recent_times), 1)

    def test_penalty_grows_with_global_burst(self):
        now = time.time()
        with GlobalRequestGate._lock:
            # 45s 内 10+ 次 -> 高惩罚档
            GlobalRequestGate._recent_times.clear()
            for i in range(12):
                GlobalRequestGate._recent_times.append(now - i * 2)
        penalty = GlobalRequestGate.burst_penalty()
        self.assertGreaterEqual(penalty, 4.0)

    def test_penalty_heavier_with_more_requests(self):
        now = time.time()
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
            for i in range(4):
                GlobalRequestGate._recent_times.append(now - i * 3)
        low = GlobalRequestGate.burst_penalty()
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
            for i in range(12):
                GlobalRequestGate._recent_times.append(now - i * 1)
        high = GlobalRequestGate.burst_penalty()
        self.assertGreaterEqual(high, low)

    def test_request_throttle_mark_mirrors_to_global_gate(self):
        throttle = RequestThrottle(delay_min=0.01, delay_max=0.02)
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
        throttle.mark()
        with GlobalRequestGate._lock:
            self.assertEqual(len(GlobalRequestGate._recent_times), 1)

    def test_page_throttle_wait_counts_as_global_request(self):
        # PageThrottle.wait() 应该把翻页计入全局历史（它是真实请求）。
        throttle = PageThrottle(delay_min=0.001, delay_max=0.002)
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
        throttle.wait()  # 真实 sleep，但只有 1-2ms
        with GlobalRequestGate._lock:
            self.assertEqual(len(GlobalRequestGate._recent_times), 1)

    def test_parallel_tasks_share_global_penalty(self):
        """两个"任务"各自 mark 后，第三个任务 wait 能看到全局爆发并叠加惩罚。"""
        # 模拟两个任务在 15s 内共发 3+ 次请求
        now = time.time()
        t1 = RequestThrottle(delay_min=0.01, delay_max=0.02)
        t2 = RequestThrottle(delay_min=0.01, delay_max=0.02)
        with GlobalRequestGate._lock:
            GlobalRequestGate._recent_times.clear()
        t1.mark()
        t2.mark()
        t1.mark()
        # 全局 15s 内 >= 3 次 -> 应该有 0.8-2.0 惩罚
        penalty = GlobalRequestGate.burst_penalty()
        self.assertGreaterEqual(penalty, 0.8)


if __name__ == "__main__":
    unittest.main()
