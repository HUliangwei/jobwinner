"""Boss直聘 (zhipin.com) channel adapter.

Migrated from the hard-coded platform bits that used to live in
``scraper/jobs.py`` / ``executor/sender.py`` / ``executor/monitor.py``.
Behavior is identical; only the data source moved.
"""

from __future__ import annotations

from jobwinner.channels.base import ChannelAdapter
from jobwinner.channels import register_channel

# BOSS直聘搜索页 URL 模板
SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}"

# JS: 从搜索列表页提取岗位卡片数据
JS_EXTRACT_LIST = """
(() => {
    const wraps = document.querySelectorAll('.job-card-wrap');
    const jobs = [];
    wraps.forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const salaryEl = box.querySelector('.job-salary');
        const tags = box.querySelectorAll('.tag-list li');
        const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
        const locationEl = box.querySelector('.company-location');
        const href = nameEl ? nameEl.getAttribute('href') : '';

        if (!nameEl || !href) return;

        jobs.push({
            title: nameEl.textContent.trim(),
            salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: tags[0] ? tags[0].textContent.trim() : '',
            education: tags[1] ? tags[1].textContent.trim() : '',
            company: companyEl ? companyEl.textContent.trim() : '',
            location: locationEl ? locationEl.textContent.trim() : '',
            url: href
        });
    });
    return JSON.stringify(jobs);
})()
"""

# JS: 从详情页提取完整岗位信息
JS_EXTRACT_DETAIL = """
(() => {
    const info = {};
    // Title and salary
    info.title = document.querySelector('.info-primary .name h1')?.textContent?.trim()
        || document.querySelector('.name h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim();
    info.salary = document.querySelector('.info-primary .salary')?.textContent?.trim()
        || document.querySelector('.salary')?.textContent?.trim() || '';

    // Tags (experience, education, etc)
    const tagItems = document.querySelectorAll('.info-primary .tag-list span');
    const tagTexts = Array.from(tagItems).map(t => t.textContent.trim());
    info.experience = tagTexts[0] || '';
    info.education = tagTexts[1] || '';

    // JD
    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim() || '';

    // Company info - try multiple selectors
    const companyLinks = document.querySelectorAll('.sider-company .company-info a');
    info.company = '';
    for (const link of companyLinks) {
        const text = link.textContent.trim();
        if (text && text.length > 0 && !text.includes('http')) {
            info.company = text;
            break;
        }
    }
    if (!info.company) {
        // Fallback: extract from page title "「职位」_公司名招聘"
        const titleMatch = document.title.match(/_(.+?)招聘/);
        info.company = titleMatch ? titleMatch[1] : '';
    }

    // Company details
    const companyTags = document.querySelectorAll('.sider-company .res-industry-item, .company-info-item');
    info.company_size = '';
    info.company_industry = '';
    companyTags.forEach(tag => {
        const text = tag.textContent.trim();
        if (text.includes('人')) info.company_size = text;
        else if (!info.company_industry) info.company_industry = text;
    });

    // HR info
    const bossSection = document.querySelector('.boss-info-attr') || document.querySelector('.job-boss-info');
    if (bossSection) {
        const nameEl = bossSection.querySelector('.name');
        const titleEl = bossSection.querySelector('.title');
        info.hr_name = nameEl?.textContent?.trim() || '';
        info.hr_title = titleEl?.textContent?.trim() || '';
    } else {
        info.hr_name = '';
        info.hr_title = '';
    }
    info.hr_active = document.querySelector('.boss-active-time')?.textContent?.trim() || '';

    // URL
    info.url = window.location.pathname;

    return JSON.stringify(info);
})()
"""


@register_channel
class BosszpAdapter(ChannelAdapter):
    """Boss直聘 (zhipin.com) channel."""

    key = "bosszp"
    label = "Boss直聘"
    domain = "zhipin.com"
    base_url = "https://www.zhipin.com"
    lock_key = "boss"
    search_url_template = SEARCH_URL
    js_extract_list = JS_EXTRACT_LIST
    js_extract_detail = JS_EXTRACT_DETAIL
    default_chat_url = "https://www.zhipin.com/web/geek/chat"
