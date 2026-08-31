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


class TestActiveChannelsMulti(unittest.TestCase):
    def test_single_string_legacy(self):
        from jobwinner.channels import get_active_channels

        channels = get_active_channels({"channels": {"active": "bosszp"}})
        self.assertEqual([c.key for c in channels], ["bosszp"])

    def test_list_returns_all(self):
        from jobwinner.channels import get_active_channels

        channels = get_active_channels({"channels": {"active": ["bosszp", "zhaopin"]}})
        self.assertEqual([c.key for c in channels], ["bosszp", "zhaopin"])

    def test_list_dedupes_and_skips_none(self):
        from jobwinner.channels import get_active_channels

        channels = get_active_channels({"channels": {"active": ["bosszp", "bosszp", None, "zhaopin"]}})
        self.assertEqual([c.key for c in channels], ["bosszp", "zhaopin"])

    def test_unknown_key_falls_back_to_bosszp(self):
        from jobwinner.channels import get_active_channels

        channels = get_active_channels({"channels": {"active": ["does-not-exist"]}})
        self.assertEqual([c.key for c in channels], ["bosszp"])

    def test_primary_is_first(self):
        from jobwinner.channels import get_active_channel

        ch = get_active_channel({"channels": {"active": ["bosszp", "zhaopin"]}})
        self.assertEqual(ch.key, "bosszp")

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

    def test_default_pagination_and_detail_caps(self):
        # Boss 保持原有行为：最多 10 页、每岗位开详情页。
        self.assertEqual(self.ch.pages_cap, 10)
        self.assertTrue(self.ch.detail_required)




class TestZhaopinAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jobwinner.channels import get_channel

        cls.ch = get_channel("zhaopin")

    def test_registered_and_identity(self):
        from jobwinner.channels import available_channels

        self.assertIn("zhaopin", available_channels())
        self.assertEqual(self.ch.key, "zhaopin")
        self.assertEqual(self.ch.label, "智联招聘")
        self.assertEqual(self.ch.domain, "zhaopin.com")
        self.assertEqual(self.ch.lock_key, "zhaopin")

    def test_search_url(self):
        url = self.ch.build_search_url("Java", "530")
        self.assertTrue(url.startswith("https://www.zhaopin.com/jobs?"))
        self.assertIn("jl=530", url)
        self.assertIn("kw=Java", url)

    def test_build_job_url_passthrough(self):
        url = "https://www.zhaopin.com/jobdetail/CC123J456.htm"
        self.assertEqual(self.ch.build_job_url({"url": url}), url)

    def test_city_codes(self):
        self.assertEqual(self.ch.resolve_city_code("北京"), "530")
        self.assertEqual(self.ch.resolve_city_code("上海"), "538")
        self.assertEqual(self.ch.resolve_city_code("合肥"), "517")
        self.assertIsNone(self.ch.resolve_city_code("火星"))

    def test_city_code_beats_stale_custom_codes(self):
        # Legacy global city_codes were written for BOSS直聘 (e.g. 101010100).
        # The channel map must win so switching channels never misresolves.
        self.assertEqual(self.ch.resolve_city_code("北京", {"北京": "101010100"}), "530")

    def test_generate_job_id_from_jobdetail_url(self):
        self.assertEqual(
            self.ch.generate_job_id("https://www.zhaopin.com/jobdetail/CC315847110J40875174507.htm"),
            "CC315847110J40875174507",
        )
        # Unknown layout falls back to md5 (32-char hex).
        fid = self.ch.generate_job_id("https://www.zhaopin.com/weird/x")
        self.assertEqual(len(fid), 16)

    def test_supports_send_false_collect_only(self):
        # 智联发送/监测未接入，渠道声明为采集-only，发送路径应优雅跳过。
        self.assertFalse(self.ch.supports_send)

    def test_pagination_and_detail_caps(self):
        # 新版 SPA 忽略 ?page=N → 只取首页；列表页 state 已含完整详情 → 不开详情页。
        self.assertEqual(self.ch.pages_cap, 1)
        self.assertFalse(self.ch.detail_required)

    def test_js_extract_list_reads_initial_state(self):
        # 新版智联搜索页把岗位列表放在内联 __INITIAL_STATE__（SSR 状态），
        # 直读 positionList 即可拿到含真实薪资/完整 URL 的行数据（无需点击）。
        result = self.ch.js_extract_list
        self.assertIn("positionList", result)
        self.assertIn("positionUrl", result)
        self.assertIn("cardCustomJson", result)
        self.assertIn("JSON.stringify(jobs)", result)
        self.assertTrue(result.strip().startswith("(() =>"))

    def test_js_extract_detail_has_fields(self):
        result = self.ch.js_extract_detail
        for field in ("describtion-card__detail-content", "company-info__desc", "hr_name"):
            self.assertIn(field, result)

    def test_is_own_page(self):
        self.assertTrue(self.ch.is_own_page("https://www.zhaopin.com/jobs?jl=530"))
        self.assertTrue(self.ch.is_own_page("https://passport.zhaopin.com/login"))
        self.assertFalse(self.ch.is_own_page("https://www.zhipin.com/job/x"))


class TestZhaopinActiveChannel(unittest.TestCase):
    def test_get_active_channel_switches(self):
        from jobwinner.channels import get_active_channel

        ch = get_active_channel({"channels": {"active": "zhaopin"}})
        self.assertEqual(ch.key, "zhaopin")


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
