import unittest

from jobwinner.ai.prefilter import quick_score
from jobwinner.job_filters import matching_blocked_company, parse_monthly_salary_k


class JobFilterTests(unittest.TestCase):
    def test_parse_common_monthly_salary_formats(self):
        self.assertEqual(parse_monthly_salary_k("10-15K"), (10.0, 15.0))
        self.assertEqual(parse_monthly_salary_k("8-13K·13薪"), (8.0, 13.0))
        self.assertEqual(parse_monthly_salary_k("12K"), (12.0, 12.0))

    def test_parse_wan_monthly_salary_formats(self):
        # 智联招聘薪资为「X-X万/月」，按 万×10 = K 换算以复用现有薪资过滤逻辑。
        self.assertEqual(parse_monthly_salary_k("2.5-3.5万"), (25.0, 35.0))
        self.assertEqual(parse_monthly_salary_k("3万"), (30.0, 30.0))
        self.assertEqual(parse_monthly_salary_k("1.2-2万·13薪"), (12.0, 20.0))

    def test_parse_yuan_monthly_salary_ranges(self):
        # 智联部分岗位直接用元/月。
        self.assertEqual(parse_monthly_salary_k("6000-12000元"), (6.0, 12.0))
        # 日薪/时薪不应被误解析为月薪。
        self.assertIsNone(parse_monthly_salary_k("150-200元/天"))
        self.assertIsNone(parse_monthly_salary_k("30-50元/小时"))

    def test_unconvertible_salary_formats_are_not_parsed(self):
        self.assertIsNone(parse_monthly_salary_k("150-200元/天"))
        self.assertIsNone(parse_monthly_salary_k("薪资面议"))

    def test_blocked_company_matches_case_insensitive_substring(self):
        matched = matching_blocked_company("某公司科技有限公司", ["某公司"])

        self.assertEqual(matched, "某公司")

    def test_blocked_company_ignores_empty_rules(self):
        matched = matching_blocked_company("某公司科技有限公司", ["", "  "])

        self.assertIsNone(matched)

    def test_quick_score_filters_existing_job_by_company(self):
        score, reason = quick_score(
            {"title": "产品经理", "company": "某公司科技有限公司", "salary": "20-30K"},
            {"profile": {"blocked_companies": ["某公司"]}},
        )

        self.assertEqual(score, 0)
        self.assertIn("某公司", reason)


if __name__ == "__main__":
    unittest.main()
