import json
import unittest
from threading import Event
from unittest.mock import Mock, call, patch

from jobwinner.scraper.jobs import scrape_jobs
from jobwinner.web.server import _execute_collect
from jobwinner.web.tasks import WorkbenchTask


class ScraperBackgroundTests(unittest.TestCase):
    def test_stopped_collection_does_not_open_a_search_page(self):
        db = Mock()
        stop_event = Event()
        stop_event.set()
        config = {
            "profile": {"target_cities": ["北京"], "deal_breakers": []},
            "search": {"max_pages": 1},
            "_workbench_stop_event": stop_event,
        }

        with patch("jobwinner.scraper.jobs.get_db", return_value=db), \
             patch("jobwinner.scraper.jobs.new_tab") as new_tab:
            count = scrape_jobs(config, ["AI"])

        self.assertEqual(count, 0)
        new_tab.assert_not_called()
        db.close.assert_called_once_with()

    def test_workbench_passes_its_stop_event_into_collection(self):
        task = WorkbenchTask(id="task-1", mode="collect", label="单独采集")
        config = {"search": {"keywords": ["AI"]}}

        calls = {"n": 0}

        def scrape_with_progress(collect_config, _keywords, *, limit=None, collected_job_ids=None):
            collect_config["_workbench_collect_progress"]({"seen": 9, "new": 3, "duplicate": 4})
            # The batch pipeline loops until a batch yields no new jobs; return
            # new jobs only on the first call so the loop terminates.
            calls["n"] += 1
            if calls["n"] == 1 and collected_job_ids is not None:
                collected_job_ids.extend(["new-1", "new-2", "new-3"])
                return 3
            return 0

        def score_with_progress(score_config, *, scope="pending", limit=None, job_ids=None, force_rescore=False):
            score_config["_workbench_score_progress"]({
                "completed": 3,
                "total": 3,
                "scored": 2,
                "filtered": 1,
                "failed": 0,
            })
            return (2, 1)

        with patch("jobwinner.scraper.jobs.scrape_jobs", side_effect=scrape_with_progress) as scrape, \
             patch("jobwinner.ai.scorer.score_jobs", side_effect=score_with_progress):
            _execute_collect(task, config)

        collection_config = scrape.call_args.args[0]
        self.assertIs(collection_config["_workbench_stop_event"], task.stop_requested)
        self.assertEqual(task.metrics["collect_seen"], 9)
        self.assertEqual(task.metrics["collect_new"], 3)
        self.assertEqual(task.metrics["collect_duplicate"], 4)
        self.assertEqual(task.metrics["ai_passed"], 2)
        self.assertEqual(task.metrics["ai_filtered"], 1)
        self.assertEqual(task.metrics["ai_failed"], 0)
        self.assertEqual(task.snapshot()["metrics"], task.metrics)

    def test_scraper_reports_seen_new_and_duplicate_counts(self):
        db = Mock()
        progress = Mock()
        progress.add_task.return_value = "task-1"
        progress_context = Mock()
        progress_context.__enter__ = Mock(return_value=progress)
        progress_context.__exit__ = Mock(return_value=False)
        updates = []
        collected_job_ids = []
        jobs = [
            {"title": "Existing", "company": "Example", "salary": "10-15K", "experience": "", "url": "/job_detail/existing.html"},
            {"title": "New", "company": "Example", "salary": "10-15K", "experience": "", "url": "/job_detail/new.html"},
        ]
        detail = {"title": "New", "company": "Example", "salary": "10-15K", "jd": "客户交付"}
        config = {
            "profile": {"target_cities": ["北京"], "deal_breakers": []},
            "search": {"max_pages": 1},
            "_workbench_collect_progress": updates.append,
        }

        with patch("jobwinner.scraper.jobs.get_db", return_value=db), \
             patch("jobwinner.scraper.jobs.Progress", return_value=progress_context), \
             patch("jobwinner.scraper.jobs.PageThrottle") as throttle_cls, \
             patch("jobwinner.scraper.jobs.new_tab", side_effect=["search-target", "detail-target"]), \
             patch("jobwinner.scraper.jobs.evaluate", side_effect=[json.dumps(jobs), json.dumps(detail)]), \
             patch("jobwinner.scraper.jobs.wait_for_load"), \
             patch("jobwinner.scraper.jobs.scroll"), \
             patch("jobwinner.scraper.jobs.close_tab"), \
             patch("jobwinner.scraper.jobs.job_exists", side_effect=[True, False]), \
             patch("jobwinner.scraper.jobs.matching_deal_breaker", return_value=False), \
             patch("jobwinner.scraper.jobs.insert_job"), \
             patch("jobwinner.scraper.jobs.time.sleep"):
            throttle_cls.return_value.wait.return_value = None
            count = scrape_jobs(config, ["AI"], collected_job_ids=collected_job_ids)

        self.assertEqual(count, 1)
        self.assertEqual(len(collected_job_ids), 1)
        self.assertEqual(updates[-1], {"seen": 2, "new": 1, "duplicate": 1})

    def test_search_and_detail_pages_open_in_background(self):
        db = Mock()
        progress = Mock()
        progress.add_task.return_value = "task-1"
        progress_context = Mock()
        progress_context.__enter__ = Mock(return_value=progress)
        progress_context.__exit__ = Mock(return_value=False)

        jobs = [{
            "title": "AI Product Manager",
            "company": "Example",
            "salary": "20-30K",
            "experience": "3-5 years",
            "url": "/job_detail/background-job.html",
        }]
        detail = {
            "title": "AI Product Manager",
            "company": "Example",
            "salary": "20-30K",
            "experience": "3-5 years",
            "jd": "Build AI products",
        }

        config = {
            "profile": {"target_cities": ["北京"], "deal_breakers": []},
            "search": {"max_pages": 1},
        }

        with patch("jobwinner.scraper.jobs.get_db", return_value=db), \
             patch("jobwinner.scraper.jobs.Progress", return_value=progress_context), \
             patch("jobwinner.scraper.jobs.PageThrottle") as throttle_cls, \
             patch(
                 "jobwinner.scraper.jobs.new_tab",
                 side_effect=["search-target", "detail-target"],
             ) as new_tab, \
             patch(
                 "jobwinner.scraper.jobs.evaluate",
                 side_effect=[json.dumps(jobs), json.dumps(detail)],
             ), \
             patch("jobwinner.scraper.jobs.wait_for_load"), \
             patch("jobwinner.scraper.jobs.scroll"), \
             patch("jobwinner.scraper.jobs.close_tab"), \
             patch("jobwinner.scraper.jobs.job_exists", return_value=False), \
             patch("jobwinner.scraper.jobs.matching_deal_breaker", return_value=False), \
             patch("jobwinner.scraper.jobs.insert_job"), \
             patch("jobwinner.scraper.jobs.time.sleep"):
            throttle_cls.return_value.wait.return_value = None
            count = scrape_jobs(config, ["AI"])

        self.assertEqual(count, 1)
        self.assertEqual(
            new_tab.call_args_list,
            [
                call(
                    "https://www.zhipin.com/web/geek/job?query=AI&city=101010100",
                    background=False,
                ),
                call(
                    "https://www.zhipin.com/job_detail/background-job.html",
                    background=True,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
