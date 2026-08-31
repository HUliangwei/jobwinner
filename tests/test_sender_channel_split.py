# -*- coding: utf-8 -*-
"""双渠道（BOSS / 智联）独立风控分控：

- 日配额按渠道分别计算（history 按 channel 计数）
- 发送间隔 / 渐进退避 per-channel 独立实例
- 风控信号(rate_limit 等)只暂停对应渠道，另一渠道继续
"""
import tempfile
import unittest


def _cleanup(tmp) -> None:
    """Windows 上 sqlite WAL 句柄可能延迟释放，清理失败可容忍。"""
    try:
        tmp.cleanup()
    except OSError:
        pass
from pathlib import Path
from unittest.mock import patch

from jobwinner.db import add_history, get_db, insert_job, update_job_greeting, update_job_status
from jobwinner.executor.sender import _merge_channel_throttle, send_greetings


def _job(job_id: str, channel: str = "bosszp") -> dict:
    return {
        "id": job_id,
        "title": "Engineer",
        "company": "C-" + channel,
        "salary": "10-20K",
        "city": "Beijing",
        "experience": "1-3 years",
        "jd": "Build product features",
        "hr_name": "HR",
        "hr_title": "Recruiter",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
        "channel": channel,
    }


class TestMergeChannelThrottle(unittest.TestCase):
    def test_no_override_falls_back_to_global(self):
        merged = _merge_channel_throttle("bosszp", {"daily_limit": 20, "interval_min": 60, "interval_max": 180})
        self.assertEqual(merged["daily_limit"], 20)
        self.assertEqual(merged["interval_min"], 60)

    def test_override_wins(self):
        tc = {
            "daily_limit": 20,
            "interval_min": 60,
            "interval_max": 180,
            "channel_overrides": {"zhaopin": {"daily_limit": 8, "interval_min": 45, "interval_max": 90}},
        }
        merged = _merge_channel_throttle("zhaopin", tc)
        self.assertEqual(merged["daily_limit"], 8)
        self.assertEqual(merged["interval_min"], 45)
        self.assertEqual(merged["interval_max"], 90)

    def test_other_channel_uses_global(self):
        tc = {"daily_limit": 20, "channel_overrides": {"zhaopin": {"daily_limit": 8}}}
        self.assertEqual(_merge_channel_throttle("bosszp", tc)["daily_limit"], 20)

    def test_channel_overrides_key_not_leaked_into_result(self):
        tc = {"daily_limit": 20, "channel_overrides": {"zhaopin": {"daily_limit": 8}}}
        merged = _merge_channel_throttle("zhaopin", tc)
        self.assertNotIn("channel_overrides", merged)


