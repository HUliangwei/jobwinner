"""官网投递进度巡检：打开各企业招聘官网的投递记录页，检测投递状态变化并写回看板 stage。

设计约束：
- 需要用户在 BossHunter Chrome 中已登录对应官网校招账号，否则页面跳转登录页，巡检自动跳过。
- 巡检频率由调用方决定（面板启动 / 监测任务启动时各跑一轮，不自动循环）。
- 仅巡检 url 指向官网投递记录页的 portal 岗位（如恒玄 https://bestechnic.zhiye.com/personal/deliveryRecord）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid


# 官网状态文本 → 看板 stage 映射（按优先级匹配：具体先于通用）
STAGE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"简历初筛[—\-]未处理|简历筛选[—\-]未处理|已投递[—\-]未处理"), "筛选"),
    (re.compile(r"已通过|通过筛选|简历通过|初筛通过|筛选通过"), "筛选通过"),
    (re.compile(r"笔试"), "笔试"),
    (re.compile(r"面试|一面|二面|三面|业务面|HR面|hr面"), "面试"),
    (re.compile(r"谈薪|薪资|offer沟通"), "谈薪"),
    (re.compile(r"已录用|录用|Offer|offer|发放offer|通过全部"), "Offer"),
    (re.compile(r"已拒绝|不合适|未通过|淘汰|已结束|流程终止"), "已拒绝"),
    (re.compile(r"简历筛选|筛选|初筛|审查|审核"), "筛选"),
]

LOGIN_REDIRECT_MARKERS = (
    "login",
    "passport",
    "sso",
)


def _parse_stage_detail(text: str) -> tuple[str | None, str | None]:
    """从官网状态文本推断招聘阶段，并附带原始状态文本。

    返回 (stage, raw_status)。raw_status 为命中的原文（如"简历初筛-未处理"）。
    """
    if not text:
        return None, None
    for pattern, stage in STAGE_RULES:
        m = pattern.search(text)
        if m:
            raw = m.group(0).strip()
            return stage, raw
    return None, None


def _extract_raw_status(text: str, needle: str) -> str | None:
    """在岗位标题(needle)附近查找 `状态:` 标签，返回其后的官网状态原文。

    兆易(mokahr)等页面结构：
        <岗位标题>
        状态:
        <状态原文>   ← 取这一行
        项目:
        ...
    返回状态原文（strip 后）；找不到返回 None。
    needle 为已 re.escape 的岗位标题正则。
    """
    if not text or not needle:
        return None
    # 用正则匹配 needle（调用方传入的是 re.escape 后的标题）
    m_title = re.search(needle, text)
    if not m_title:
        return None
    # 在标题后 400 字符内找 状态: 标签
    window = text[m_title.end(): m_title.end() + 400]
    m = re.search(r"状态\s*[:：]\s*\n\s*([^\n]+)", window)
    if not m:
        m = re.search(r"状态\s*[:：]\s*([^\n]+)", window)
    if not m:
        # 恒玄等站点格式：当前进度：简历初筛-未处理
        m = re.search(r"当前进度\s*[:：]\s*([^\n]+)", window)
    if not m:
        return None
    raw = m.group(1).strip()
    # 排除"状态:"后面跟的是"项目/时间"等非状态行
    if not raw or raw in ("项目", "投递记录", "个人资料") or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return None
    return raw


def _looks_like_login_url(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in LOGIN_REDIRECT_MARKERS)


def _safe_close(tab_id: str) -> None:
    try:
        from bosshunter.browser import close_tab

        close_tab(tab_id)
    except Exception:
        pass


def check_single_record_page(
    url: str,
    *,
    stop_event=None,
    wait_seconds: int = 6,
) -> dict:
    """打开一个官网投递记录页，返回页面文本与登录状态。"""
    from bosshunter.browser import evaluate, get_page_info, new_tab

    def stopped() -> bool:
        try:
            return bool(stop_event and stop_event.is_set())
        except Exception:
            return False

    if stopped():
        return {"ok": False, "error": "stopped"}
    tab_id = new_tab(url, background=True)
    if not tab_id:
        return {"ok": False, "error": "open_failed"}
    try:
        # 等待页面加载；期间可被停止打断
        deadline = time.time() + wait_seconds + 2
        while time.time() < deadline:
            if stopped():
                _safe_close(tab_id)
                return {"ok": False, "error": "stopped"}
            time.sleep(0.5)
            try:
                info = get_page_info(tab_id)
                final_url = (info or {}).get("url") or ""
                if final_url and not final_url.endswith("about:blank"):
                    break
            except Exception:
                continue

        info = get_page_info(tab_id)
        final_url = str((info or {}).get("url") or "")
        title = str((info or {}).get("title") or "")
        if _looks_like_login_url(final_url):
            return {"ok": False, "error": "not_logged_in", "redirect_url": final_url}

        # 抓取页面主要文本；轮询直到文本稳定/足够长（应对 SPA 慢加载）
        text = ""
        prev_len = -1
        stable_rounds = 0
        poll_deadline = time.time() + wait_seconds + 4
        while time.time() < poll_deadline:
            if stopped():
                _safe_close(tab_id)
                return {"ok": False, "error": "stopped"}
            js = (
                "(() => {"
                "const el = document.querySelector('body');"
                "const t = el ? el.innerText : '';"
                "return JSON.stringify({text: t.slice(0, 10000), url: location.href});"
                "})()"
            )
            try:
                raw = evaluate(tab_id, js)
                payload = raw.get("value") if isinstance(raw, dict) else raw
                if isinstance(payload, str):
                    data = json.loads(payload)
                elif isinstance(payload, dict):
                    data = payload
                else:
                    data = {}
                text = data.get("text", "") or ""
                if len(text) >= 300:
                    break
                if len(text) == prev_len:
                    stable_rounds += 1
                    if stable_rounds >= 2:
                        break
                else:
                    stable_rounds = 0
                prev_len = len(text)
            except Exception:
                pass
            time.sleep(1.2)
        return {
            "ok": True,
            "url": final_url,
            "title": title,
            "text": text,
        }
    finally:
        _safe_close(tab_id)


def _update_job_stage(
    job_id: str,
    stage: str,
    detail: str,
    conn: sqlite3.Connection | None = None,
    stage_source: str = "auto",
) -> bool:
    from bosshunter.db import get_db, set_job_stage

    local = conn is None
    db = conn or get_db()
    try:
        return bool(set_job_stage(db, job_id, stage, detail, stage_source=stage_source))
    finally:
        if local:
            db.close()


def _mark_job_unknown(
    job_id: str,
    detail: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """官网记录页打开成功但查不到该岗位 → 标 unknown（不覆盖已有 auto 进度）。"""
    from bosshunter.db import get_db

    local = conn is None
    db = conn or get_db()
    try:
        row = db.execute(
            "SELECT stage_source FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
        ).fetchone()
        if not row or not row[0] or row[0] == "unknown":
            # 无来源或已是 unknown → 无需重复标记
            return False
        if row[0] == "auto":
            # 已有官网同步进度：查不到新信息不降级为 unknown，保留现有
            return False
        # manual → 覆盖为 unknown 不合适（手动是用户确认过的）；但需求"查不到显示查不到"
        # 若页面打开成功却查不到岗位 → 标记 unknown 让用户知晓官网无此投递记录
        db.execute(
            "UPDATE jobs SET stage_source='unknown', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        db.execute(
            "INSERT INTO history (job_id, action, detail, created_at) VALUES (?, 'stage_unknown', ?, CURRENT_TIMESTAMP)",
            (job_id, detail),
        )
        db.commit()
        return True
    finally:
        if local:
            db.close()


def check_portal_progress(
    *,
    stop_event=None,
    wait_seconds: int = 6,
    log=None,
    db_conn: sqlite3.Connection | None = None,
) -> dict:
    """巡检所有 portal 岗位的官网投递记录页，检测并写回 stage。

    聚合逻辑：
    - 按 url 去重（同一官网记录页只开一次），解析页面文本 → 每页一次。
    - 对每个岗位：若其官网记录 url 命中了该页文本中的某个状态词，则尝试更新 stage。
    - 未登录 / 打开失败的页面跳过。
    """
    from bosshunter.db import get_db

    def emit(msg: str) -> None:
        if callable(log):
            log(msg)

    local = db_conn is None
    db = db_conn or get_db()
    try:
        rows = db.execute(
            "SELECT id, company, title, url, stage FROM jobs "
            "WHERE source='portal' AND deleted_at IS NULL AND url IS NOT NULL AND url != ''"
        ).fetchall()
        portal = [dict(r) for r in rows]
    finally:
        if local:
            db.close()

    # 只巡检"记录页"型的 url：含 deliveryRecord / delivery / applications / apply-record 等
    record_urls: dict[str, dict] = {}
    for job in portal:
        u = str(job.get("url") or "").strip()
        if not u or u.startswith("portal://") or _looks_like_login_url(u):
            continue
        if re.search(r"deliveryRecord|delivery|application|apply|record|progress", u, re.I):
            record_urls.setdefault(u, {"jobs": [], "company": job["company"]})
            record_urls[u]["jobs"].append(job)

    if not record_urls:
        emit("官网巡检：无可巡检的投递记录页（已登录官网并提供记录页链接后生效）")
        return {"checked": 0, "updated": 0, "skipped": 0, "notes": ["no_record_urls"]}

    emit(f"官网巡检：发现 {len(record_urls)} 个官网投递记录页待检查")
    updated: list[str] = []
    skipped: list[str] = []
    task_id = uuid.uuid4().hex[:8]

    for url, meta in record_urls.items():
        result = check_single_record_page(url, stop_event=stop_event, wait_seconds=wait_seconds)
        if not result.get("ok"):
            reason = result.get("error", "unknown")
            skipped.append(url)
            emit(f"官网巡检：跳过 {meta['company']}（{reason}）")
            if reason == "not_logged_in":
                emit(f"官网巡检：{meta['company']} 投递记录页未登录，请在 Chrome 登录该官网校招账号")
            continue

        text = result.get("text", "")
        emit(f"官网巡检：{meta['company']} 记录页已读取（{len(text)} 字符）")

        # 尝试按岗位标题在页面文本中定位("职位名"附近的状态词)
        db = db_conn or get_db()
        try:
            for job in meta["jobs"]:
                title = str(job.get("title") or "")
                needle = re.escape(title[:8])
                m = re.search(needle, text)
                if not m:
                    # 标题太长截断仍找不到 → 尝试公司名
                    needle_c = re.escape(str(meta["company"])[:6])
                    m = re.search(needle_c, text)
                if not m:
                    # 页面文本不含该岗位 → 官网记录页查不到：标 unknown（保留已有进度）
                    did_mark = _mark_job_unknown(job["id"], f"官网记录页查不到该岗位: {url}", conn=db)
                    if did_mark:
                        emit(f"官网巡检：{meta['company']} {title[:18]} 记录页查不到，已标记 unknown")
                    updated.append(("unknown", job["id"], "no_match_in_page"))
                    continue
                start = max(m.start() - 200, 0)
                end = min(m.end() + 400, len(text))
                window = text[start:end]

                # 优先：直接取官网"状态:"原文（如 人才库 / 简历筛选中），如实反映官网
                raw_status = _extract_raw_status(window, needle) or _extract_raw_status(text, needle)
                if raw_status:
                    stage = raw_status
                else:
                    stage, raw_status = _parse_stage_detail(window)
                    if not stage:
                        stage, raw_status = _parse_stage_detail(text)
                if not stage:
                    # 页面有该岗位但读不出状态词（如仅标题行）→ 保守标 unknown
                    did_mark = _mark_job_unknown(job["id"], f"官网记录页读到岗位但状态未识别: {url}", conn=db)
                    if did_mark:
                        emit(f"官网巡检：{meta['company']} {title[:18]} 状态未识别，标记 unknown")
                    updated.append(("unknown", job["id"], "unrecognized_status"))
                    continue
                old_stage = job.get("stage")
                if old_stage == stage:
                    continue
                raw_txt = f"（官网原文：{raw_status}）" if raw_status else ""
                ok = _update_job_stage(
                    job["id"], stage, f"官网进度巡检(#{task_id}): {url} {raw_txt}",
                    conn=db, stage_source="auto",
                )
                if ok:
                    updated.append(("update", job["id"], f"{old_stage or '已投递'}→{stage}"))
                    emit(f"官网巡检：{meta['company']} {title[:18]} 进度更新(官网) {old_stage or '?'}→{stage}{raw_txt}")
        finally:
            if db_conn is None:
                db.close()

    updates = [u for u in updated if u[0] == "update"]
    emit(f"官网巡检完成：检查 {len(record_urls)} 页，更新 {len(updates)} 个岗位进度")
    return {
        "checked": len(record_urls),
        "updated": len(updates),
        "skipped": len(skipped),
        "notes": [u[1] for u in updates if u[0] == "update"],
    }
