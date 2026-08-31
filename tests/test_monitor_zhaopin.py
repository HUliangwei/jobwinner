# -*- coding: utf-8 -*-
"""智联消息中心监测：会话匹配 + 会话列表提取 JS。"""

import unittest

from jobwinner.executor.monitor import (
    JS_EXTRACT_CHAT_LIST_ZHAOPIN,
    _match_zhaopin_conversation_to_job,
)


class TestMatchZhaopinConversation(unittest.TestCase):
    """会话 → 岗位匹配（company + job title 三档）。"""

    def setUp(self):
        self.jobs = [
            {"id": "zp1", "company": "易企查(北京)信息科技", "title": "AI Agent丨开发工程师", "status": "sent"},
            {"id": "zp2", "company": "华为", "title": "软件测试工程师", "status": "sent"},
            {"id": "zp3", "company": "华为", "title": "AI软件开发工程师", "status": "replied"},
            {"id": "zp4", "company": "新东方教育科技集团", "title": "27届校招南京-高中物理教师", "status": "sent"},
        ]

    def test_exact_company_title(self):
        conv = {"company": "易企查(北京)信息科技", "job_title": "AI Agent丨开发工程师"}
        self.assertIs(_match_zhaopin_conversation_to_job(conv, self.jobs), self.jobs[0])

    def test_substring_company_title(self):
        # 会话里公司名带后缀/岗位名略有差异时按包含匹配
        conv = {"company": "易企查(北京)信息科技有限公司", "job_title": "AI Agent开发工程师"}
        self.assertIs(_match_zhaopin_conversation_to_job(conv, self.jobs), self.jobs[0])

    def test_same_company_two_titles_resolve_by_title(self):
        # 同一公司多个岗位：精确 title 命中正确的那个
        conv = {"company": "华为", "job_title": "AI软件开发工程师"}
        self.assertIs(_match_zhaopin_conversation_to_job(conv, self.jobs), self.jobs[2])

    def test_company_only_fallback(self):
        # title 缺失/无法匹配时退回公司级匹配（取第一条同公司）
        conv = {"company": "新东方教育科技集团", "job_title": ""}
        got = _match_zhaopin_conversation_to_job(conv, self.jobs)
        self.assertIsNotNone(got)
        self.assertEqual(got["company"], "新东方教育科技集团")

    def test_no_match_returns_none(self):
        conv = {"company": "阿里巴巴", "job_title": "前端工程师"}
        self.assertIsNone(_match_zhaopin_conversation_to_job(conv, self.jobs))

    def test_empty_company_returns_none(self):
        self.assertIsNone(_match_zhaopin_conversation_to_job({"company": "", "job_title": "x"}, self.jobs))

    def test_whitespace_insensitive(self):
        conv = {"company": " 华为 ", "job_title": "AI 软件开发 工程师"}
        self.assertIs(_match_zhaopin_conversation_to_job(conv, self.jobs), self.jobs[2])


class TestZhaopinChatListJS(unittest.TestCase):
    """会话列表提取 JS 的静态断言（防选择器漂移）。"""

    def test_extract_references_im_session_item(self):
        self.assertIn(".im-session-item", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn(".im-session-item__preview-row", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn(".im-session-item__name", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn(".im-session-item__company-name", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn(".im-session-item__job", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn(".im-session-item__badge", JS_EXTRACT_CHAT_LIST_ZHAOPIN)

    def test_extract_returns_json_stringify(self):
        self.assertIn("JSON.stringify(results)", JS_EXTRACT_CHAT_LIST_ZHAOPIN)
        self.assertIn("results.push({", JS_EXTRACT_CHAT_LIST_ZHAOPIN)

    def test_extract_tracks_unread_and_hr_fields(self):
        for field in ("hr_name", "company", "job_title", "last_message", "unread"):
            self.assertIn(field, JS_EXTRACT_CHAT_LIST_ZHAOPIN)


if __name__ == "__main__":
    unittest.main()