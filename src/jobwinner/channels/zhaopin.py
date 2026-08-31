"""智联招聘 (zhaopin.com) channel adapter.

新增渠道（阶段 C）示例：采集侧完整可用（搜索 → 逐卡点击提取 → 详情抽取），
发送/监测暂未接入（``supports_send = False``），后续按 site-patterns 文档补齐。

当前页面结构要点：
- 搜索页已从 sou.zhaopin.com 迁移到 https://www.zhaopin.com/jobs?jl={code}&kw={kw}
- 新版是「左列表 + 右详情面板」split 布局：卡片 ``.job-card``，右侧面板 ``.job-detail-summary``
- 卡片自身不带岗位链接，岗位 URL 只能从右侧面板 ``.job-company-info__view-all`` 读取
- 故列表提取采用「逐卡点击 → 读右侧面板」策略（一次 async evaluate 内完成）
- 未登录时薪资被模糊为 ``**-**元``（登录后恢复真实值），城市码已验证（见 city_codes）
"""

from __future__ import annotations

from jobwinner.channels.base import ChannelAdapter
from jobwinner.channels import register_channel

# 智联城市编码（jl=x，2026-08 实机验证，页面标题 "XX热门职位招聘" 为准）：
# 北京530 上海538 深圳765 杭州653 合肥517 长沙/长株潭749。
# 其余城市可用 config.search.city_codes 补充（或参照站点切换城市后的 URL）。
ZHAOPIN_CITY_CODES: dict[str, str] = {
    "北京": "530",
    "上海": "538",
    "深圳": "765",
    "杭州": "653",
    "合肥": "517",
    "长沙": "749",  # 智联将长沙归入「长株潭」区域
}

SEARCH_URL = "https://www.zhaopin.com/jobs?jl={city_code}&kw={keyword}"

# JS: 从搜索页提取岗位卡片。逐卡点击右侧面板渲染后读取 view-all 链接与字段，
# 轮询 href 变化以便尽早继续（数据在本地 store，通常 <200ms）。
# 注意 Runtime 的 evaluate 命令硬超时 5s，这里上限 15 卡/页。
JS_EXTRACT_LIST = """
(() => {
    // 新版智联搜索页在 <script> 内联 window.__INITIAL_STATE__（SSR 状态）。
    // positionList 即当前页岗位数组；每个岗位的 jobDetailData.position 携带完整详情
    // （真实薪资 base.salaryReal / 经验 / 学历 / 全文 JD desc.description），
    // 列表页即可拿全 —— 无需逐卡点击，也无需再开详情页（采集只开列表页）。
    const s = window.__INITIAL_STATE__ || {};
    const list = Array.isArray(s.positionList) ? s.positionList : [];
    const jobs = [];
    const stripHtml = (html) => String(html || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;|&#160;/g, ' ').replace(/\s{2,}/g, ' ').trim();
    for (let i = 0; i < list.length; i++) {
        const p = list[i] || {};
        const pos = (p.jobDetailData || {}).position || {};
        const base = pos.base || {};
        const total = base.salaryReal || base.salary || p.salary60 || '';
        const cc = (() => { try { return JSON.parse(p.cardCustomJson || '{}'); } catch (e) { return {}; } })();
        const u = p.positionUrl || pos.positionUrl || base.positionUrl || (p.number ? 'https://www.zhaopin.com/jobdetail/' + p.number + '.htm' : '');
        if (!u || !p.number) continue;
        const descObj = pos.desc || {};
        const jd = stripHtml(descObj.description || p.jobSummary || '');
        if (!jd) continue;
        jobs.push({
            title: p.name || base.positionName || '',
            salary: total,
            url: String(u).split('?')[0],
            location: (cc.address && cc.address.trim()) || p.cityDistrict || '',
            experience: p.workingExp || base.positionWorkingExp || '',
            education: p.education || base.education || '',
            company: p.companyName || '',
            company_size: p.companySize || '',
            company_industry: p.industryName || '',
            jd: jd,
            jobNumber: p.number,
        });
    }
    return JSON.stringify(jobs);
})()
"""

# JS: 独立岗位详情页 (www.zhaopin.com/jobdetail/xxx.htm) 字段提取。
JS_EXTRACT_DETAIL = """
(() => {
    const q = (s) => document.querySelector(s);
    const txt = (e) => e ? e.innerText.replace(/\\s+/g, ' ').trim() : '';
    const titleEl = q('h1');
    const title = titleEl ? titleEl.innerText.trim() : (document.title.split('_')[0] || '');
    const salary = txt(q('[class*="salary"]'));
    const jdEl = q('.describtion-card__detail-content') || q('.describtion-card') || q('[class*="describtion"]');
    const companyMeta = txt(q('.company-info__header-left') || q('.company-info__meta') || q('.company-info__header'));
    const desc = txt(q('.company-info__desc') || q('.company-info__meta'));
    const company = (companyMeta.split(' ')[0] || '').trim();
    let company_size = '', company_industry = '';
    const parts = desc.split('·').map(s => s.trim());
    if (parts.length >= 3) {
        company_size = parts[1];
        company_industry = (parts[2].split(' ')[0] || parts[2]).trim();
    }
    return JSON.stringify({
        title: title,
        salary: salary,
        jd: jdEl ? jdEl.innerText.trim() : '',
        company: company,
        company_size: company_size,
        company_industry: company_industry,
        hr_name: '',
        hr_title: '',
        hr_active: '',
        url: location.pathname,
    });
})()
"""


@register_channel
class ZhaopinAdapter(ChannelAdapter):
    """智联招聘 (zhaopin.com) channel — 采集侧已完成，发送待接入。"""

    key = "zhaopin"
    label = "智联招聘"
    domain = "zhaopin.com"
    base_url = "https://www.zhaopin.com"
    lock_key = "zhaopin"
    search_url_template = SEARCH_URL
    city_codes = ZHAOPIN_CITY_CODES
    js_extract_list = JS_EXTRACT_LIST
    js_extract_detail = JS_EXTRACT_DETAIL
    # 监测（消息中心回复检测）已接入：executor/monitor.py::_check_zhaopin_channel_replies。
    default_chat_url = "https://i.zhaopin.com/im"
    # 投递 + 招呼语发送已接入（executor/sender.py 的 _send_zhaopin_greeting_once 链路）。
    supports_send = True

    # 新版 SPA 忽略 ?page=N，列表页 20 条/页；登录后可能解锁翻页控件再放开。
    pages_cap = 1
    # 列表页 __INITIAL_STATE__ 已含完整 JD/真实薪资，无需逐条开详情页。
    detail_required = False

    def generate_job_id(self, url: str) -> str:
        """智联岗位 URL 形如 /jobdetail/CC315847110J40875174507.htm，
        岗位号在 /jobdetail/ 之后、.htm 之前。"""
        import hashlib
        import re as _re

        match = _re.search(r'/jobdetail/([^.]+)', url or "")
        if match:
            return match.group(1)
        return hashlib.md5((url or "").encode()).hexdigest()[:16]
