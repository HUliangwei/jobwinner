"""Shared job filtering helpers."""

import re


def matching_deal_breaker(text: str, deal_breakers: list[str]) -> str | None:
    """Return the first deal-breaker keyword found in text."""
    text_lower = text.lower()
    for keyword in deal_breakers:
        cleaned_keyword = keyword.strip()
        if cleaned_keyword and cleaned_keyword.lower() in text_lower:
            return keyword
    return None


def matching_blocked_company(company: str, blocked_companies: list[str]) -> str | None:
    """Return the first blocked-company rule contained in a company name."""
    company_lower = str(company or "").strip().lower()
    for rule in blocked_companies or []:
        cleaned_rule = str(rule or "").strip()
        if cleaned_rule and cleaned_rule.lower() in company_lower:
            return cleaned_rule
    return None


def parse_monthly_salary_k(salary: str) -> tuple[float, float] | None:
    """Parse common monthly salary labels into a comparable K-range.

    Supports both ``25-50K`` (BOSS直聘) and ``2.5-3.5万`` (智联招聘,
    converted: 万/月 x10 -> K/月, i.e. 2.5万 = 25K).
    """
    normalized = str(salary or "").strip()

    # 元/月 ranges (智联部分岗位直接给元，如 "6000-12000元") -> K = /1000。
    # 日薪/时薪("150-200元/天") 由 (?!\s*/) 排除；低于 1000 元视为日薪级别不解析。
    yuan_range = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*元(?!\s*/)", normalized)
    if yuan_range:
        low, high = (float(value) / 1000 for value in yuan_range.groups())
        if low >= 1 and high >= 1:
            return (min(low, high), max(low, high))

    # 万/月 first: otherwise the plain K-range regex would read "2.5-3.5万"
    # as a 2.5-3.5K range and silently undercut the salary filter.
    wan_range = re.search(
        r"(\d+(?:\.\d+)?)\s*[wW万]?\s*-\s*(\d+(?:\.\d+)?)\s*[wW万]",
        normalized,
    )
    if wan_range:
        low, high = (float(value) * 10 for value in wan_range.groups())
        return (min(low, high), max(low, high))

    wan_single = re.search(r"(\d+(?:\.\d+)?)\s*[wW万](?!\w)", normalized)
    if wan_single:
        value = float(wan_single.group(1)) * 10
        return value, value

    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*[kK]?\s*-\s*(\d+(?:\.\d+)?)\s*[kK]",
        normalized,
    )
    if range_match:
        low, high = (float(value) for value in range_match.groups())
        return (min(low, high), max(low, high))

    single_match = re.search(r"(\d+(?:\.\d+)?)\s*[kK](?!\w)", normalized)
    if single_match:
        value = float(single_match.group(1))
        return value, value
    return None
