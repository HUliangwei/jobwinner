"""Tests for the DB channel column (multi-channel stage B)."""

import unittest


def _full_job(jid: str, title: str, company: str, url: str) -> dict:
    return {
        "id": jid,
        "title": title,
        "company": company,
        "salary": "20-30K",
        "city": "上海",
        "experience": "",
        "jd": "job description",
        "hr_name": "",
        "hr_title": "",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": url,
    }


class TestChannelColumn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        from jobwinner.db import get_db

        tmp = Path(tempfile.mkdtemp()) / "channel-test.db"
        cls.db = get_db(tmp)
        cols = {r[1] for r in cls.db.execute("PRAGMA table_info(jobs)").fetchall()}
        cls.channel_col = "channel" in cols

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_channel_column_exists(self):
        self.assertTrue(self.channel_col)

    def test_insert_default_channel(self):
        from jobwinner.db import insert_job, query_jobs

        insert_job(self.db, _full_job("ch-default", "默认渠道岗位", "公司A", "http://x/1"))
        jobs, total = query_jobs(self.db, job_ids=["ch-default"])
        self.assertEqual(total, 1)
        self.assertEqual(jobs[0]["channel"], "bosszp")

    def test_insert_explicit_channel(self):
        from jobwinner.db import insert_job, query_jobs

        rec = _full_job("ch-explicit", "多渠道路线岗位", "公司B", "http://x/2")
        rec["channel"] = "liepin"
        insert_job(self.db, rec)
        jobs, total = query_jobs(self.db, job_ids=["ch-explicit"])
        self.assertEqual(jobs[0]["channel"], "liepin")

    def test_query_filter_channel(self):
        from jobwinner.db import query_jobs

        jobs, total = query_jobs(self.db, channel="liepin", limit=50)
        self.assertGreaterEqual(total, 1)
        for job in jobs:
            self.assertEqual(job["channel"], "liepin")

        jobs, total = query_jobs(self.db, channel="bosszp", limit=50)
        self.assertGreaterEqual(total, 1)
        for job in jobs:
            self.assertEqual(job["channel"], "bosszp")


if __name__ == "__main__":
    unittest.main()
