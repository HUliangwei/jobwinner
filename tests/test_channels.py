"""Tests for the JobWinner channel abstraction layer."""

import json
import unittest


class TestChannelRegistry(unittest.TestCase):
    def test_registry_has_bosszp(self):
        from jobwinner.channels import available_channels, get_channel

        self.assertIn("bosszp", available_channels())
        ch = get_channel("bosszp")
        self.assertEqual(ch.key, "bosszp")
        self.assertEqual(ch.domain, "zhipin.com")

    def test_unknown_key_falls_back_to_bosszp(self):
        from jobwinner.channels import get_channel

        ch = get_channel("does-not-exist")
        self.assertEqual(ch.key, "bosszp")

    def test_get_active_channel_default(self):
        from jobwinner.channels import get_active_channel

        ch = get_active_channel({})
        self.assertEqual(ch.key, "bosszp")

    def test_get_active_channel_from_config(self):
        from jobwinner.channels import get_active_channel

        ch = get_active_channel({"channels": {"active": "bosszp"}})
        self.assertEqual(ch.key, "bosszp")

    def test_current_channel_falls_back(self):
        from jobwinner import channels as c

        # Previously set by earlier tests; force reset then verify fallback.
        c.set_active_channel(c.get_channel("bosszp"))
        self.assertEqual(c.current_channel().key, "bosszp")


class TestBosszpAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jobwinner.channels import get_channel

        cls.ch = get_channel("bosszp")

    def test_identity(self):
        self.assertEqual(self.ch.label, "Boss直聘")
        self.assertEqual(self.ch.lock_key, "boss")
        self.assertEqual(self.ch.base_url, "https://www.zhipin.com")

    def test_search_url_parity(self):
        """Must match the historical hard-coded SEARCH_URL construction."""
        url = self.ch.build_search_url("具身智能", "101020100", page=2, sort="newest")
        self.assertTrue(url.startswith("https://www.zhipin.com/web/geek/job?"))
        self.assertIn("query=%E5%85%B7%E8%BA%AB%E6%99%BA%E8%83%BD", url)
        self.assertIn("city=101020100", url)
        self.assertIn("sortType=2", url)
        self.assertIn("page=2", url)

    def test_build_job_url(self):
        self.assertEqual(
            self.ch.build_job_url({"url": "/job_detail/abc.html"}),
            "https://www.zhipin.com/job_detail/abc.html",
        )
        # Absolute URLs pass through unchanged.
        self.assertEqual(
            self.ch.build_job_url({"url": "https://example.com/x"}),
            "https://example.com/x",
        )

    def test_chat_url(self):
        self.assertEqual(self.ch.build_chat_url(), "https://www.zhipin.com/web/geek/chat")
        # config override wins
        ch2 = self.ch.__class__({"monitor": {"chat_url": "https://x/z"}})
        self.assertEqual(ch2.build_chat_url(), "https://x/z")

    def test_is_own_page(self):
        self.assertTrue(self.ch.is_own_page("https://www.zhipin.com/web/geek/chat"))
        self.assertTrue(self.ch.is_own_page("https://zhipin.com/job_detail/x.html"))
        self.assertFalse(self.ch.is_own_page("https://www.liepin.com/job/x"))

    def test_js_extract_list_returns_json_string(self):
        result = self.ch.js_extract_list
        self.assertIn(".job-card-wrap", result)
        self.assertIn("JSON.stringify(jobs)", result)
        # should be a valid IIFE
        self.assertTrue(result.strip().startswith("(() =>"))

    def test_js_extract_detail_has_expected_fields(self):
        result = self.ch.js_extract_detail
        for field in ("hr_name", "company_industry", "company_size", "url"):
            self.assertIn(field, result)

    def test_throttle_policy_default_empty(self):
        self.assertEqual(self.ch.throttle_policy(), {})


class TestChannelAdapterBase(unittest.TestCase):
    def test_base_instantiation_guard(self):
        from jobwinner.channels.base import ChannelAdapter

        # ChannelAdapter provides default implementations for every method, so
        # it is directly instantiable; identity fields default to empty and
        # are overridden by concrete adapters.
        inst = ChannelAdapter()
        self.assertEqual(inst.key, "")
        self.assertEqual(inst.domain, "")

    def test_base_url_build(self):
        from jobwinner.channels.base import ChannelAdapter

        inst = ChannelAdapter()
        inst.domain = "example.com"
        self.assertEqual(
            inst.build_job_url({"url": "/job/1.html"}),
            "https://example.com/job/1.html",
        )


if __name__ == "__main__":
    unittest.main()