class TestSendChannelSplit(unittest.TestCase):
    """端到端：双渠道发送时配额/间隔/风控互相独立。"""

    def _seed(self, jobs):
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "jobwinner.db"
        db = get_db(db_path)
        try:
            for job in jobs:
                insert_job(db, job)
                update_job_status(db, job["id"], "ready")
                update_job_greeting(db, job["id"], "您好，我对 " + job["id"] + " 很感兴趣。")
        finally:
            db.close()
        return tmp, db_path

    def _config(self, jobs, **kw):
        cfg = {
            "_workbench_job_ids": [job["id"] for job in jobs],
            "throttle": {
                "daily_limit": kw.get("daily_limit", 20),
                "interval_min": 0,
                "interval_max": 0,
            },
        }
        if kw.get("channel_overrides"):
            cfg["throttle"]["channel_overrides"] = kw["channel_overrides"]
        return cfg

    def test_channel_quotas_are_independent(self):
        jobs = [_job("b1", "bosszp"), _job("b2", "bosszp"), _job("z1", "zhaopin"), _job("z2", "zhaopin")]
        tmp, db_path = self._seed(jobs)
        config = self._config(
            jobs,
            daily_limit=2,
            channel_overrides={"zhaopin": {"daily_limit": 1}},
        )
        with patch("jobwinner.db.DB_PATH", db_path), \
             patch("jobwinner.executor.sender.should_take_day_off", return_value=False), \
             patch("jobwinner.executor.sender.SendWindowChecker.is_active", return_value=True), \
             patch("jobwinner.executor.sender._send_greeting_once", return_value=({"success": True}, None)) as boss_send, \
             patch("jobwinner.executor.sender._send_zhaopin_greeting_once", return_value=({"success": True, "greeting_sent": True}, None)) as zp_send:
            sent = send_greetings(config, force=True)
        report = config["_workbench_send_report"]
        _cleanup(tmp)

        # BOSS 配额 2 → 2 个都发；智联配额 1 → 只发 1 个，另 1 个 deferred
        self.assertEqual(sent, 3)
        self.assertEqual(boss_send.call_count, 2)
        self.assertEqual(zp_send.call_count, 1)
        self.assertEqual(report["scheduled_count"], 3)
        self.assertEqual(report["quota_deferred_count"], 1)
        self.assertEqual(report["stop_reason"], "daily_limit")

    def test_risk_signal_pauses_only_that_channel(self):
        jobs = [_job("z1", "zhaopin"), _job("z2", "zhaopin"), _job("b1", "bosszp")]
        tmp, db_path = self._seed(jobs)
        config = self._config(jobs, daily_limit=10)

        risk = {"success": False, "error": "rate_limit", "history_detail": "风控"}
        with patch("jobwinner.db.DB_PATH", db_path), \
             patch("jobwinner.executor.sender.should_take_day_off", return_value=False), \
             patch("jobwinner.executor.sender.SendWindowChecker.is_active", return_value=True), \
             patch("jobwinner.executor.sender._send_greeting_once", return_value=({"success": True}, None)) as boss_send, \
             patch("jobwinner.executor.sender._send_zhaopin_greeting_once", side_effect=[(risk, None), ({"success": True, "greeting_sent": True}, None)]) as zp_send:
            sent = send_greetings(config, force=True)
        _cleanup(tmp)

        # z1 触发风控 → zhaopin 渠道暂停（z2 跳过），BOSS 渠道照常发送
        self.assertEqual(zp_send.call_count, 1)
        self.assertEqual(boss_send.call_count, 1)
        self.assertEqual(sent, 1)

    def test_todays_history_counts_per_channel(self):
        # zhaopin 今天已 sent 1 条（补历史），限额 1 → 新 job 该渠道 deferred
        jobs = [_job("z_old", "zhaopin"), _job("z_new", "zhaopin"), _job("b1", "bosszp")]
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "jobwinner.db"
        db = get_db(db_path)
        try:
            for job in jobs:
                insert_job(db, job)
                update_job_status(db, job["id"], "ready")
                update_job_greeting(db, job["id"], "您好")
            # 今天的智联历史发送记录
            add_history(db, "z_old", "sent", "已经发过")
        finally:
            db.close()

        config = self._config(
            [jobs[1], jobs[2]],
            daily_limit=10,
            channel_overrides={"zhaopin": {"daily_limit": 1}},
        )
        with patch("jobwinner.db.DB_PATH", db_path), \
             patch("jobwinner.executor.sender.should_take_day_off", return_value=False), \
             patch("jobwinner.executor.sender.SendWindowChecker.is_active", return_value=True), \
             patch("jobwinner.executor.sender._send_greeting_once", return_value=({"success": True}, None)) as boss_send, \
             patch("jobwinner.executor.sender._send_zhaopin_greeting_once", return_value=({"success": True, "greeting_sent": True}, None)) as zp_send:
            sent = send_greetings(config, force=True)
        report = config["_workbench_send_report"]
        _cleanup(tmp)

        # 智联今天已满 → z_new deferred；BOSS b1 照发
        self.assertEqual(zp_send.call_count, 0)
        self.assertEqual(boss_send.call_count, 1)
        self.assertEqual(sent, 1)
        self.assertEqual(report["quota_deferred_count"], 1)


if __name__ == "__main__":
    unittest.main()