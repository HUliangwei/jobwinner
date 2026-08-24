"""Tests for portal progress check cooldown and page-wait behaviour."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jobwinner.executor import portal_tracker
from jobwinner.executor.portal_tracker import (
    DEFAULT_PORTAL_COOLDOWN_MINUTES,
    check_portal_progress,
)


class PortalCooldownTests(unittest.TestCase):
    def setUp(self):
        # Reset cooldown state so tests are isolated.
        with portal_tracker._COOLDOWN_LOCK:
            portal_tracker._LAST_CHECK_TS = 0.0

    def _make_db(self, tmp: Path, urls: list[str]):
        """Create a temp DB with portal jobs; return the DB connection."""
        from jobwinner.db import get_db, insert_job

        db_path = tmp / "data" / "jobwinner.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = get_db(db_path)
        for i, url in enumerate(urls):
            insert_job(
                db,
                {
                    "id": f"portal-{i}",
                    "title": f"岗位{i}",
                    "company": f"公司{i}",
                    "salary": "",
                    "city": "",
                    "experience": "",
                    "jd": "",
                    "hr_name": "",
                    "hr_title": "",
                    "hr_active": "",
                    "company_size": "",
                    "company_industry": "",
                    "url": url,
                    "source": "portal",
                },
            )
        # insert_job 的 INSERT 不写 source 列（表默认 'boss'），这里显式改成 portal
        db.execute("UPDATE jobs SET source='portal' WHERE id LIKE 'portal-%'")
        db.commit()
        return db, db_path

    @patch("jobwinner.executor.portal_tracker.check_single_record_page")
    def test_first_check_runs_pages(self, mock_check):
        """第一次巡检（无上次记录）应真正执行并打开页面。"""
        mock_check.return_value = {"ok": True, "url": "https://x.com/deliveryRecord", "title": "", "text": "岗位0 简历初筛-未处理"}
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = self._make_db(Path(tmp), ["https://x.com/deliveryRecord"])
            result = check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=30, force=True)
            db.close()
        self.assertEqual(result["checked"], 1)
        mock_check.assert_called_once()

    @patch("jobwinner.executor.portal_tracker.check_single_record_page")
    def test_cooldown_skips_second_call(self, mock_check):
        """刚巡检过，冷却期内第二次非强制调用应跳过（不打开页面）。"""
        mock_check.return_value = {"ok": True, "url": "https://x.com/deliveryRecord", "title": "", "text": "岗位0 简历初筛-未处理"}
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = self._make_db(Path(tmp), ["https://x.com/deliveryRecord"])
            first = check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=30, force=True)
            self.assertEqual(first["checked"], 1)
            second = check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=30)
            self.assertEqual(second["checked"], 0)
            self.assertIn("cooldown_skipped", second["notes"])
            db.close()
        # 只真正打开过一次页面
        self.assertEqual(mock_check.call_count, 1)

    @patch("jobwinner.executor.portal_tracker.check_single_record_page")
    def test_force_bypasses_cooldown(self, mock_check):
        """force=True 时即使冷却期内也应真正执行。"""
        mock_check.return_value = {"ok": True, "url": "https://x.com/deliveryRecord", "title": "", "text": "岗位0 简历初筛-未处理"}
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = self._make_db(Path(tmp), ["https://x.com/deliveryRecord"])
            check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=30, force=True)
            forced = check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=30, force=True)
            self.assertEqual(forced["checked"], 1)
            db.close()
        self.assertEqual(mock_check.call_count, 2)

    @patch("jobwinner.executor.portal_tracker.check_single_record_page")
    def test_zero_cooldown_always_checks(self, mock_check):
        """cooldown_minutes=0 表示不冷却，每次都执行。"""
        mock_check.return_value = {"ok": True, "url": "https://x.com/deliveryRecord", "title": "", "text": "岗位0 简历初筛-未处理"}
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = self._make_db(Path(tmp), ["https://x.com/deliveryRecord"])
            check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=0)
            check_portal_progress(db_conn=db, wait_seconds=0, log=lambda m: None, cooldown_minutes=0)
            db.close()
        self.assertEqual(mock_check.call_count, 2)

    def test_cooldown_helper_respects_window(self):
        """_should_skip_due_to_cooldown 在冷却窗口内外行为正确。"""
        portal_tracker._mark_checked_now()
        self.assertTrue(portal_tracker._should_skip_due_to_cooldown(30))
        # 窗口按分钟计；用负值模拟"很久以前"
        with portal_tracker._COOLDOWN_LOCK:
            portal_tracker._LAST_CHECK_TS = time.time() - 3600  # 1 hour ago
        self.assertFalse(portal_tracker._should_skip_due_to_cooldown(30))
        # 冷却 0 = 不冷却
        with portal_tracker._COOLDOWN_LOCK:
            portal_tracker._LAST_CHECK_TS = time.time()
        self.assertFalse(portal_tracker._should_skip_due_to_cooldown(0))


if __name__ == "__main__":
    unittest.main()
